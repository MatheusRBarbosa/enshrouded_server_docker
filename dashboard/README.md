# Server dashboard

A web page for the friends who share the server. After logging in, they can:
- see whether the server is up and ready for players
- start or stop it
- skip the nightly automatic stop
- take, list and download backups
- read the game logs
- check the settings the server is actually running with

Everything runs on AWS Lambda plus pay-per-use services, so there is nothing always-on beyond the game server itself.

```
Browser ──HTTPS──► Lambda function URL (enshrouded-dashboard)
                     │  login, API, page
                     ├─ EC2: describe / start / stop (only the instance tagged Name=enshrouded)
                     ├─ SSM Run Command: fixed document enshrouded-dashboard-backup ─► instance
                     ├─ DynamoDB enshrouded-dashboard: sessions, throttling, skips, runs, backups
                     ├─ S3 <backup bucket>: world/, downloads/, dashboard/snapshot.json
                     ├─ CloudWatch Logs /enshrouded/server (read only)
                     ├─ CloudWatch metrics: CPU (AWS/EC2) and memory (Enshrouded), read only
                     └─ EventBridge Scheduler: read its own schedules (no changes)
EventBridge Scheduler (group enshrouded) ──► same Lambda: stop 00:00, backup hourly :30
Instance: enshrouded-inspect timer (settings snapshot), CloudWatch agent (logs, memory),
          enshrouded-dashboard-backup (run by SSM), enshrouded-autostart.service
```

## Files

| Path | What |
|---|---|
| `backend/api.py` | HTTP routes, the login gate and background tasks |
| `backend/auth.py` | Signed session cookie, CSRF, login throttling |
| `backend/ops.py` | Start/stop and scheduled runs |
| `backend/backups.py` | Backup workflow, verification, history, retention (newest 24) |
| `backend/schedules.py` | Cron and time zones, skips, run claims |
| `backend/logs.py` | Log retrieval and limits |
| `backend/metrics.py` | CPU and memory history for the Overview charts, cached for 2 minutes |
| `backend/store.py` | DynamoDB access, plus an in-memory twin used by tests and the dev server |
| `backend/cloud.py` | Every AWS call |
| `backend/settings.py`, `catalog.py`, `sanitize.py`, `runtime.py` | Server Settings: normalization, labels, redaction, live facts |
| `frontend/` | Static page with no build step. Tabs: Overview, Schedule, Backups, Logs, Settings |
| `agent/` | Everything installed on the instance: settings inspector, backup script, unit files, CloudWatch agent config, SSM document |
| `iam/deployer-policy.json` | Extra permissions whoever runs `deploy.sh` needs (see Deploy) |
| `tests/` | `unittest` suite, stdlib only, with fakes for AWS |
| `dev_server.py` | The whole dashboard on localhost against the fakes |
| `deploy.sh` | `deploy`, `agent`, `verify`, `url`, `remove` |

## How each feature works

### Login

There is one shared username and password. They are stored as the Lambda environment variables `DASHBOARD_USERNAME` and `DASHBOARD_PASSWORD`, and `DASHBOARD_SESSION_SECRET` is generated once at deploy.

**Session**
- Logging in sets an HMAC-signed session cookie named `__Host-enshrouded_session`, with `Secure; HttpOnly; SameSite=Strict`. It lasts 12 hours.
- The signing key is derived from the secret, the username and the password, so **changing the password logs everyone out**.
- Logging out also records the session as revoked, so a copied cookie stops working too.

**Protecting changes**
- Every POST must carry the session's CSRF token in `X-CSRF-Token`.
- If the browser sends an `Origin` header, it must be the dashboard's own origin; a cross-site `Sec-Fetch-Site` is refused.
- The function URL's CORS setting allows only that origin.

**Login throttling:** 5 failed logins per IP, or 30 in total, within 15 minutes block further logins until the window ends.

**What anyone can reach without logging in:** the static page files, `GET /api/session` (which only answers "logged in or not") and `POST /api/login`. Everything else returns 401 without a valid session.

**Why there is no IAM auth:** the function URL itself is `AuthType NONE`, because a browser can't sign IAM requests. The function enforces the login on every request.

### Start and stop

**Stop** (from the button, which asks for confirmation, or from the schedule):
1. Stops the game container.
2. Backs up the stopped world, which is the most consistent copy possible.
3. Stops the instance. It does this **even if the backup failed**, so a scheduled stop can't leave the machine running all night. A failed backup is recorded and shown.

**Start** is manual only; there is no scheduled start. It only starts the instance. `enshrouded-autostart.service` starts the container at boot, which also updates the game. The page polls until the machine is running and then until the game is **Ready**.

**What "Ready" means:** the machine is running, the instance agent has reported since this boot, the game process is up, and its log shows `HostOnline`. A running EC2 instance alone is shown as "Starting".

**Rules:**
- Only one start or stop runs at a time.
- Manual start and stop never change the recurring schedules.

### Schedule

