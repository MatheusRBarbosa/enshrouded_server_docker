#!/usr/bin/env python3
"""Inspect the Enshrouded server on this host and publish a sanitized snapshot to S3.

Installed on the game server as /usr/local/bin/enshrouded-inspect by dashboard/deploy.sh and run
as root every 2 minutes by enshrouded-inspect.timer. It reads local sources only:

  - enshrouded_server.json        what is configured
  - logs/enshrouded_server.log    what the running server reports it applied at startup
  - steamapps/appmanifest_*.acf   installed Steam build
  - the enshrouded_server.exe process and `docker inspect`

Passwords and banned account IDs never leave the host: they are reduced to "is set" / a count
before anything is written. The dashboard Lambda turns the snapshot into the settings page.

    enshrouded-inspect           build and upload the snapshot
    enshrouded-inspect --print   build and print it, upload nothing
"""
import datetime
import json
import os
import re
import struct
import subprocess
import sys
import tempfile

ENV_FILE = "/etc/enshrouded-backup.env"  # written by deploy/aws/bootstrap.sh; reused for the bucket
STATE_FILE = "/var/lib/enshrouded-dashboard/loaded-config.json"
AWS = "/snap/bin/aws"
CONTAINER = "enshrouded"
CONTAINER_DATA_DIR = "/home/steam/enshrouded"
APP_ID = "2278520"
SNAPSHOT_KEY = "dashboard/snapshot.json"
SCHEMA = 1
# The server rewrites its config file right after it starts, so an mtime this close to the
# process start still means "the file the server loaded".
LOADED_GRACE_SECONDS = 120
LOG_SCAN_LINES = 5000

SENSITIVE_KEY = re.compile(r"pass(word|wd)?|secret|token|credential|private_?key|api_?key", re.I)
SAFE_KEYS = {"passwordSet"}  # produced by sanitize_config: only says whether a password exists
LOG_LINE = re.compile(r"^\[[A-Z] [^\]]*\] ?(.*)$")
HEX_FLOAT = re.compile(r"^[0-9a-f]{8}$")


def iso(ts):
    if ts is None:
        return None
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_env(path):
    env = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                env[key] = value.strip().strip('"')
    return env


# --- Sanitizing ----------------------------------------------------------------------

def redact(value, path="", redacted=None):
    """Drop every key that looks like a secret, recursively. Returns (clean, redacted_paths)."""
    if redacted is None:
        redacted = []
    if isinstance(value, dict):
        clean = {}
        for key, item in value.items():
            here = f"{path}.{key}" if path else key
            if SENSITIVE_KEY.search(key) and key not in SAFE_KEYS:
                redacted.append(here)
                continue
            clean[key] = redact(item, here, redacted)[0]
        return clean, redacted
    if isinstance(value, list):
        return [redact(item, f"{path}[{i}]", redacted)[0] for i, item in enumerate(value)], redacted
    return value, redacted


def sanitize_config(config):
    """Replace group passwords with passwordSet and the ban list with a count, then redact."""
    config = json.loads(json.dumps(config))  # deep copy
    for group in config.get("userGroups") or []:
        if isinstance(group, dict) and "password" in group:
            group["passwordSet"] = bool(group.pop("password"))
    for key in ("bannedAccounts", "bans"):  # 1.0 renamed "bans" to "bannedAccounts"
        if isinstance(config.get(key), list):
            config[key + "Count"] = len(config.pop(key))
    return redact(config)


# --- Server log ------------------------------------------------------------------------

def decode_log_value(value):
    """The startup log prints floats as IEEE-754 hex ("3fc00000" = 1.5), 1.0 as "1" and
    durations as {"value": nanoseconds}."""
    if isinstance(value, dict) and set(value) == {"value"}:
        return value["value"]
    if isinstance(value, str):
        if HEX_FLOAT.match(value):
            return float(f"{struct.unpack('>f', bytes.fromhex(value))[0]:.6g}")
        try:
            number = float(value)
        except ValueError:
            return value
        return int(number) if number.is_integer() else number
    return value


