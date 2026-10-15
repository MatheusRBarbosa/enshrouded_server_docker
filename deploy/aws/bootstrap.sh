#!/bin/bash
# EC2 user-data for the Enshrouded server host (Ubuntu 24.04).
# launch.sh replaces __BUCKET__ and __BUCKET_REGION__ before passing this to the instance.
set -euxo pipefail

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y docker.io
usermod -aG docker ubuntu
snap install aws-cli --classic

# Game install, config, savegame/ and logs/ live here (mounted into the container)
mkdir -p /srv/enshrouded
chown 1000:1000 /srv/enshrouded

cat > /etc/enshrouded-backup.env <<'EOF'
BUCKET=__BUCKET__
BUCKET_REGION=__BUCKET_REGION__
DATA_DIR=/srv/enshrouded
EOF

# ---------------------------------------------------------------------------
# enshrouded-backup: tar savegame/ + enshrouded_server.json and upload to S3
# ---------------------------------------------------------------------------
cat > /usr/local/bin/enshrouded-backup <<'EOF'
#!/bin/bash
set -euo pipefail
. /etc/enshrouded-backup.env
AWS=/snap/bin/aws

if [ ! -d "$DATA_DIR/savegame" ]; then
    echo "No $DATA_DIR/savegame yet; nothing to back up."
    exit 0
fi

stamp=$(date -u +%Y%m%dT%H%M%SZ)
archive=$(mktemp --suffix=.tar.gz)
trap 'rm -f "$archive"' EXIT

files=(savegame)
[ -f "$DATA_DIR/enshrouded_server.json" ] && files+=(enshrouded_server.json)

# GNU tar exits 1 when a file changed while being read (server saving mid-backup);
# the archive is still usable and Enshrouded keeps rolling copies, so treat it as a warning.
tar -C "$DATA_DIR" -czf "$archive" "${files[@]}" || { rc=$?; [ $rc -eq 1 ] || exit $rc; echo "WARNING: files changed during backup." >&2; }

"$AWS" s3 cp --region "$BUCKET_REGION" --only-show-errors "$archive" "s3://$BUCKET/world/$stamp.tar.gz"
echo "Backup uploaded: s3://$BUCKET/world/$stamp.tar.gz ($(du -h "$archive" | cut -f1))"
EOF
chmod 755 /usr/local/bin/enshrouded-backup

# ---------------------------------------------------------------------------
# enshrouded-restore [--force] [--target DIR] [KEY]
#   Restores the newest backup (or KEY, e.g. world/20261015T120000Z.tar.gz).
#   Without --force it refuses to overwrite an existing savegame/.
# ---------------------------------------------------------------------------
cat > /usr/local/bin/enshrouded-restore <<'EOF'
#!/bin/bash
set -euo pipefail
. /etc/enshrouded-backup.env
AWS=/snap/bin/aws

force=false
target="$DATA_DIR"
key=""
while [ $# -gt 0 ]; do
    case "$1" in
        --force) force=true ;;
        --target) target="$2"; shift ;;
        *) key="$1" ;;
    esac
    shift
done

if [ -n "$(ls -A "$target/savegame" 2>/dev/null)" ] && [ "$force" != true ]; then
    echo "$target/savegame already has data; skipping restore (use --force to overwrite)."
    exit 0
fi

if [ -z "$key" ]; then
    key=$("$AWS" s3api list-objects-v2 --region "$BUCKET_REGION" --bucket "$BUCKET" --prefix world/ \
        --query 'sort_by(Contents || `[]`, &Key)[-1].Key' --output text)
    if [ -z "$key" ] || [ "$key" = "None" ]; then
        echo "No backups found in s3://$BUCKET/world/; starting with a fresh world."
        exit 0
    fi
fi

archive=$(mktemp --suffix=.tar.gz)
trap 'rm -f "$archive"' EXIT
"$AWS" s3 cp --region "$BUCKET_REGION" --only-show-errors "s3://$BUCKET/$key" "$archive"

mkdir -p "$target"
if [ -d "$target/savegame" ] && [ "$force" = true ]; then
    mv "$target/savegame" "$target/savegame.before-restore-$(date -u +%Y%m%dT%H%M%SZ)"
fi
tar -C "$target" -xzf "$archive"
chown -R 1000:1000 "$target"
echo "Restored s3://$BUCKET/$key into $target"
EOF
chmod 755 /usr/local/bin/enshrouded-restore

# ---------------------------------------------------------------------------
# Back up every 6 hours (Persistent catches up on runs missed while stopped)
# ---------------------------------------------------------------------------
cat > /etc/systemd/system/enshrouded-backup.service <<'EOF'
[Unit]
Description=Back up the Enshrouded world to S3
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/enshrouded-backup
EOF

cat > /etc/systemd/system/enshrouded-backup.timer <<'EOF'
[Unit]
Description=Back up the Enshrouded world to S3 every 6 hours

[Timer]
OnCalendar=*-*-* 00/6:00:00
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now enshrouded-backup.timer
