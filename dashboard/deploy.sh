#!/usr/bin/env bash
# Deploy, verify or remove the server dashboard. Never creates, recreates or deletes the game
# server itself (instance, security group, bucket, world backups); see dashboard/README.md.
#
#   PROFILE=enshrouded REGION=us-east-2 ./dashboard/deploy.sh [deploy|agent|verify|url|remove]
#
#   deploy  (default) everything below; safe to re-run
#   agent   only (re)install the pieces on the instance; launch.sh runs this after replacing it
#   verify  check the deployed resources and that the API refuses unauthenticated requests
#   url     print the dashboard URL
#   remove  delete everything the dashboard added; world backups and the game server stay
#
# Login: DASHBOARD_USERNAME / DASHBOARD_PASSWORD from the environment or the git-ignored
# dashboard/secrets.env. Unset: keep what the Lambda has; none at all: generate and save them.
# Optional env: BUCKET (default enshrouded-backups-<account>-<region>, like deploy/aws/launch.sh)
set -euo pipefail

PROFILE="${PROFILE:?set PROFILE}"
REGION="${REGION:?set REGION}"
ACTION="${1:-deploy}"
NAME=enshrouded                      # tag/name of the game server resources (deploy/aws/launch.sh)
SERVER_ROLE=enshrouded-server        # instance role created by launch.sh
FUNCTION=enshrouded-dashboard        # Lambda, its role and the DynamoDB table share this name
TABLE=enshrouded-dashboard
SCHEDULER_ROLE=enshrouded-scheduler
GROUP=enshrouded                     # EventBridge Scheduler group
DOCUMENT=enshrouded-dashboard-backup # SSM command document
LOG_GROUP=/enshrouded/server
LAMBDA_LOG_GROUP=/aws/lambda/$FUNCTION
METRICS_NAMESPACE=Enshrouded           # memory metric from the CloudWatch agent (backend/metrics.py)
TIMEZONE=America/Sao_Paulo
KEY_FILE="$HOME/.ssh/enshrouded.pem"
KNOWN_HOSTS="$HOME/.ssh/known_hosts_enshrouded"
HERE="$(cd "$(dirname "$0")" && pwd)"
SECRETS="$HERE/secrets.env"

aws() { command aws --profile "$PROFILE" --region "$REGION" "$@"; }
log() { echo ">>> $*" >&2; }

ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
BUCKET="${BUCKET:-enshrouded-backups-$ACCOUNT-$REGION}"
BUCKET_REGION=$(aws s3api get-bucket-location --bucket "$BUCKET" --query LocationConstraint --output text 2>/dev/null) \
    || { echo "ERROR: bucket $BUCKET not found. Run deploy/aws/launch.sh first, or set BUCKET." >&2; exit 1; }
[ "$BUCKET_REGION" = "None" ] && BUCKET_REGION=us-east-1
FUNCTION_ARN="arn:aws:lambda:$REGION:$ACCOUNT:function:$FUNCTION"

running_instance() {  # "<instance-id> <public-ip>" of the running game server, or "None None"
    aws ec2 describe-instances --filters Name=tag:Name,Values="$NAME" Name=instance-state-name,Values=running \
        --query 'Reservations[0].Instances[0].[InstanceId,PublicIpAddress]' --output text
}
server_ip() { running_instance | cut -f2; }
server_running() { [ "$(running_instance | cut -f1)" != "None" ]; }
ssh_server() {  # no Elastic IP: the IP changes on every start, so host keys are stored under the instance ID
    local id ip
    read -r id ip < <(running_instance)
    ssh -i "$KEY_FILE" -o UserKnownHostsFile="$KNOWN_HOSTS" -o HostKeyAlias="$id" -o StrictHostKeyChecking=accept-new \
        -o ConnectTimeout=10 "ubuntu@$ip" "$@"
}
ensure_role() {  # name, trusted service, [condition json]
    if ! aws iam get-role --role-name "$1" >/dev/null 2>&1; then
        log "Creating IAM role $1"
        aws iam create-role --role-name "$1" --assume-role-policy-document "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Principal\":{\"Service\":\"$2\"},\"Action\":\"sts:AssumeRole\"${3:+,\"Condition\":$3}}]}" >/dev/null
    fi
}
ensure_log_group() {  # name, retention days
    local err
    if err=$(aws logs create-log-group --log-group-name "$1" 2>&1); then
        log "Created log group $1"
    elif ! grep -q ResourceAlreadyExists <<<"$err"; then
        echo "$err" >&2; exit 1
    fi
    aws logs put-retention-policy --log-group-name "$1" --retention-in-days "$2"
}

