#!/usr/bin/env bash
# Push deploy/aws/enshrouded_server.json to the running server and restart it.
# Everything comes from the repo file except:
#   - passwords: from git-ignored deploy/aws/secrets.env (or env vars), else kept from the live config
#   - bannedAccounts: always kept from the live config
#
#   PROFILE=enshrouded REGION=us-east-2 ./deploy/aws/apply-config.sh
set -euo pipefail

PROFILE="${PROFILE:?set PROFILE}"
REGION="${REGION:?set REGION}"
NAME=enshrouded
KEY_FILE="$HOME/.ssh/enshrouded.pem"
KNOWN_HOSTS="$HOME/.ssh/known_hosts_enshrouded"
REPO_CONFIG="$(cd "$(dirname "$0")" && pwd)/enshrouded_server.json"
LIVE_CONFIG=/srv/enshrouded/enshrouded_server.json

log() { echo ">>> $*" >&2; }

SECRETS="$(dirname "$REPO_CONFIG")/secrets.env"
if [ -f "$SECRETS" ]; then
    for var in ADMIN_PW FRIEND_PW GUEST_PW VISITOR_PW; do
        [ -z "${!var:-}" ] && eval "$(grep -E "^$var=" "$SECRETS" | tail -n1)"
    done
fi

EIP=$(aws --profile "$PROFILE" --region "$REGION" ec2 describe-addresses \
    --filters Name=tag:Name,Values="$NAME" --query 'Addresses[0].PublicIp' --output text)
[ "$EIP" = "None" ] && { echo "ERROR: no Elastic IP tagged Name=$NAME in $REGION." >&2; exit 1; }
SSH=(ssh -i "$KEY_FILE" -o UserKnownHostsFile="$KNOWN_HOSTS" -o StrictHostKeyChecking=accept-new "ubuntu@$EIP")

live=$("${SSH[@]}" "sudo cat $LIVE_CONFIG")
merged=$(jq --argjson live "$live" \
    --arg Admin "${ADMIN_PW:-}" --arg Friend "${FRIEND_PW:-}" --arg Guest "${GUEST_PW:-}" --arg Visitor "${VISITOR_PW:-}" '
    {Admin: $Admin, Friend: $Friend, Guest: $Guest, Visitor: $Visitor} as $local
    | .userGroups |= map(. as $g | .password = (
        if ($local[$g.name] // "") != "" then $local[$g.name]
        else ([$live.userGroups[] | select(.name == $g.name) | .password][0] // "") end))
    | .bannedAccounts = ($live.bannedAccounts // [])' "$REPO_CONFIG")

missing=$(jq -r '[.userGroups[] | select(.password == "") | .name] | join(", ")' <<<"$merged")
[ -n "$missing" ] && { echo "ERROR: no live password for group(s): $missing. Set it in $LIVE_CONFIG first." >&2; exit 1; }

log "Changes (live -> repo), passwords hidden:"
# The server rewrites numbers as 1.500000; normalize so only real changes show up
norm='del(.userGroups[].password) | walk(if type == "number" then . + 0 else . end)'
diff <(jq "$norm" <<<"$live") <(jq "$norm" <<<"$merged") && echo "(no setting changes)" >&2 || true
changed_pw=$(jq -rn --argjson a "$live" --argjson b "$merged" \
    '[$b.userGroups[] as $g | select(([$a.userGroups[] | select(.name == $g.name) | .password][0]) != $g.password) | $g.name] | join(", ")')
[ -n "$changed_pw" ] && log "Password changed for: $changed_pw"

log "Stopping server, taking a backup, writing config, starting server on $EIP"
"${SSH[@]}" 'cat > /tmp/enshrouded_server.json.new' <<<"$merged"
"${SSH[@]}" "set -e
    sudo docker stop enshrouded >/dev/null
    sudo systemctl start enshrouded-backup.service
    sudo install -o 1000 -g 1000 -m 644 /tmp/enshrouded_server.json.new $LIVE_CONFIG
    rm -f /tmp/enshrouded_server.json.new
    sudo docker start enshrouded >/dev/null"
log "Done. Follow startup with: ${SSH[*]} 'sudo docker logs -f --since 1m enshrouded'"
