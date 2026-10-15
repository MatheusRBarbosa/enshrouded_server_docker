#!/usr/bin/env python3
"""Back up the Enshrouded world to S3. Run by the dashboard through the fixed SSM document
enshrouded-dashboard-backup; installed as /usr/local/bin/enshrouded-dashboard-backup.

    enshrouded-dashboard-backup --id 20261016T030000Z-shutdown --type shutdown --stop-server yes

1. --stop-server yes: stop the game container first (used right before the machine is stopped).
2. Copy savegame/ to a temp dir. If any file changed while copying (the server saves every few
   minutes), wait and copy again, so the archive is a consistent set of files.
3. Upload world/<id>.tar.gz (savegame/ + enshrouded_server.json, for disaster recovery) and
   downloads/<id>.tar.gz (savegame/ only, what players may download), each with its SHA-256
   as object metadata. The dashboard compares that against S3 before calling it a success.
4. Print one JSON line with the result (the dashboard reads it from the command output).
"""
import argparse
import datetime
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time

ENV_FILE = "/etc/enshrouded-backup.env"
LOCK_FILE = "/run/enshrouded-dashboard-backup.lock"
LOG_FILE = "/var/log/enshrouded-dashboard-backup.log"
AWS = "/snap/bin/aws"
CONTAINER = "enshrouded"
ID_PATTERN = re.compile(r"^\d{8}T\d{6}Z-(automatic|manual|shutdown)$")
COPY_ATTEMPTS = 6
COPY_RETRY_SECONDS = 5


def log(msg):
    line = f"{datetime.datetime.now(datetime.timezone.utc):%Y-%m-%dT%H:%M:%SZ} {msg}"
    print(line, file=sys.stderr, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def read_env(path):
    env = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k] = v.strip().strip('"')
    return env


def fingerprint(root):
    """{relative path: (size, mtime_ns)} for every file under root."""
    out = {}
    for dirpath, _, files in os.walk(root):
        for name in files:
            path = os.path.join(dirpath, name)
            st = os.stat(path)
            out[os.path.relpath(path, root)] = (st.st_size, st.st_mtime_ns)
    return out


def consistent_copy(src, dst, attempts=COPY_ATTEMPTS, wait=COPY_RETRY_SECONDS, sleep=time.sleep):
    """Copy src to dst until a copy completes with no file changing meanwhile. Returns attempts used."""
    for attempt in range(1, attempts + 1):
        before = fingerprint(src)
        if os.path.exists(dst):
            shutil.rmtree(dst)
        shutil.copytree(src, dst)
        after = fingerprint(src)
        copied = {k: v[0] for k, v in fingerprint(dst).items()}
        if before == after and copied == {k: v[0] for k, v in after.items()}:
            return attempt
        log(f"save files changed while copying (attempt {attempt}); retrying in {wait}s")
        sleep(wait)
    raise RuntimeError("save files kept changing; no consistent copy")


def make_tar(path, root, members):
    with tarfile.open(path, "w:gz") as tar:
        for m in members:
            tar.add(os.path.join(root, m), arcname=m)
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return os.path.getsize(path), h.hexdigest()


def upload(env, path, key, sha256, backup_id, backup_type):
    subprocess.run([AWS, "s3", "cp", "--region", env["BUCKET_REGION"], "--only-show-errors",
                    "--metadata", f"sha256={sha256},backup-id={backup_id},type={backup_type}",
                    path, f"s3://{env['BUCKET']}/{key}"], check=True, timeout=300)


def container_running():
    out = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", CONTAINER], capture_output=True, text=True)
    return out.returncode == 0 and out.stdout.strip() == "true"


def backup(backup_id, backup_type, stop_server):
    env = read_env(ENV_FILE)
    data = env.get("DATA_DIR", "/srv/enshrouded")
    stopped = False
    if stop_server and container_running():
        log("stopping the game container")
        subprocess.run(["docker", "stop", "--time", "90", CONTAINER], check=True, capture_output=True, timeout=150)
        stopped = True
    if not os.path.isdir(os.path.join(data, "savegame")):
        raise RuntimeError(f"no savegame directory in {data}")

    with tempfile.TemporaryDirectory(prefix="enshrouded-backup-") as tmp:
        stage = os.path.join(tmp, "stage")
        os.makedirs(stage)
        attempts = consistent_copy(os.path.join(data, "savegame"), os.path.join(stage, "savegame"))
        members = ["savegame"]
        config = os.path.join(data, "enshrouded_server.json")
        if os.path.exists(config):
            shutil.copy2(config, os.path.join(stage, "enshrouded_server.json"))
            members.append("enshrouded_server.json")
        files = len(fingerprint(os.path.join(stage, "savegame")))

        full, dl = os.path.join(tmp, "full.tar.gz"), os.path.join(tmp, "download.tar.gz")
        size, sha = make_tar(full, stage, members)
        dsize, dsha = make_tar(dl, stage, ["savegame"])
        key, dkey = f"world/{backup_id}.tar.gz", f"downloads/{backup_id}.tar.gz"
        upload(env, full, key, sha, backup_id, backup_type)
        upload(env, dl, dkey, dsha, backup_id, backup_type)
    log(f"uploaded {key} ({size} bytes, {files} save files, copy attempt {attempts})")
    return {"ok": True, "id": backup_id, "key": key, "size": size, "sha256": sha, "downloadKey": dkey,
            "downloadSize": dsize, "downloadSha256": dsha, "files": files, "attempts": attempts, "serverStopped": stopped}


def main(argv):
    p = argparse.ArgumentParser()
    p.add_argument("--id", required=True)
    p.add_argument("--type", required=True, choices=["automatic", "manual", "shutdown"])
    p.add_argument("--stop-server", choices=["yes", "no"], default="no")
    args = p.parse_args(argv)
    if not ID_PATTERN.match(args.id) or not args.id.endswith("-" + args.type):
        print(json.dumps({"ok": False, "error": "invalid backup id"}))
        return 2
    lock = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print(json.dumps({"ok": False, "error": "another backup is running on the server"}))
        return 3
    log(f"backup {args.id} started (stop server: {args.stop_server})")
    try:
        result = backup(args.id, args.type, args.stop_server == "yes")
    except Exception as e:  # noqa: BLE001 - reported to the dashboard
        log(f"backup {args.id} FAILED: {e}")
        print(json.dumps({"ok": False, "error": str(e)[:300]}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
