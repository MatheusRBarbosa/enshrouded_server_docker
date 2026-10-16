#!/usr/bin/env bash
# Create (or reuse) everything needed to run the Enshrouded server on EC2, then build
# and start the container. Safe to re-run: existing resources are looked up by name/tag.
#
#   PROFILE=enshrouded REGION=us-east-2 ./deploy/aws/launch.sh
#
# Optional env:
#   BUCKET         backup bucket (default enshrouded-backups-<account>-<region>); point it at an
#                  existing bucket in another region to move the world between regions
#   INSTANCE_TYPE  default m6a.xlarge
#   ADMIN_PW, FRIEND_PW, GUEST_PW, VISITOR_PW (or in git-ignored deploy/aws/secrets.env)
#                  passwords for a fresh world, which is seeded from deploy/aws/enshrouded_server.json
#                  (passwords are blank in git). Unset ones get a random password, printed once.
#                  Ignored if a config already exists or was restored from a backup.
set -euo pipefail

PROFILE="${PROFILE:?set PROFILE}"
REGION="${REGION:?set REGION}"
INSTANCE_TYPE="${INSTANCE_TYPE:-m6a.xlarge}"
NAME=enshrouded
ROLE=enshrouded-server
KEY_FILE="$HOME/.ssh/enshrouded.pem"
KNOWN_HOSTS="$HOME/.ssh/known_hosts_enshrouded"
REPO_DIR="$(cd "$(dirname "$0")/../.." && pwd)"

# Local, git-ignored passwords (environment variables take precedence)
SECRETS="$REPO_DIR/deploy/aws/secrets.env"
if [ -f "$SECRETS" ]; then
    for var in ADMIN_PW FRIEND_PW GUEST_PW VISITOR_PW; do
        [ -z "${!var:-}" ] && eval "$(grep -E "^$var=" "$SECRETS" | tail -n1)"
    done
fi

aws() { command aws --profile "$PROFILE" --region "$REGION" "$@"; }
log() { echo ">>> $*" >&2; }

ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
BUCKET="${BUCKET:-enshrouded-backups-$ACCOUNT-$REGION}"

# --- S3 backup bucket -------------------------------------------------------
# get-bucket-location works from any region's endpoint, so it doubles as the existence check
if BUCKET_REGION=$(aws s3api get-bucket-location --bucket "$BUCKET" --query 'LocationConstraint' --output text 2>/dev/null); then
    [ "$BUCKET_REGION" = "None" ] && BUCKET_REGION=us-east-1
    log "Using existing bucket $BUCKET ($BUCKET_REGION)"
else
    BUCKET_REGION="$REGION"
    log "Creating bucket $BUCKET"
    if [ "$REGION" = "us-east-1" ]; then
        aws s3api create-bucket --bucket "$BUCKET" >/dev/null
    else
        aws s3api create-bucket --bucket "$BUCKET" \
            --create-bucket-configuration "LocationConstraint=$REGION" >/dev/null
    fi
    aws s3api put-public-access-block --bucket "$BUCKET" --public-access-block-configuration \
        BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
    aws s3api put-bucket-encryption --bucket "$BUCKET" --server-side-encryption-configuration \
        '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
fi

# --- IAM role so the instance can write/read backups --------------------------
if ! aws iam get-role --role-name "$ROLE" >/dev/null 2>&1; then
    log "Creating IAM role $ROLE"
    aws iam create-role --role-name "$ROLE" --assume-role-policy-document \
        '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}' >/dev/null
fi
aws iam put-role-policy --role-name "$ROLE" --policy-name backup-bucket --policy-document "{
  \"Version\": \"2012-10-17\",
  \"Statement\": [
    {\"Effect\": \"Allow\", \"Action\": [\"s3:PutObject\", \"s3:GetObject\"], \"Resource\": \"arn:aws:s3:::$BUCKET/world/*\"},
    {\"Effect\": \"Allow\", \"Action\": \"s3:ListBucket\", \"Resource\": \"arn:aws:s3:::$BUCKET\"}
  ]
}"
if ! aws iam get-instance-profile --instance-profile-name "$ROLE" >/dev/null 2>&1; then
    log "Creating instance profile $ROLE"
    aws iam create-instance-profile --instance-profile-name "$ROLE" >/dev/null
    aws iam add-role-to-instance-profile --instance-profile-name "$ROLE" --role-name "$ROLE"
