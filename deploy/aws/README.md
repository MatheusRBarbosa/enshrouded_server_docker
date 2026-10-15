# Running the server on AWS EC2

`launch.sh` creates (or reuses) everything and starts the server:

| Resource | Name / tag | Notes |
|---|---|---|
| EC2 instance | `Name=enshrouded` | Ubuntu 24.04, m6a.xlarge, 30 GB gp3, termination protection on |
| Elastic IP | `Name=enshrouded` | Same address across stop/start and instance replacement (per region) |
| Security group | `enshrouded` | UDP 15637 open; SSH only from the IP that ran `launch.sh` |
| Key pair | `enshrouded` | Private key in `~/.ssh/enshrouded.pem` |
| S3 bucket | `enshrouded-backups-<account>-<region>` | Private, encrypted, backups expire after 30 days |
| IAM role + instance profile | `enshrouded-server` | Instance may only read/write `world/*` in the bucket |

The image is built on the instance from this repo's `Dockerfile` and `entrypoint.sh`. World data lives in `/srv/enshrouded` on the instance.

## Launch / redeploy

```bash
PROFILE=enshrouded REGION=us-east-2 \
./deploy/aws/launch.sh
```

A brand-new world is seeded from `deploy/aws/enshrouded_server.json`. Passwords come from `deploy/aws/secrets.env`, which is git-ignored because the repo is public:

```bash
ADMIN_PW=...
FRIEND_PW=...
GUEST_PW=...
VISITOR_PW=...
```

Environment variables with the same names override it. Any password left unset gets a random value, printed once. Re-running the script rebuilds the image and restarts the container (this also updates the game).

Shortcuts used below:

```bash
A="aws --profile enshrouded --region us-east-2"
I=$($A ec2 describe-instances --filters Name=tag:Name,Values=enshrouded Name=instance-state-name,Values=pending,running,stopped --query 'Reservations[0].Instances[0].InstanceId' --output text)
S="ssh -i ~/.ssh/enshrouded.pem -o UserKnownHostsFile=~/.ssh/known_hosts_enshrouded ubuntu@<elastic-ip>"
```

## Gameplay settings

`deploy/aws/enshrouded_server.json` is the server config kept in git: the name and gameplay settings. Its passwords are blank on purpose, because the repo is public. The current settings use the `Custom` preset, so the server actually reads the `gameSettings` values. They copy the Hard values (enemy damage, boss health and attack frequency 1.5x, more enemies) and then add starvation off, crops and crafting 2x faster, more XP (combat and quests 1.5x), and faster gathering (loot and mining 1.5x).

To change settings, edit that file, commit it, then run:

```bash
PROFILE=enshrouded REGION=us-east-2 ./deploy/aws/apply-config.sh
```

It shows the diff, stops the server, takes a backup, writes the config and starts the server again. Passwords come from `secrets.env`, or are kept from the live server when missing there. The ban list is always kept from the live server. Valid ranges for each setting are in [docs/enshrouded_server.md](../../docs/enshrouded_server.md).

## Dashboard (start/stop, schedule, backups, logs)

Friends manage the server from a page with a shared login:
- start and stop it
- skip the nightly automatic stop (00:00 São Paulo time; there's no automatic start)
- take and download backups (hourly, kept 48 h)
- read the logs and see the live settings

Deploy it with `PROFILE=enshrouded REGION=us-east-2 ./dashboard/deploy.sh`. Remove it with `... ./dashboard/deploy.sh remove`, which leaves the game server and the `world/` backups alone. `launch.sh` reinstalls its agent when it replaces the instance.

While the dashboard is installed, it replaces the 6-hourly backup timer below with hourly backups. See [dashboard/README.md](../../dashboard/README.md).

## Backups

- Every 6 hours a systemd timer uploads `savegame/` + `enshrouded_server.json` to `s3://<bucket>/world/<UTC timestamp>.tar.gz`. Missed runs (instance stopped) run at next boot.
- Back up now: `$S sudo systemctl start enshrouded-backup.service`
- Status / next run: `$S systemctl list-timers enshrouded-backup.timer` and `$S journalctl -u enshrouded-backup`
- List backups: `$A s3 ls s3://<bucket>/world/`

## Stop / start (save money when nobody plays)

```bash
$S 'sudo docker stop enshrouded && sudo systemctl start enshrouded-backup.service'
$A ec2 stop-instances --instance-ids $I
$A ec2 start-instances --instance-ids $I   # container starts by itself, game updates on start
```

The server does not shut down gracefully yet (the entrypoint doesn't forward SIGTERM to Wine), so back up right after stopping the container, as above.

## Restore a specific backup

```bash
$S 'sudo docker stop enshrouded && sudo enshrouded-restore --force world/20261015T120000Z.tar.gz && sudo docker start enshrouded'
```

The previous `savegame/` is kept as `savegame.before-restore-<timestamp>`. Without a key, the newest backup is used. `--target DIR` extracts somewhere else (useful to inspect a backup).

## Disaster recovery (instance lost)

Re-run `launch.sh` with the same `PROFILE`/`REGION`. It reuses the bucket, Elastic IP and security group, launches a new instance, restores the newest backup into the empty volume and starts the server at the same address.

## Moving to another region

Elastic IPs are regional, so the address changes. Point the new region at the existing bucket:

```bash
$S 'sudo docker stop enshrouded && sudo systemctl start enshrouded-backup.service'   # final backup
PROFILE=enshrouded REGION=sa-east-1 BUCKET=enshrouded-backups-<account>-us-east-2 ./deploy/aws/launch.sh
```

Then check the world on the new server and tear down the old region. Both regions must be allowed for the account while the bucket stays in the old one.

## Teardown (deletes the world unless you keep the bucket)

```bash
$A ec2 modify-instance-attribute --instance-id $I --no-disable-api-termination
$A ec2 terminate-instances --instance-ids $I
$A ec2 release-address --allocation-id <eipalloc-...>
# optional: $A ec2 delete-security-group --group-name enshrouded ; $A ec2 delete-key-pair --key-name enshrouded
# optional, deletes all backups: $A s3 rb s3://<bucket> --force
```