# Fail before changing anything if the deploying user lacks dashboard/iam/deployer-policy.json
preflight() {
    local missing=() probe
    probe() { aws "$@" >/dev/null 2>&1 || missing+=("$1 $2"); }
    probe scheduler list-schedule-groups
    probe dynamodb list-tables
    probe ssm describe-instance-information --max-results 5
    probe logs filter-log-events --log-group-name "$LOG_GROUP" --limit 1
    # filter-log-events also fails while the group doesn't exist yet; only AccessDenied counts
    if [ "${#missing[@]}" -gt 0 ] && [ "${missing[-1]}" = "logs filter-log-events" ]; then
        local out
        out=$(aws logs filter-log-events --log-group-name "$LOG_GROUP" --limit 1 2>&1 || true)
        grep -q AccessDenied <<<"$out" || unset 'missing[-1]'
    fi
    if [ "${#missing[@]}" -gt 0 ]; then
        echo "ERROR: this IAM identity can't use: ${missing[*]}" >&2
        echo "Attach dashboard/iam/deployer-policy.json to it (inline policy), then re-run. Nothing was changed." >&2
        exit 1
    fi
}

# --- Shared infrastructure ---------------------------------------------------------------------
deploy_shared() {
    # Logs are only for recent troubleshooting. CloudWatch has no 2-day option; 3 is the shortest >= 48 h.
    ensure_log_group "$LOG_GROUP" 3
    ensure_log_group "$LAMBDA_LOG_GROUP" 3

    if ! aws dynamodb describe-table --table-name "$TABLE" >/dev/null 2>&1; then
        log "Creating DynamoDB table $TABLE (on-demand)"
        aws dynamodb create-table --table-name "$TABLE" --billing-mode PAY_PER_REQUEST \
            --attribute-definitions AttributeName=pk,AttributeType=S AttributeName=sk,AttributeType=S \
            --key-schema AttributeName=pk,KeyType=HASH AttributeName=sk,KeyType=RANGE \
            --tags "Key=Name,Value=$TABLE" >/dev/null
        aws dynamodb wait table-exists --table-name "$TABLE"
    fi
    if [ "$(aws dynamodb describe-time-to-live --table-name "$TABLE" --query TimeToLiveDescription.TimeToLiveStatus --output text)" = "DISABLED" ]; then
        aws dynamodb update-time-to-live --table-name "$TABLE" --time-to-live-specification Enabled=true,AttributeName=ttl >/dev/null
    fi

    if aws ssm describe-document --name "$DOCUMENT" >/dev/null 2>&1; then
        if version=$(aws ssm update-document --name "$DOCUMENT" --document-version '$LATEST' --document-format JSON \
                --content "file://$HERE/agent/ssm-backup-document.json" --query DocumentDescription.DocumentVersion --output text 2>/dev/null); then
            aws ssm update-document-default-version --name "$DOCUMENT" --document-version "$version" >/dev/null
            log "Updated SSM document $DOCUMENT (version $version)"
        fi
    else
        log "Creating SSM document $DOCUMENT"
        aws ssm create-document --name "$DOCUMENT" --document-type Command --document-format JSON \
            --content "file://$HERE/agent/ssm-backup-document.json" --tags "Key=Name,Value=$DOCUMENT" >/dev/null
    fi
}

