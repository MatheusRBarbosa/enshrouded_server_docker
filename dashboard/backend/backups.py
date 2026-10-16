"""Backups: start, run (on the instance via the fixed SSM document), verify, record, expire.

A backup is only marked succeeded after the instance script reports the uploaded keys, sizes and
SHA-256 *and* S3 confirms both objects with matching size and checksum metadata. Only one backup
runs at a time (LOCK/backup). Retention keeps the newest RETENTION_COUNT successful backups however
old they are, so a pause never thins them out; everything older than the oldest kept one goes. It never
touches in-progress backups and never deletes the newest object under world/.

Objects per backup <id> (= <UTC stamp>-<type>):
  world/<id>.tar.gz       savegame/ + enshrouded_server.json  (disaster recovery; has passwords)
  downloads/<id>.tar.gz   savegame/ only                       (what the dashboard lets people download)
"""
import json

RETENTION_COUNT = 24  # successful backups kept, regardless of age
LOCK_TTL_SECONDS = 15 * 60
STALE_SECONDS = 30 * 60
TYPES = ("automatic", "manual", "shutdown")


def _iso(ts):
    import schedules

    return schedules.iso(ts)


def _stamp(ts):
    return _iso(ts).replace("-", "").replace(":", "")


def new_id(backup_type, ts):
    return f"{_stamp(ts)}-{backup_type}"


class Busy(Exception):
    pass


class NotRunning(Exception):
    pass


def record_skipped(store, backup_type, now, reason, ts=None):
    bid = new_id(backup_type, ts or now)
    item = {"pk": "BACKUP", "sk": bid, "id": bid, "type": backup_type, "status": "skipped",
            "startedAt": _iso(now), "finishedAt": _iso(now), "reason": reason}
    store.put(item, if_absent=True)
    return store.get("BACKUP", bid)


def begin(store, cloud, backup_type, now, ts=None, instance=None):
    """Claim the lock and create the in_progress record. Raises NotRunning / Busy."""
    assert backup_type in TYPES
    instance = instance or cloud.instance()
    if not instance or instance["state"] != "running":
        raise NotRunning("The server machine isn't running.")
    bid = new_id(backup_type, ts or now)
    if not store.acquire_lock("backup", bid, now, LOCK_TTL_SECONDS):
        raise Busy("Another backup is still running.")
    item = {"pk": "BACKUP", "sk": bid, "id": bid, "type": backup_type, "status": "in_progress",
            "startedAt": _iso(now), "instanceId": instance["id"]}
    if not store.put(item, if_absent=True):
        # Same id (same second): that backup exists already; its lock isn't ours to release
        raise Busy("Another backup is still running.")
    return item