The schedules are EventBridge Scheduler schedules in the `enshrouded` group:
- `enshrouded-stop`: `cron(0 0 * * ? *)`
- `enshrouded-backup`: `cron(30 * * * ? *)`

There is no automatic start: after the midnight stop, the server stays off until someone presses Start. Both schedules use the time zone `America/Sao_Paulo`. Each invokes the Lambda through the `enshrouded-scheduler` role, passing `<aws.scheduler.scheduled-time>` as the run's time.

**What the Schedule tab shows:** the configuration read live from AWS (expression, time zone, on/off, next run, whether the target and role are right) and recent runs.

**Controls:** only "Skip next" for the stop. The page can't turn a schedule off or change it; its role may only read the schedules. A schedule turned off in the AWS console shows as off.

**How skips stay correct:**
- **Tied to one run:** a skip belongs to one run, identified by its UTC time. It is used up by a conditional write only when the run for that exact time arrives.
- **Retries:** a retry of the same run sees "consumed" and skips again, and no other run can use it up.
- **Duplicate deliveries:** each run is claimed in DynamoDB, so a duplicate does nothing.
- **Failures:** a run that failed or crashed can be retried once.

**Redeploys:** `deploy.sh` creates missing schedules but leaves existing ones alone. To change one, delete it in the console (or run `remove`) and redeploy.

### Backups

**What runs:**
- **Hourly** (at :30) while the machine runs. While it's stopped, the run is recorded as *skipped*; the machine is never started just for a backup.
- **Before every stop.**
- **On demand** with "Create backup now".

**How a backup is taken:** the Lambda runs the fixed SSM document `enshrouded-dashboard-backup`, whose only parameters are a validated id, the type, and a yes/no for stopping the server. On the instance, the script:
1. Copies `savegame/` and checks that no file changed during the copy; if one did, it copies again, so the archive is a consistent set of files.
2. Uploads `world/<id>.tar.gz` (save files plus config, for disaster recovery; `enshrouded-restore` still picks the newest one).
3. Uploads `downloads/<id>.tar.gz` (save files only, so the download never contains the server passwords).
4. Records each file's SHA-256 checksum on the uploaded object.

**When it counts as successful:** only after S3 confirms both objects with the expected size and checksum.

**Concurrency:** only one backup runs at a time, enforced by a lock in DynamoDB and another on the instance.

**History:** the Backups tab lists backups 15 per page, with type, status, size and reason. Downloads are 5-minute presigned links, handed out only to logged-in users.

**Retention:**
- **By count, not by age:** after each backup, the newest **24 successful backups** are kept, however old they are. A weekend or a month without play never thins them out.
- **The cutoff:** once there are more than 24, everything older than the 24th-newest successful backup is deleted: older backups with their files, failed and skipped runs, and files with no record (for example old backups made with the command-line script). With 24 or fewer, nothing is deleted.
- **Always kept:**
  - backups still in progress
  - the newest object in `world/`
- **Stuck backups:** a backup still in progress after 30 minutes is marked failed.
- **Nothing else deletes backups:** the bucket has no lifecycle rule.

**The old timer:** the 6-hourly `enshrouded-backup.timer` from `deploy/aws/bootstrap.sh` is turned off when the agent is installed and turned back on by `remove`. Its service still exists, because `deploy/aws/apply-config.sh` uses it.

### Logs

The CloudWatch agent on the instance ships three streams to `/enshrouded/server`, kept for 3 days (CloudWatch's shortest option of at least 48 h; there's no 2-day setting):

| Stream | Source |
|---|---|
| `game` | `logs/enshrouded_server.log`, with `[I]/[W]/[E]` levels |
| `container` | Docker stdout: SteamCMD updates, startup and game output |
| `backup` | the backup script's log |

**What the page can ask for:** only a named source, a window of up to 24 h, a text filter of up to 100 characters (applied in the Lambda, never sent to CloudWatch as a query) and up to 500 lines. The Lambda's role can read only this log group.

**On the page:** the Logs tab refreshes every 15 s, and the Overview shows the last hour's `[E]` lines.

### CPU and memory charts

The Overview shows CPU and memory usage of the whole machine over the last 6 hours. Hovering shows the exact value, and gaps mean the server was off.
- **CPU:** EC2's built-in `CPUUtilization`, from free basic monitoring, with one point every 5 minutes.
- **Memory:** EC2 can't see inside the machine, so the CloudWatch agent publishes `mem_used_percent` every minute as the custom metric `Enshrouded/mem_used_percent` (dimension `InstanceId`). It's then also visible in the AWS console next to CPU.
- **Cost control:** CloudWatch bills every metric read, so the page refreshes the charts every 5 minutes, and the Lambda caches one result for 2 minutes that every viewer shares. The Lambda's role may only read metrics; the instance may only publish to the `Enshrouded` namespace.

### Server settings

This part is unchanged from the first version, and is now a secondary tab whose groups start collapsed. Values come from the instance's settings snapshot (`agent/enshrouded_inspect.py`, every 2 minutes) and live AWS and Steam facts.