def parse_log(lines):
    """Pick the facts the dashboard needs out of the server log's startup section.
    Player names and IDs are never extracted."""
    info = {}
    block, preset = None, None
    for raw in lines:
        m = LOG_LINE.match(raw.rstrip("\r\n"))
        if not m:
            continue
        msg = m.group(1)
        if block is not None:
            block.append(msg)
            if msg.strip() == "}":
                try:
                    info["gameSettings"] = {k: decode_log_value(v) for k, v in json.loads("\n".join(block)).items()}
                except ValueError:
                    info["gameSettingsParseError"] = True
                info["preset"] = preset
                block = None
            continue
        if (g := re.match(r"enshrouded_server.* - version (\S+)(?: \((\S+)\))?", msg)):
            info["version"], info["branch"] = g.group(1), g.group(2)
        elif (g := re.match(r"Game Version \(SVN\): (\d+)", msg)):
            info["svn"] = g.group(1)
        elif (g := re.match(r"Config Parsed: '(.+)'", msg)):
            info["configParsed"] = g.group(1)
        elif (g := re.match(r"Current UTC date: (\d{4}\.\d\d\.\d\d \d\d:\d\d:\d\d)", msg)):
            started = datetime.datetime.strptime(g.group(1), "%Y.%m.%d %H:%M:%S")
            info["startedAt"] = started.replace(tzinfo=datetime.timezone.utc).timestamp()
        elif (g := re.match(r"\[server\] Game Settings '(.+)'", msg)):
            preset, block = g.group(1), []
        elif msg.startswith("[online] Server connected to Steam successfully"):
            info["steamConnected"] = True
        elif (g := re.match(r"\[online\] Public ipv4: (\S+)", msg)):
            info["publicIp"] = g.group(1)
        elif "[Session] 'HostOnline' (up)!" in msg:
            info["hostOnline"] = True
    return info


def read_log(path):
    lines = []
    with open(path, errors="replace") as f:
        for i, line in enumerate(f):
            if i >= LOG_SCAN_LINES:
                break
            lines.append(line)
    return parse_log(lines)


# --- Steam manifest, process, container ----------------------------------------------------

def read_manifest(path):
    with open(path, errors="replace") as f:
        text = f.read()
    fields = dict(re.findall(r'^\s*"(buildid|LastUpdated|TargetBuildID|StateFlags)"\s+"([^"]*)"', text, re.M))
    last = fields.get("LastUpdated")
    return {
        "buildId": fields.get("buildid"),
        "targetBuildId": fields.get("TargetBuildID"),
        "lastUpdated": iso(int(last)) if last and last.isdigit() and int(last) > 0 else None,
        "fullyInstalled": fields.get("StateFlags") == "4",
    }


def server_process_start():
    """Start time (epoch seconds) of the oldest enshrouded_server.exe process, or None."""
    with open("/proc/stat") as f:
        btime = next(int(line.split()[1]) for line in f if line.startswith("btime "))
    ticks = os.sysconf("SC_CLK_TCK")
    starts = []
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                if b"enshrouded_server.exe" not in f.read():
                    continue
            with open(f"/proc/{pid}/stat") as f:
                fields = f.read().rsplit(")", 1)[1].split()
            starts.append(btime + int(fields[19]) / ticks)  # field 22: starttime
        except (OSError, IndexError, ValueError):
            continue
    return round(min(starts)) if starts else None


def container_info():
    out = subprocess.run(["docker", "inspect", CONTAINER], capture_output=True, text=True, timeout=20)
    if out.returncode != 0:
        return {"exists": False}
    c = json.loads(out.stdout)[0]
    state, host = c.get("State", {}), c.get("HostConfig", {})
    ports = []
    for port, bindings in sorted((host.get("PortBindings") or {}).items()):
        for b in bindings or []:
            ports.append({"container": port, "host": b.get("HostPort"), "hostIp": b.get("HostIp") or "0.0.0.0"})
    return {
        "exists": True,
        "status": state.get("Status"),
        "running": bool(state.get("Running")),
        "startedAt": state.get("StartedAt"),
        "finishedAt": state.get("FinishedAt"),
        "restartCount": c.get("RestartCount"),
        "image": c.get("Config", {}).get("Image"),
        "imageId": (c.get("Image") or "")[:19] or None,
        "restartPolicy": (host.get("RestartPolicy") or {}).get("Name"),
        "stopTimeout": c.get("Config", {}).get("StopTimeout"),
        "ports": ports,
        "mounts": [{"source": m.get("Source"), "destination": m.get("Destination")} for m in c.get("Mounts", [])],
    }


