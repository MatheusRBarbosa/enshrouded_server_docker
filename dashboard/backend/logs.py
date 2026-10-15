"""Recent server logs from CloudWatch Logs (shipped by the CloudWatch agent on the instance).

The browser picks only a named source, a time window and a plain-text filter. The log group,
stream names and limits are fixed here, and the filter is applied in Python, never passed to
CloudWatch as a query.

Streams in LOG_GROUP (see dashboard/agent/cloudwatch-agent.json):
  game       /srv/enshrouded/logs/enshrouded_server.log  game log with [I]/[W]/[E] levels
  container  the container's stdout (Docker json-file): SteamCMD updates, startup, game output
  backup     /var/log/enshrouded-dashboard-backup.log
"""
import json
import re

SOURCES = {
    "game": "Game server",
    "container": "Container (updates and startup)",
    "backup": "Backups",
}
MAX_MINUTES = 24 * 60
MAX_RETURNED = 500
MAX_FETCHED = 3000
MAX_FILTER = 100
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\[0m")
LEVEL = re.compile(r"^\[([IWE]) ")


class BadRequest(ValueError):
    pass


def parse_line(source, message):
    """(text, level, timestamp_override_iso)"""
    text, at = message, None
    if source == "container" and message.startswith("{"):
        try:
            data = json.loads(message)
            text, at = data.get("log", ""), data.get("time")
        except ValueError:
            pass
    text = ANSI.sub("", text).rstrip("\r\n")
    m = LEVEL.match(text)
    level = {"I": "info", "W": "warn", "E": "error"}[m.group(1)] if m else None
    return text, level, at


def fetch(cloud, source, minutes, query, now, limit=200):
    if source not in SOURCES:
        raise BadRequest("Unknown log source.")
    try:
        minutes = int(minutes)
        limit = int(limit)
    except (TypeError, ValueError):
        raise BadRequest("Invalid number.")
    minutes = max(1, min(minutes, MAX_MINUTES))
    limit = max(1, min(limit, MAX_RETURNED))
    query = (query or "").strip()
    if len(query) > MAX_FILTER:
        raise BadRequest(f"Filter is limited to {MAX_FILTER} characters.")
    needle = query.lower()

    end_ms = int(now * 1000)
    events = cloud.log_events(source, end_ms - minutes * 60000, end_ms, MAX_FETCHED)
    lines = []
    for e in events:
        text, level, at = parse_line(source, e["m"])
        if not text.strip():
            continue
        if needle and needle not in text.lower():
            continue
        lines.append({"t": e["t"], "at": at, "level": level, "text": text[:2000]})
    lines.sort(key=lambda x: x["t"])
    truncated = len(lines) > limit or len(events) >= MAX_FETCHED
    return {"source": source, "minutes": minutes, "filter": query, "lines": lines[-limit:], "truncated": truncated,
            "fetched": len(events)}


def recent_errors(cloud, now, minutes=60, count=5):
    data = fetch(cloud, "game", minutes, "", now, limit=MAX_RETURNED)
    errors = [l for l in data["lines"] if l["level"] == "error"]
    return errors[-count:]