def _parse_result(stdout):
    for line in reversed((stdout or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                return None
    return None


def _verify(cloud, result, bid):
    for prefix, k in (("world/", "key"), ("downloads/", "downloadKey")):
        key = result.get(k)
        if key != f"{prefix}{bid}.tar.gz":
            return f"unexpected object key {key!r}"
        head = cloud.head(key)
        if head is None:
            return f"{key} is missing in S3"
        size_field = "size" if k == "key" else "downloadSize"
        sha_field = "sha256" if k == "key" else "downloadSha256"
        if head["size"] != result.get(size_field):
            return f"{key}: size in S3 ({head['size']}) differs from what was uploaded ({result.get(size_field)})"
        if head["metadata"].get("sha256") != result.get(sha_field):
            return f"{key}: checksum metadata doesn't match"
    return None


def run(store, cloud, item, clock, stop_server=False, timeout=300):
    """Execute a started backup to completion and record the outcome. Always releases the lock."""
    bid = item["id"]
    try:
        try:
            command = cloud.send_backup(item["instanceId"], bid, item["type"], stop_server)
        except Exception as e:  # noqa: BLE001 - recorded, not raised
            return _fail(store, bid, clock.now(), f"Could not reach the server agent (SSM): {e.__class__.__name__}")
        store.update("BACKUP", bid, {"commandId": command})
        deadline = clock.now() + timeout
        status, out, err = "Pending", "", ""
        while clock.now() < deadline:
            clock.sleep(3)
            status, out, err = cloud.command_result(command, item["instanceId"])
            if status in ("Success", "Failed", "Cancelled", "TimedOut", "Cancelling", "DeliveryTimedOut", "Undeliverable", "Terminated"):
                break
        result = _parse_result(out)
        if status != "Success":
            reason = (result or {}).get("error") or (err or "").strip().splitlines()[-1:] or [status]
            reason = reason if isinstance(reason, str) else reason[0]
            return _fail(store, bid, clock.now(), f"Backup command {status.lower()}: {reason}"[:300])
        if not result or not result.get("ok"):
            return _fail(store, bid, clock.now(), "The backup script didn't report a result.")
        problem = _verify(cloud, result, bid)
        if problem:
            return _fail(store, bid, clock.now(), "Upload not verified: " + problem)
        store.update("BACKUP", bid, {
            "status": "succeeded", "finishedAt": _iso(clock.now()),
            "key": result["key"], "size": result["size"], "sha256": result["sha256"],
            "downloadKey": result["downloadKey"], "downloadSize": result["downloadSize"],
            "files": result.get("files"), "attempts": result.get("attempts"),
            "serverStopped": bool(result.get("serverStopped")),
        })
        return store.get("BACKUP", bid)
    finally:
        store.release_lock("backup", bid)


def _fail(store, bid, now, reason):
    store.update("BACKUP", bid, {"status": "failed", "finishedAt": _iso(now), "reason": reason})
    return store.get("BACKUP", bid)


# --- History, health, retention ---------------------------------------------------------------

def public(item):
    keep = ("id", "type", "status", "startedAt", "finishedAt", "size", "downloadSize", "reason", "files", "attempts", "serverStopped")
    out = {k: item[k] for k in keep if k in item}
    out["downloadable"] = item.get("status") == "succeeded" and bool(item.get("downloadKey"))
    return out


def page(store, limit=20, cursor=None):
    items, nxt = store.query("BACKUP", newest_first=True, limit=limit, start_after=cursor)
    return [public(i) for i in items], nxt


def latest_successful(items):
    ok = [i for i in items if i.get("status") == "succeeded"]
    return max(ok, key=lambda i: i["sk"]) if ok else None


def health(store, now, instance_state):
    import schedules

    items, _ = store.query("BACKUP", newest_first=True, limit=48)
    latest = latest_successful(items)
    attempts = [i for i in items if i["status"] in ("succeeded", "failed")]
    recent_failures = sum(1 for i in items if i["status"] == "failed" and schedules.parse_iso(i["startedAt"]) > now - 86400)
    age = now - schedules.parse_iso(latest["finishedAt"]) if latest else None
    if latest is None:
        state, text = "none", "No successful backup recorded yet."
    elif attempts and attempts[0]["status"] == "failed" and len(attempts) > 1 and attempts[1]["status"] == "failed":
        state, text = "failing", "The last two backups failed."
    elif instance_state != "running":
        state, text = "paused", "Backups pause while the server machine is stopped."
    elif age <= 2 * 3600:
        state, text = "healthy", "Backups are running on schedule."
    else:
        state, text = "stale", "No successful backup in the last 2 hours."
    return {"state": state, "text": text, "latest": public(latest) if latest else None,
            "recentFailures": recent_failures,
            "lastAttempt": public(items[0]) if items else None}


def apply_retention(store, cloud, now):
    """Keep the newest RETENTION_COUNT successful backups; expire what's older. Returns what was removed."""
    import schedules

    items, _ = store.query("BACKUP", newest_first=True)
    for i in items:
        if i["status"] == "in_progress" and schedules.parse_iso(i["startedAt"]) < now - STALE_SECONDS:
            store.update("BACKUP", i["sk"], {"status": "failed", "finishedAt": schedules.iso(now),
                                             "reason": "Never finished (timed out)."}, expect={"status": "in_progress"})
    succeeded = sorted((i for i in items if i["status"] == "succeeded"), key=lambda i: i["sk"], reverse=True)
    if len(succeeded) <= RETENTION_COUNT:
        return {"records": [], "objects": []}  # nothing is old enough yet, however long ago it was taken
    # Ids start with the UTC stamp, so they sort by time. Everything older than the oldest kept backup goes:
    # older backups with their files, and failed or skipped runs from before it.
    cutoff = succeeded[RETENTION_COUNT - 1]
    cutoff_started = schedules.parse_iso(cutoff["startedAt"])
    removed_items, removed_keys = [], []
    for i in items:
        if i["status"] == "in_progress" or i["sk"] >= cutoff["sk"]:
            continue
        keys = [k for k in (i.get("key"), i.get("downloadKey")) if k]
        cloud.delete_objects(keys)
        store.delete("BACKUP", i["sk"])
        removed_items.append(i["sk"])
        removed_keys += keys

    # Objects without a record (older CLI backups, records already gone). An upload still in flight has
    # no record keys yet either, but it is newer than the cutoff.
    remaining, _ = store.query("BACKUP", newest_first=True)
    referenced = {k for i in remaining for k in (i.get("key"), i.get("downloadKey")) if k}
    world = cloud.list_objects("world/")
    newest_world = max((o["key"] for o in world), default=None)
    orphans = [o["key"] for o in world + cloud.list_objects("downloads/")
               if o["key"] not in referenced and o["key"] != newest_world
               and o["lastModified"] < cutoff_started]
    cloud.delete_objects(orphans)
    return {"records": removed_items, "objects": removed_keys + orphans}