# --- Instance -----------------------------------------------------------------------------------
install_agent() {
    log "Granting the instance role what the agent needs (policy dashboard-agent)"
    aws iam put-role-policy --role-name "$SERVER_ROLE" --policy-name dashboard-agent --policy-document "{
      \"Version\": \"2012-10-17\",
      \"Statement\": [
        {\"Sid\": \"Uploads\", \"Effect\": \"Allow\", \"Action\": \"s3:PutObject\",
         \"Resource\": [\"arn:aws:s3:::$BUCKET/dashboard/*\", \"arn:aws:s3:::$BUCKET/downloads/*\", \"arn:aws:s3:::$BUCKET/world/*\"]},
        {\"Sid\": \"ShipLogs\", \"Effect\": \"Allow\", \"Action\": [\"logs:CreateLogStream\", \"logs:PutLogEvents\", \"logs:DescribeLogStreams\"],
         \"Resource\": \"arn:aws:logs:$REGION:$ACCOUNT:log-group:$LOG_GROUP:*\"},
        {\"Sid\": \"PublishMemory\", \"Effect\": \"Allow\", \"Action\": \"cloudwatch:PutMetricData\", \"Resource\": \"*\",
         \"Condition\": {\"StringEquals\": {\"cloudwatch:namespace\": \"$METRICS_NAMESPACE\"}}},
        {\"Sid\": \"RunCommand\", \"Effect\": \"Allow\", \"Action\": [
            \"ssm:UpdateInstanceInformation\", \"ssm:ListInstanceAssociations\", \"ssm:DescribeAssociation\",
            \"ssm:GetDocument\", \"ssm:DescribeDocument\",
            \"ec2messages:AcknowledgeMessage\", \"ec2messages:DeleteMessage\", \"ec2messages:FailMessage\",
            \"ec2messages:GetEndpoint\", \"ec2messages:GetMessages\", \"ec2messages:SendReply\",
            \"ssmmessages:CreateControlChannel\", \"ssmmessages:CreateDataChannel\",
            \"ssmmessages:OpenControlChannel\", \"ssmmessages:OpenDataChannel\"], \"Resource\": \"*\"}
      ]
    }"
    aws iam delete-role-policy --role-name "$SERVER_ROLE" --policy-name dashboard-snapshot 2>/dev/null || true  # older name

    if ! server_running; then
        log "WARNING: the server instance isn't running; agent not installed. Re-run '$0 agent' once it is."
        return 0
    fi
    log "Installing the agent on $(server_ip)"
    local tarball=/tmp/enshrouded-dashboard-agent.tar
    tar -C "$HERE/agent" -cf - enshrouded_inspect.py enshrouded_dashboard_backup.py enshrouded-inspect.service \
            enshrouded-inspect.timer enshrouded-autostart.service cloudwatch-agent.json \
        | ssh_server "cat > $tarball"
    ssh_server "REGION=$REGION TARBALL=$tarball bash -s" <<'REMOTE'
set -euo pipefail
d=$(mktemp -d); trap 'rm -rf "$d"' EXIT
tar -C "$d" -xf "$TARBALL" && rm -f "$TARBALL"
sudo install -m 755 "$d/enshrouded_inspect.py" /usr/local/bin/enshrouded-inspect
sudo install -m 755 "$d/enshrouded_dashboard_backup.py" /usr/local/bin/enshrouded-dashboard-backup
sudo install -m 644 "$d/enshrouded-inspect.service" "$d/enshrouded-inspect.timer" "$d/enshrouded-autostart.service" /etc/systemd/system/
sudo touch /var/log/enshrouded-dashboard-backup.log
sudo systemctl daemon-reload
sudo systemctl enable --now enshrouded-inspect.timer >/dev/null 2>&1
sudo systemctl enable enshrouded-autostart.service >/dev/null 2>&1
# Hourly dashboard backups replace the 6-hourly timer from bootstrap.sh (the service stays for apply-config.sh)
sudo systemctl disable --now enshrouded-backup.timer >/dev/null 2>&1 || true

if ! dpkg -s amazon-cloudwatch-agent >/dev/null 2>&1; then
    echo "installing the CloudWatch agent"
    curl -fsSL -o "$d/cwagent.deb" "https://amazoncloudwatch-agent-$REGION.s3.$REGION.amazonaws.com/ubuntu/amd64/latest/amazon-cloudwatch-agent.deb"
    sudo dpkg -i -E "$d/cwagent.deb" >/dev/null
fi
sudo install -m 644 "$d/cloudwatch-agent.json" /opt/aws/amazon-cloudwatch-agent/etc/enshrouded.json
sudo /opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl -a fetch-config -m ec2 -s \
    -c file:/opt/aws/amazon-cloudwatch-agent/etc/enshrouded.json >/dev/null
# Pick up the new instance-role permissions for Run Command now instead of at the agent's next
# retry (25 min). IAM changes take a few seconds to apply; restarting too early just fails again.
sleep 20
sudo snap restart amazon-ssm-agent >/dev/null
for i in 1 2 3 4 5 6; do sudo systemctl start enshrouded-inspect.service && exit 0; sleep 10; done
sudo journalctl -u enshrouded-inspect.service -n 20 --no-pager; exit 1
REMOTE
    log "Agent, log shipping and backup script installed"
}

# --- Lambda --------------------------------------------------------------------------------------
load_credentials() {
    local current
    current=$(aws lambda get-function-configuration --function-name "$FUNCTION" --query Environment.Variables --output json 2>/dev/null || echo '{}')
    [ "$current" = "null" ] && current='{}'
    if [ -f "$SECRETS" ]; then
        for var in DASHBOARD_USERNAME DASHBOARD_PASSWORD; do
            [ -z "${!var:-}" ] && eval "$(grep -E "^$var=" "$SECRETS" | tail -n1)"
        done
    fi
    DASHBOARD_USERNAME="${DASHBOARD_USERNAME:-$(jq -r '.DASHBOARD_USERNAME // empty' <<<"$current")}"
    DASHBOARD_PASSWORD="${DASHBOARD_PASSWORD:-$(jq -r '.DASHBOARD_PASSWORD // empty' <<<"$current")}"
    SESSION_SECRET="$(jq -r '.DASHBOARD_SESSION_SECRET // empty' <<<"$current")"
    if [ -z "$DASHBOARD_USERNAME" ] || [ -z "$DASHBOARD_PASSWORD" ]; then
        DASHBOARD_USERNAME="${DASHBOARD_USERNAME:-friends}"
        DASHBOARD_PASSWORD=$(python3 -c 'import secrets; print(secrets.token_urlsafe(15))')
        umask 077
        printf 'DASHBOARD_USERNAME=%s\nDASHBOARD_PASSWORD=%s\n' "$DASHBOARD_USERNAME" "$DASHBOARD_PASSWORD" > "$SECRETS"
        log "Generated a dashboard login and saved it to $SECRETS (git-ignored)"
        GENERATED_LOGIN=1
    fi
    [ -n "$SESSION_SECRET" ] || SESSION_SECRET=$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')
}

deploy_function() {
    ensure_role "$FUNCTION" lambda.amazonaws.com
    aws iam delete-role-policy --role-name "$FUNCTION" --policy-name read-settings 2>/dev/null || true  # older name
    aws iam put-role-policy --role-name "$FUNCTION" --policy-name dashboard --policy-document "{
      \"Version\": \"2012-10-17\",
      \"Statement\": [
        {\"Sid\": \"ReadBackupsAndSnapshot\", \"Effect\": \"Allow\", \"Action\": \"s3:GetObject\",
         \"Resource\": [\"arn:aws:s3:::$BUCKET/dashboard/*\", \"arn:aws:s3:::$BUCKET/world/*\", \"arn:aws:s3:::$BUCKET/downloads/*\"]},
        {\"Sid\": \"ExpireBackups\", \"Effect\": \"Allow\", \"Action\": \"s3:DeleteObject\",
         \"Resource\": [\"arn:aws:s3:::$BUCKET/world/*\", \"arn:aws:s3:::$BUCKET/downloads/*\"]},
        {\"Sid\": \"ListBackups\", \"Effect\": \"Allow\", \"Action\": \"s3:ListBucket\", \"Resource\": \"arn:aws:s3:::$BUCKET\"},
        {\"Sid\": \"DescribeServer\", \"Effect\": \"Allow\",
         \"Action\": [\"ec2:DescribeInstances\", \"ec2:DescribeSecurityGroups\"], \"Resource\": \"*\"},
        {\"Sid\": \"StartStopGameServerOnly\", \"Effect\": \"Allow\", \"Action\": [\"ec2:StartInstances\", \"ec2:StopInstances\"],
         \"Resource\": \"arn:aws:ec2:$REGION:$ACCOUNT:instance/*\", \"Condition\": {\"StringEquals\": {\"aws:ResourceTag/Name\": \"$NAME\"}}},
        {\"Sid\": \"BackupDocumentOnly\", \"Effect\": \"Allow\", \"Action\": \"ssm:SendCommand\",
         \"Resource\": \"arn:aws:ssm:$REGION:$ACCOUNT:document/$DOCUMENT\"},
        {\"Sid\": \"OnGameServerOnly\", \"Effect\": \"Allow\", \"Action\": \"ssm:SendCommand\",
         \"Resource\": \"arn:aws:ec2:$REGION:$ACCOUNT:instance/*\", \"Condition\": {\"StringEquals\": {\"ssm:resourceTag/Name\": \"$NAME\"}}},
        {\"Sid\": \"CommandResults\", \"Effect\": \"Allow\", \"Action\": [\"ssm:GetCommandInvocation\", \"ssm:DescribeInstanceInformation\"], \"Resource\": \"*\"},
        {\"Sid\": \"State\", \"Effect\": \"Allow\",
         \"Action\": [\"dynamodb:GetItem\", \"dynamodb:PutItem\", \"dynamodb:UpdateItem\", \"dynamodb:DeleteItem\", \"dynamodb:Query\"],
         \"Resource\": \"arn:aws:dynamodb:$REGION:$ACCOUNT:table/$TABLE\"},
        {\"Sid\": \"ReadUsageCharts\", \"Effect\": \"Allow\", \"Action\": \"cloudwatch:GetMetricData\", \"Resource\": \"*\"},
        {\"Sid\": \"ReadServerLogs\", \"Effect\": \"Allow\", \"Action\": \"logs:FilterLogEvents\",
         \"Resource\": \"arn:aws:logs:$REGION:$ACCOUNT:log-group:$LOG_GROUP:*\"},
        {\"Sid\": \"OwnLogs\", \"Effect\": \"Allow\", \"Action\": [\"logs:CreateLogStream\", \"logs:PutLogEvents\"],
         \"Resource\": \"arn:aws:logs:$REGION:$ACCOUNT:log-group:$LAMBDA_LOG_GROUP:*\"},
        {\"Sid\": \"ReadSchedules\", \"Effect\": \"Allow\", \"Action\": \"scheduler:GetSchedule\",
         \"Resource\": \"arn:aws:scheduler:$REGION:$ACCOUNT:schedule/$GROUP/*\"},
        {\"Sid\": \"ContinueAsync\", \"Effect\": \"Allow\", \"Action\": \"lambda:InvokeFunction\", \"Resource\": \"$FUNCTION_ARN\"}
      ]
    }"
    ROLE_ARN=$(aws iam get-role --role-name "$FUNCTION" --query Role.Arn --output text)

    local build
    build=$(mktemp -d)
    trap 'rm -rf "$build"' RETURN
    mkdir "$build/pkg"
    cp "$HERE"/backend/*.py "$build/pkg/"
    cp -r "$HERE/frontend" "$build/pkg/frontend"
    (cd "$build/pkg" && python3 - <<'EOF'
import os, zipfile
with zipfile.ZipFile("../dashboard.zip", "w", zipfile.ZIP_DEFLATED) as z:
    for root, _, files in os.walk("."):
        for f in files:
            if not f.endswith(".pyc"):
                z.write(os.path.join(root, f), os.path.relpath(os.path.join(root, f), "."))
EOF
    )
    load_credentials
    (umask 077; jq -n --arg u "$DASHBOARD_USERNAME" --arg p "$DASHBOARD_PASSWORD" --arg s "$SESSION_SECRET" '{Variables: {
        DASHBOARD_USERNAME: $u, DASHBOARD_PASSWORD: $p, DASHBOARD_SESSION_SECRET: $s,
        BUCKET: env.BUCKET, BUCKET_REGION: env.BUCKET_REGION, INSTANCE_NAME: env.NAME, TABLE: env.TABLE,
        LOG_GROUP: env.LOG_GROUP, SSM_DOCUMENT: env.DOCUMENT, SCHEDULE_GROUP: env.GROUP, FUNCTION_ARN: env.FUNCTION_ARN}}' \
        > "$build/env.json")
    local config=(--timeout 600 --memory-size 256 --environment "file://$build/env.json"
                  --logging-config "LogFormat=Text,LogGroup=$LAMBDA_LOG_GROUP")

    if aws lambda get-function --function-name "$FUNCTION" >/dev/null 2>&1; then
        log "Updating Lambda function $FUNCTION"
        aws lambda update-function-code --function-name "$FUNCTION" --zip-file "fileb://$build/dashboard.zip" >/dev/null
        aws lambda wait function-updated-v2 --function-name "$FUNCTION"
        aws lambda update-function-configuration --function-name "$FUNCTION" --role "$ROLE_ARN" "${config[@]}" >/dev/null
        aws lambda wait function-updated-v2 --function-name "$FUNCTION"
    else
        log "Creating Lambda function $FUNCTION"
        for attempt in 1 2 3 4 5 6; do  # a brand-new role takes a few seconds before Lambda can assume it
            if aws lambda create-function --function-name "$FUNCTION" --runtime python3.13 --handler handler.lambda_handler \
                --role "$ROLE_ARN" --zip-file "fileb://$build/dashboard.zip" --architectures arm64 "${config[@]}" \
                --description "Enshrouded server dashboard" --tags "Name=$FUNCTION" >/dev/null 2>"$build/err"; then
                break
            fi
            grep -q "cannot be assumed" "$build/err" && [ "$attempt" -lt 6 ] || { cat "$build/err" >&2; exit 1; }
            sleep 10
        done
        aws lambda wait function-active-v2 --function-name "$FUNCTION"
    fi
    # Async work (stop, backups) is idempotent; retries must not arrive long after the fact
    aws lambda put-function-event-invoke-config --function-name "$FUNCTION" \
        --maximum-retry-attempts 2 --maximum-event-age-in-seconds 1800 >/dev/null

    if ! aws lambda get-function-url-config --function-name "$FUNCTION" >/dev/null 2>&1; then
        log "Creating function URL (login is enforced by the function itself)"
        aws lambda create-function-url-config --function-name "$FUNCTION" --auth-type NONE >/dev/null
        aws lambda add-permission --function-name "$FUNCTION" --statement-id public-url \
            --action lambda:InvokeFunctionUrl --principal '*' --function-url-auth-type NONE >/dev/null
        aws lambda add-permission --function-name "$FUNCTION" --statement-id public-url-invoke \
            --action lambda:InvokeFunction --principal '*' --invoked-via-function-url >/dev/null
    fi
    local origin
    origin=$(print_url); origin="${origin%/}"
    aws lambda update-function-url-config --function-name "$FUNCTION" --cors "{
        \"AllowOrigins\": [\"$origin\"], \"AllowCredentials\": true, \"AllowMethods\": [\"GET\", \"POST\"],
        \"AllowHeaders\": [\"content-type\", \"x-csrf-token\"], \"MaxAge\": 600}" >/dev/null
}

# --- Schedules -------------------------------------------------------------------------------------
deploy_schedules() {
    ensure_role "$SCHEDULER_ROLE" scheduler.amazonaws.com "{\"StringEquals\":{\"aws:SourceAccount\":\"$ACCOUNT\"}}"
    aws iam put-role-policy --role-name "$SCHEDULER_ROLE" --policy-name invoke-dashboard --policy-document \
        "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":\"lambda:InvokeFunction\",\"Resource\":\"$FUNCTION_ARN\"}]}"
    local role_arn="arn:aws:iam::$ACCOUNT:role/$SCHEDULER_ROLE"
    aws scheduler get-schedule-group --name "$GROUP" >/dev/null 2>&1 || aws scheduler create-schedule-group --name "$GROUP" >/dev/null

    # key, expression, description. Existing schedules are left alone so on/off set on the
    # console survives a redeploy; delete one in the console (or use remove) to recreate it.
    while IFS='|' read -r key expr desc; do
        local name="enshrouded-$key"
        if aws scheduler get-schedule --group-name "$GROUP" --name "$name" >/dev/null 2>&1; then
            log "Schedule $name exists; leaving it as it is"
            continue
        fi
        local input="{\"task\":\"scheduled\",\"schedule\":\"$key\",\"scheduledTime\":\"<aws.scheduler.scheduled-time>\"}"
        local target
        target=$(jq -n --arg arn "$FUNCTION_ARN" --arg role "$role_arn" --arg input "$input" \
            '{Arn: $arn, RoleArn: $role, Input: $input, RetryPolicy: {MaximumRetryAttempts: 3, MaximumEventAgeInSeconds: 900}}')
        for attempt in 1 2 3 4 5 6; do  # a brand-new role takes a few seconds to be usable
            local err
            if err=$(aws scheduler create-schedule --group-name "$GROUP" --name "$name" --description "$desc" \
                --schedule-expression "$expr" --schedule-expression-timezone "$TIMEZONE" \
                --flexible-time-window Mode=OFF --state ENABLED --target "$target" 2>&1 >/dev/null); then
                break
            fi
            # Only a not-yet-usable new role is worth retrying
            if [ "$attempt" -eq 6 ] || ! grep -qi "role" <<<"$err"; then echo "$err" >&2; exit 1; fi
            sleep 10
        done
        log "Created schedule $name: $expr ($TIMEZONE)"
    done <<EOF
stop|cron(0 0 * * ? *)|Stop the game server (backup first) at midnight
backup|cron(30 * * * ? *)|Hourly world backup while the server runs
EOF
}

print_url() { aws lambda get-function-url-config --function-name "$FUNCTION" --query FunctionUrl --output text; }

# --- Verify -----------------------------------------------------------------------------------------
verify() {
    local url fail=0
    url=$(print_url)
    check() { if eval "$2"; then echo "  ok    $1"; else echo "  FAIL  $1"; fail=1; fi; }
    echo "Function URL: $url"
    check "function URL auth is NONE, login enforced in code" "[ \"\$(aws lambda get-function-url-config --function-name $FUNCTION --query AuthType --output text)\" = NONE ]"
    check "CORS limited to the dashboard origin" "aws lambda get-function-url-config --function-name $FUNCTION --query 'Cors.AllowOrigins' --output text | grep -qx '${url%/}'"
    check "login configured (env var names present)" "aws lambda get-function-configuration --function-name $FUNCTION --query 'keys(Environment.Variables)' --output text | tr '\t' '\n' | grep -qx DASHBOARD_PASSWORD"
    for key in stop backup; do
        local s
        s=$(aws scheduler get-schedule --group-name "$GROUP" --name "enshrouded-$key" --output json 2>/dev/null || echo '{}')
        echo "Schedule enshrouded-$key: $(jq -r '"\(.State // "MISSING") \(.ScheduleExpression // "") \(.ScheduleExpressionTimezone // "")"' <<<"$s")"
        check "  targets $FUNCTION with the scheduler role" "[ \"\$(jq -r '.Target.Arn + \" \" + .Target.RoleArn' <<<'$s')\" = '$FUNCTION_ARN arn:aws:iam::$ACCOUNT:role/$SCHEDULER_ROLE' ]"
        check "  passes the scheduled time" "jq -r .Target.Input <<<'$s' | grep -q 'aws.scheduler.scheduled-time'"
    done
    check "scheduler role may invoke the function" "aws iam get-role-policy --role-name $SCHEDULER_ROLE --policy-name invoke-dashboard --query PolicyDocument --output json | grep -q '$FUNCTION_ARN'"
    check "DynamoDB table active with TTL" "[ \"\$(aws dynamodb describe-table --table-name $TABLE --query Table.TableStatus --output text)\" = ACTIVE ] && [ \"\$(aws dynamodb describe-time-to-live --table-name $TABLE --query TimeToLiveDescription.TimeToLiveStatus --output text)\" = ENABLED ]"
    check "SSM document exists" "aws ssm describe-document --name $DOCUMENT >/dev/null"
    if server_running; then
        local id
        id=$(aws ec2 describe-instances --filters Name=tag:Name,Values="$NAME" Name=instance-state-name,Values=running --query 'Reservations[0].Instances[0].InstanceId' --output text)
        check "instance $id is online in SSM" "[ \"\$(aws ssm describe-instance-information --filters Key=InstanceIds,Values=$id --query 'InstanceInformationList[0].PingStatus' --output text)\" = Online ]"
        check "instance ships logs (stream 'game' exists)" "aws logs describe-log-streams --log-group-name $LOG_GROUP --log-stream-name-prefix game --query 'logStreams[0].logStreamName' --output text | grep -qx game"
        check "instance publishes memory usage ($METRICS_NAMESPACE/mem_used_percent)" "aws cloudwatch list-metrics --namespace $METRICS_NAMESPACE --metric-name mem_used_percent --dimensions Name=InstanceId,Value=$id --query 'length(Metrics)' --output text | grep -qx 1"
    fi
    for g in "$LOG_GROUP" "$LAMBDA_LOG_GROUP"; do
        check "log retention 3 days on $g" "[ \"\$(aws logs describe-log-groups --log-group-name-prefix $g --query 'logGroups[0].retentionInDays' --output text)\" = 3 ]"
    done
    for ep in "GET api/status" "GET api/logs" "GET api/backups" "GET api/settings" "POST api/server/stop" "POST api/server/start" "POST api/backups" "POST api/schedules/stop/skip"; do
        set -- $ep
        check "unauthenticated $1 /$2 is refused (401)" "[ \"\$(curl -s -o /dev/null -w '%{http_code}' -X $1 -H 'content-type: application/json' -d '{\"confirm\":\"stop\",\"enabled\":false}' '${url}$2')\" = 401 ]"
    done
    return $fail
}

# --- Remove -------------------------------------------------------------------------------------------
remove_all() {
    for key in stop start backup; do  # start: removed 2026-10-15, deleted here if an old deploy left it
        aws scheduler delete-schedule --group-name "$GROUP" --name "enshrouded-$key" 2>/dev/null && log "Deleted schedule enshrouded-$key" || true
    done
    aws scheduler delete-schedule-group --name "$GROUP" 2>/dev/null || true
    if aws lambda get-function --function-name "$FUNCTION" >/dev/null 2>&1; then
        log "Deleting Lambda function $FUNCTION (and its URL)"
        aws lambda delete-function --function-name "$FUNCTION"
    fi
    for role in "$FUNCTION" "$SCHEDULER_ROLE"; do
        if aws iam get-role --role-name "$role" >/dev/null 2>&1; then
            for p in $(aws iam list-role-policies --role-name "$role" --query PolicyNames --output text); do
                aws iam delete-role-policy --role-name "$role" --policy-name "$p"
            done
            aws iam delete-role --role-name "$role" && log "Deleted IAM role $role"
        fi
    done
    for p in dashboard-agent dashboard-snapshot; do
        aws iam delete-role-policy --role-name "$SERVER_ROLE" --policy-name "$p" 2>/dev/null && log "Removed $p from $SERVER_ROLE (its backup policy stays)" || true
    done
    aws dynamodb delete-table --table-name "$TABLE" >/dev/null 2>&1 && log "Deleted DynamoDB table $TABLE" || true
    aws ssm delete-document --name "$DOCUMENT" 2>/dev/null && log "Deleted SSM document $DOCUMENT" || true
    for g in "$LOG_GROUP" "$LAMBDA_LOG_GROUP"; do
        aws logs delete-log-group --log-group-name "$g" 2>/dev/null && log "Deleted log group $g" || true
    done
    if server_running; then
        log "Removing the agent from the server and re-enabling the 6-hourly backup timer"
        ssh_server 'sudo systemctl disable --now enshrouded-inspect.timer >/dev/null 2>&1 || true
            sudo systemctl disable enshrouded-autostart.service >/dev/null 2>&1 || true
            sudo rm -f /etc/systemd/system/enshrouded-inspect.service /etc/systemd/system/enshrouded-inspect.timer \
                /etc/systemd/system/enshrouded-autostart.service /usr/local/bin/enshrouded-inspect /usr/local/bin/enshrouded-dashboard-backup
            sudo rm -rf /var/lib/enshrouded-dashboard
            sudo /opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl -a stop >/dev/null 2>&1 || true
            sudo dpkg -r amazon-cloudwatch-agent >/dev/null 2>&1 || true
            sudo systemctl daemon-reload
            sudo systemctl enable --now enshrouded-backup.timer >/dev/null
            sudo docker start enshrouded >/dev/null'
    else
        log "WARNING: server not running; run this again while it runs to remove the agent and re-enable the backup timer."
    fi
    log "Deleting s3://$BUCKET/dashboard/ and downloads/ (world/ backups are kept)"
    aws s3 rm "s3://$BUCKET/dashboard/" --recursive --only-show-errors
    aws s3 rm "s3://$BUCKET/downloads/" --recursive --only-show-errors
    log "Dashboard removed."
}

export BUCKET BUCKET_REGION NAME TABLE LOG_GROUP DOCUMENT GROUP FUNCTION_ARN
case "$ACTION" in
    deploy)
        preflight
        deploy_shared
        install_agent
        deploy_function
        deploy_schedules
        echo
        echo "Dashboard: $(print_url)"
        if [ -n "${GENERATED_LOGIN:-}" ]; then
            echo "Login:     $DASHBOARD_USERNAME / $DASHBOARD_PASSWORD   (saved in $SECRETS)"
        else
            echo "Login:     unchanged (see $SECRETS or set DASHBOARD_USERNAME/DASHBOARD_PASSWORD)"
        fi
        bucket_arg=""; [ "$BUCKET" != "enshrouded-backups-$ACCOUNT-$REGION" ] && bucket_arg="BUCKET=$BUCKET "
        echo "Check it:  PROFILE=$PROFILE REGION=$REGION $bucket_arg$0 verify"
        ;;
    agent) install_agent ;;
    verify) verify ;;
    url) print_url ;;
    remove) remove_all ;;
    *) echo "usage: $0 [deploy|agent|verify|url|remove]" >&2; exit 2 ;;
esac