fi

# --- SSH key pair -------------------------------------------------------------
if ! aws ec2 describe-key-pairs --key-names "$NAME" >/dev/null 2>&1; then
    [ -e "$KEY_FILE" ] && { echo "ERROR: $KEY_FILE exists but key pair '$NAME' is missing in $REGION; move the file away first." >&2; exit 1; }
    log "Creating key pair $NAME -> $KEY_FILE"
    mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh"
    aws ec2 create-key-pair --key-name "$NAME" --key-type ed25519 --query KeyMaterial --output text > "$KEY_FILE"
    chmod 600 "$KEY_FILE"
fi

# --- Security group -------------------------------------------------------------
VPC=$(aws ec2 describe-vpcs --filters Name=isDefault,Values=true --query 'Vpcs[0].VpcId' --output text)
SG=$(aws ec2 describe-security-groups --filters Name=group-name,Values="$NAME" Name=vpc-id,Values="$VPC" \
    --query 'SecurityGroups[0].GroupId' --output text)
if [ "$SG" = "None" ]; then
    log "Creating security group $NAME"
    SG=$(aws ec2 create-security-group --group-name "$NAME" --description "Enshrouded server" \
        --vpc-id "$VPC" --query GroupId --output text)
fi
MY_IP=$(curl -fsS https://checkip.amazonaws.com)
for perm in \
    'IpProtocol=udp,FromPort=15637,ToPort=15637,IpRanges=[{CidrIp=0.0.0.0/0,Description=enshrouded}]' \
    "IpProtocol=tcp,FromPort=22,ToPort=22,IpRanges=[{CidrIp=$MY_IP/32,Description=ssh}]"; do
    aws ec2 authorize-security-group-ingress --group-id "$SG" --ip-permissions "$perm" >/dev/null 2>&1 || true
done

# --- Instance -------------------------------------------------------------------
INSTANCE=$(aws ec2 describe-instances \
    --filters Name=tag:Name,Values="$NAME" Name=instance-state-name,Values=pending,running,stopping,stopped \
    --query 'Reservations[0].Instances[0].InstanceId' --output text)
if [ "$INSTANCE" = "None" ]; then
    AMI=$(aws ec2 describe-images --owners 099720109477 \
        --filters 'Name=name,Values=ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*' \
        --query 'sort_by(Images,&CreationDate)[-1].ImageId' --output text)
    USERDATA=$(mktemp)
    sed -e "s/__BUCKET__/$BUCKET/" -e "s/__BUCKET_REGION__/$BUCKET_REGION/" \
        "$REPO_DIR/deploy/aws/bootstrap.sh" > "$USERDATA"
    log "Launching $INSTANCE_TYPE from $AMI"
    # A freshly created instance profile takes a few seconds to become usable
    for attempt in 1 2 3 4 5 6; do
        if INSTANCE=$(aws ec2 run-instances --image-id "$AMI" --instance-type "$INSTANCE_TYPE" \
            --key-name "$NAME" --security-group-ids "$SG" \
            --iam-instance-profile "Name=$ROLE" \
            --disable-api-termination \
            --block-device-mappings 'DeviceName=/dev/sda1,Ebs={VolumeSize=30,VolumeType=gp3,DeleteOnTermination=true}' \
            --user-data "file://$USERDATA" \
            --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$NAME}]" \
                                 "ResourceType=volume,Tags=[{Key=Name,Value=$NAME}]" \
            --query 'Instances[0].InstanceId' --output text); then
            break
        fi
        [ "$attempt" -eq 6 ] && exit 1
        log "run-instances failed (attempt $attempt), retrying in 10s..."
        sleep 10
    done
    rm -f "$USERDATA"
elif [ "$(aws ec2 describe-instances --instance-ids "$INSTANCE" --query 'Reservations[0].Instances[0].State.Name' --output text)" != "running" ]; then
    log "Starting existing instance $INSTANCE"
    aws ec2 start-instances --instance-ids "$INSTANCE" >/dev/null
fi
log "Waiting for $INSTANCE to run"
aws ec2 wait instance-running --instance-ids "$INSTANCE"
# No Elastic IP: the default subnet gives the instance a public IP that changes on every start
IP=$(aws ec2 describe-instances --instance-ids "$INSTANCE" --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)
[ "$IP" = "None" ] && { echo "ERROR: $INSTANCE has no public IP; its subnet must auto-assign one." >&2; exit 1; }