# --- Snapshot ----------------------------------------------------------------------------

def loaded_config(config, config_mtime, process_start, state_file=STATE_FILE):
    """Best guess of the config the running server loaded, plus whether the file changed since.

    While the file is unchanged since the server started, it *is* what the server loaded, so it
    is cached; after a later edit the cached copy still says what is running."""
    if process_start is None or config is None:
        return None, None
    if config_mtime <= process_start + LOADED_GRACE_SECONDS:
        os.makedirs(os.path.dirname(state_file), exist_ok=True)
        with open(state_file, "w") as f:
            json.dump({"serverStartedAt": process_start, "config": config}, f)
        return config, False
    try:
        with open(state_file) as f:
            cached = json.load(f)
    except (OSError, ValueError):
        return None, True
    if abs(cached.get("serverStartedAt", 0) - process_start) <= 2:
        return cached.get("config"), True
    return None, True


def build_snapshot(data_dir, now=None, process_start=None, container=None, state_file=STATE_FILE):
    now = now if now is not None else datetime.datetime.now(datetime.timezone.utc).timestamp()
    errors = []
    snap = {"schema": SCHEMA, "inspectedAt": iso(now), "errors": errors}

    config_path = os.path.join(data_dir, "enshrouded_server.json")
    config, mtime = None, None
    snap["configFile"] = {"path": config_path,
                          "containerPath": CONTAINER_DATA_DIR + "/enshrouded_server.json",
                          "present": os.path.exists(config_path)}
    if snap["configFile"]["present"]:
        mtime = os.stat(config_path).st_mtime
        snap["configFile"]["modifiedAt"] = iso(mtime)
        try:
            with open(config_path) as f:
                config, redacted = sanitize_config(json.load(f))
            snap["configFile"]["redactedKeys"] = redacted
        except ValueError as e:
            errors.append(f"config: not valid JSON ({e.__class__.__name__})")
    snap["config"] = config

    snap["serverProcess"] = {"running": process_start is not None, "startedAt": iso(process_start)}
    loaded, changed = loaded_config(config, mtime, process_start, state_file) if mtime else (None, None)
    snap["loadedConfig"] = {"serverStartedAt": iso(process_start), "config": loaded} if loaded else None
    snap["configChangedSinceStart"] = changed

    log_dir = (config or {}).get("logDirectory") or "./logs"
    log_path = os.path.normpath(os.path.join(data_dir, log_dir, "enshrouded_server.log"))
    snap["log"] = None
    if os.path.exists(log_path):
        try:
            log = read_log(log_path)
            started = log.pop("startedAt", None)
            log["startedAt"] = iso(started)
            log["path"] = log_path
            if process_start is not None and started is not None:
                log["run"] = "current" if abs(started - process_start) <= LOADED_GRACE_SECONDS else "previous"
            else:
                log["run"] = "previous" if process_start is None else "unknown"
            snap["log"] = log
        except OSError as e:
            errors.append(f"log: {e.strerror}")

    manifest = os.path.join(data_dir, "steamapps", f"appmanifest_{APP_ID}.acf")
    snap["build"] = read_manifest(manifest) if os.path.exists(manifest) else None
    snap["container"] = container
    return snap


def main(argv):
    env = read_env(ENV_FILE)
    data_dir = env.get("DATA_DIR", "/srv/enshrouded")
    try:
        container = container_info()
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        container = {"exists": None, "error": e.__class__.__name__}
    snap = build_snapshot(data_dir, process_start=server_process_start(), container=container)
    body = json.dumps(snap, indent=1, sort_keys=True)
    if "--print" in argv:
        print(body)
        return 0
    with tempfile.NamedTemporaryFile("w", suffix=".json") as tmp:
        tmp.write(body)
        tmp.flush()
        subprocess.run([AWS, "s3", "cp", "--region", env["BUCKET_REGION"], "--only-show-errors",
                        "--content-type", "application/json", "--cache-control", "no-store",
                        tmp.name, f"s3://{env['BUCKET']}/{SNAPSHOT_KEY}"], check=True, timeout=60)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