**Statuses:**
- **Applied:** reported by the server itself.
- **Configured:** read from the config file.
- **Restart needed:** the file changed since the server started.
- **Live:** a runtime fact.
- **Unknown:** couldn't be determined.

**Passwords:** they never leave the instance; the page only shows whether each group has one.

**Restarts:** every setting in `enshrouded_server.json` needs a restart to change, except the ban list, which the server updates in game.

## Develop and test

```bash
python3 -m unittest discover -s dashboard/tests     # ~130 tests, stdlib only
python3 dashboard/dev_server.py                      # http://localhost:8000, login dev / dev, fake AWS
python3 dashboard/dev_server.py --state stopped
```

The dev server runs the real `api.App` with `tests/fakes.py` instead of AWS, so start, stop, backups, skips and logs all work against the fake.

After changing the settings inspector, regenerate its fixture with `python3 dashboard/tests/helpers.py`.

## Deploy

1. Attach `dashboard/iam/deployer-policy.json` to the IAM user that deploys, as an inline policy. It covers Scheduler, DynamoDB, SSM documents and CloudWatch Logs, all limited to `enshrouded-*` names. Lambda, EC2, S3 and IAM for `enshrouded-*` roles were already needed by `deploy/aws/launch.sh`.
2. Run the deploy and the checks:

   ```bash
   PROFILE=enshrouded REGION=sa-east-1 BUCKET=enshrouded-backups-<account>-us-east-2 ./dashboard/deploy.sh          # prints URL and login
   PROFILE=enshrouded REGION=sa-east-1 BUCKET=enshrouded-backups-<account>-us-east-2 ./dashboard/deploy.sh verify   # checks resources + 401s
   ```

   `REGION` is where the game server runs; the dashboard is deployed next to it. `BUCKET` is needed because the backups stayed in their us-east-2 bucket when the server moved to São Paulo.

The game server must already exist (`deploy/aws/launch.sh`).

**The login:**
- **Your own:** set `DASHBOARD_USERNAME` and `DASHBOARD_PASSWORD` in `dashboard/secrets.env` (git-ignored) or in the environment, then re-run `deploy.sh`.
- **Nothing set:** the values on the Lambda are kept. If there are none at all, a password is generated, saved there and printed once.
- **Changing the password** logs everyone out.

**Redeploying:** re-running `deploy.sh` is safe and updates code and permissions. `launch.sh` re-runs `deploy.sh agent` whenever it replaces the instance.

**What deploy creates or changes:**

| Resource | Notes |
|---|---|
| Lambda `enshrouded-dashboard` + function URL | python3.13 arm64, 256 MB, 600 s timeout. Async retries: 2, at most 30 min old |
| IAM role `enshrouded-dashboard`, inline `dashboard` | Least privilege; EC2 start/stop and SSM limited to the instance tagged `Name=enshrouded` |
| IAM role `enshrouded-scheduler`, inline `invoke-dashboard` | May only invoke the dashboard Lambda |
| Scheduler group `enshrouded` with 2 schedules | See Schedule |
| DynamoDB `enshrouded-dashboard` | On-demand, TTL on `ttl` |
| SSM document `enshrouded-dashboard-backup` | The only command the Lambda may run on the instance |
| Log groups `/enshrouded/server`, `/aws/lambda/enshrouded-dashboard` | 3-day retention |
| Inline policy `dashboard-agent` on `enshrouded-server` | S3 uploads to `dashboard/`, `downloads/`, `world/`; log shipping; memory metric (namespace `Enshrouded` only); SSM agent registration |
| On the instance | Agent files and units (see the diagram); CloudWatch agent; `enshrouded-backup.timer` disabled |

**Monthly cost:**
- **Added by the dashboard:** about **$0.30**, almost all CloudWatch Logs ingestion (~0.3 GB) plus S3 requests. Lambda, Scheduler, DynamoDB on-demand and SSM Run Command stay within free tiers or cost cents.
- **Charts:** the memory metric is 1 custom metric, inside CloudWatch's always-free 10; otherwise it would be $0.30/month, prorated to the hours the server runs. Reading the charts costs $0.01 per 1,000 metrics read; at 2 metrics every 5 minutes per viewer, that's cents a month.
- **Saved by the nightly stop:** the c8i.xlarge (about $0.29/h in São Paulo) and its public IPv4 address ($0.005/h) are billed only for the hours it runs, and it's off from midnight until someone starts it. The disk costs the same either way. There's no Elastic IP, so nothing is charged for an idle address.

## Remove

```bash
PROFILE=enshrouded REGION=sa-east-1 BUCKET=enshrouded-backups-<account>-us-east-2 ./dashboard/deploy.sh remove
```

**Deleted:**
- the schedules and their group
- the Lambda and its URL
- both dashboard roles, and the `dashboard-agent` policy on the instance role
- the table, the SSM document and the log groups
- `dashboard/` and `downloads/` in S3

**Undone on the instance:**
- the agent, the CloudWatch agent and the autostart unit are removed
- `enshrouded-backup.timer` is turned back on

**Kept:** the instance, the container, the security group, the bucket and every `world/` backup.