# --- Deploy the container ---------------------------------------------------------
# Host keys are stored under the instance ID, since the IP changes and gets reused by other instances
SSH=(ssh -i "$KEY_FILE" -o UserKnownHostsFile="$KNOWN_HOSTS" -o HostKeyAlias="$INSTANCE"
     -o StrictHostKeyChecking=accept-new -o ConnectTimeout=5)
log "Waiting for SSH on $IP"
until "${SSH[@]}" -o BatchMode=yes "ubuntu@$IP" true 2>/dev/null; do sleep 5; done
log "Waiting for bootstrap (cloud-init) to finish"
"${SSH[@]}" "ubuntu@$IP" 'cloud-init status --wait >/dev/null; cloud-init status --long | grep -q "status: done"' \
    || { echo "ERROR: bootstrap failed; see /var/log/cloud-init-output.log on the instance." >&2; exit 1; }

log "Restoring the latest backup if the world is empty"
"${SSH[@]}" "ubuntu@$IP" 'sudo enshrouded-restore'

if ! "${SSH[@]}" "ubuntu@$IP" 'test -f /srv/enshrouded/enshrouded_server.json'; then
    random_pw() { head -c 256 /dev/urandom | tr -dc 'A-Za-z0-9' | head -c 12; }
    ADMIN_PW="${ADMIN_PW:-$(random_pw)}" FRIEND_PW="${FRIEND_PW:-$(random_pw)}"
    GUEST_PW="${GUEST_PW:-$(random_pw)}" VISITOR_PW="${VISITOR_PW:-$(random_pw)}"
    log "Seeding enshrouded_server.json from deploy/aws/enshrouded_server.json"
    log "Passwords: Admin=$ADMIN_PW Friend=$FRIEND_PW Guest=$GUEST_PW Visitor=$VISITOR_PW"
    jq --arg admin "$ADMIN_PW" --arg friend "$FRIEND_PW" --arg guest "$GUEST_PW" --arg visitor "$VISITOR_PW" \
       '.userGroups |= map(.password = ({Admin: $admin, Friend: $friend, Guest: $guest, Visitor: $visitor}[.name] // .password))' \
       "$REPO_DIR/deploy/aws/enshrouded_server.json" \
       | "${SSH[@]}" "ubuntu@$IP" 'sudo install -o 1000 -g 1000 -m 644 /dev/stdin /srv/enshrouded/enshrouded_server.json'
fi

log "Building image and (re)starting the container"
"${SSH[@]}" "ubuntu@$IP" 'mkdir -p ~/build'
scp -q -i "$KEY_FILE" -o UserKnownHostsFile="$KNOWN_HOSTS" -o HostKeyAlias="$INSTANCE" \
    "$REPO_DIR/Dockerfile" "$REPO_DIR/entrypoint.sh" "ubuntu@$IP:build/"
"${SSH[@]}" "ubuntu@$IP" 'set -e
    sudo docker build -q -t enshrouded:local ~/build
    sudo docker rm -f enshrouded >/dev/null 2>&1 || true
    sudo docker run -d --name enshrouded --restart unless-stopped --stop-timeout 90 \
        -p 15637:15637/udp -e PUID=1000 -e PGID=1000 \
        -v /srv/enshrouded:/home/steam/enshrouded enshrouded:local'

# A replaced instance needs the dashboard's inspection agent again (only if the dashboard is deployed)
if aws lambda get-function --function-name enshrouded-dashboard >/dev/null 2>&1; then
    log "Reinstalling the dashboard agent"
    PROFILE="$PROFILE" REGION="$REGION" BUCKET="$BUCKET" "$REPO_DIR/dashboard/deploy.sh" agent
fi

cat <<EOF

Done. Instance $INSTANCE ($INSTANCE_TYPE) in $REGION, address $IP:15637 (changes on every start)
Backups: s3://$BUCKET/world/ ($BUCKET_REGION), every 6h
Logs:    ssh -i $KEY_FILE -o UserKnownHostsFile=$KNOWN_HOSTS -o HostKeyAlias=$INSTANCE ubuntu@$IP 'sudo docker logs -f enshrouded'
EOF
