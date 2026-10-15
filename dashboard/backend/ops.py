"""Server power operations and the work behind scheduled runs.

Stopping always: stops the game container, takes a backup of the stopped world, then stops the
instance. The instance is stopped even if that backup fails (the failure is recorded), because
a scheduled stop must not leave the machine running all night. Starting only starts the
instance; the container starts at boot (enshrouded-autostart.service) and updates the game.

Only one start/stop runs at a time (LOCK/power). Manual operations are recorded as OP items
so the page can follow them; they never touch the recurring schedules.
"""
import secrets

import backups
import schedules

POWER_LOCK_SECONDS = 15 * 60
OP_TTL_SECONDS = 7 * 86400


class Clock:
    def now(self):
        import time

        return time.time()

    def sleep(self, seconds):
        import time

        time.sleep(seconds)


def new_op(store, kind, now, by="dashboard"):
    op = {"pk": "OP", "sk": f"{schedules.iso(now)}-{secrets.token_hex(3)}", "kind": kind, "status": "queued",
          "createdAt": schedules.iso(now), "by": by, "steps": [], "ttl": int(now + OP_TTL_SECONDS)}
    op["id"] = op["sk"]
    store.put(op, if_absent=True)
    return op


def _op_update(store, op_id, now, status=None, step=None, message=None):
    if not op_id:
        return
    current = store.get("OP", op_id) or {}
    changes = {}
    if status:
        changes["status"] = status
        if status in ("succeeded", "failed"):
            changes["finishedAt"] = schedules.iso(now)
    if step:
        changes["steps"] = (current.get("steps") or []) + [{"at": schedules.iso(now), "text": step}]
    if message:
        changes["message"] = message
    store.update("OP", op_id, changes)


def public_op(op):
    if not op:
        return None
    return {k: op.get(k) for k in ("id", "kind", "status", "createdAt", "finishedAt", "message", "steps", "by")}


def latest_op(store):
    items, _ = store.query("OP", newest_first=True, limit=1)
    return public_op(items[0]) if items else None


def _wait_for_lock(store, name, owner, clock, wait_seconds, ttl):
    deadline = clock.now() + wait_seconds
    while True:
        if store.acquire_lock(name, owner, clock.now(), ttl):
            return True
        if clock.now() >= deadline:
            return False
        clock.sleep(5)


def stop_server(store, cloud, clock, op_id=None, reason="dashboard"):
    """Returns (ok, message)."""
    owner = op_id or f"stop-{secrets.token_hex(4)}"
    if not store.acquire_lock("power", owner, clock.now(), POWER_LOCK_SECONDS):
        _op_update(store, op_id, clock.now(), "failed", message="Another start or stop is already in progress.")
        return False, "Another start or stop is already in progress."
    try:
        _op_update(store, op_id, clock.now(), "running")
        instance = cloud.instance()
        if not instance:
            _op_update(store, op_id, clock.now(), "failed", message="Server machine not found.")
            return False, "Server machine not found."
        if instance["state"] in ("stopped", "stopping"):
            _op_update(store, op_id, clock.now(), "succeeded", message=f"Already {instance['state']}.")
            return True, f"already {instance['state']}"
        if instance["state"] != "running":
            _op_update(store, op_id, clock.now(), "failed", message=f"Can't stop while {instance['state']}.")
            return False, f"instance is {instance['state']}"

        backup_note = "no backup"
        _op_update(store, op_id, clock.now(), step="Stopping the game and saving a backup")
        # An hourly backup may be running; give it time to finish
        if _wait_for_lock(store, "backup", "power-wait", clock, 150, 1):
            store.release_lock("backup", "power-wait")
        try:
            item = backups.begin(store, cloud, "shutdown", clock.now(), instance=instance)
            result = backups.run(store, cloud, item, clock, stop_server=True, timeout=360)
            backup_note = "backup saved" if result["status"] == "succeeded" else f"backup failed: {result.get('reason')}"
        except (backups.Busy, backups.NotRunning) as e:
            backup_note = f"backup skipped: {e}"
        _op_update(store, op_id, clock.now(), step=backup_note[:1].upper() + backup_note[1:])

        _op_update(store, op_id, clock.now(), step="Stopping the server machine")
        cloud.stop_instance(instance["id"])
        msg = f"Stop requested ({backup_note})."
        _op_update(store, op_id, clock.now(), "succeeded", message=msg)
        return True, msg
    except Exception as e:  # noqa: BLE001 - reported to the page and the run record
        _op_update(store, op_id, clock.now(), "failed", message=f"Stop failed: {e.__class__.__name__}")
        raise
    finally:
        store.release_lock("power", owner)


def start_server(store, cloud, clock, op_id=None, wait_for_stopped=0):
    owner = op_id or f"start-{secrets.token_hex(4)}"
    if not store.acquire_lock("power", owner, clock.now(), POWER_LOCK_SECONDS):
        _op_update(store, op_id, clock.now(), "failed", message="Another start or stop is already in progress.")
        return False, "Another start or stop is already in progress."
    try:
        _op_update(store, op_id, clock.now(), "running")
        deadline = clock.now() + wait_for_stopped
        while True:
            instance = cloud.instance()
            if not instance:
                _op_update(store, op_id, clock.now(), "failed", message="Server machine not found.")
                return False, "Server machine not found."
            if instance["state"] != "stopping" or clock.now() >= deadline:
                break
            clock.sleep(10)
        if instance["state"] in ("running", "pending"):
            _op_update(store, op_id, clock.now(), "succeeded", message=f"Already {instance['state']}.")
            return True, f"already {instance['state']}"
        if instance["state"] != "stopped":
            _op_update(store, op_id, clock.now(), "failed", message=f"Can't start while {instance['state']}.")
            return False, f"instance is {instance['state']}"
        cloud.start_instance(instance["id"])
        _op_update(store, op_id, clock.now(), "succeeded", step="Start requested",
                   message="Machine starting. The game updates and loads the world before players can join.")
        return True, "start requested"
    except Exception as e:  # noqa: BLE001
        _op_update(store, op_id, clock.now(), "failed", message=f"Start failed: {e.__class__.__name__}")
        raise
    finally:
        store.release_lock("power", owner)


def scheduled(store, cloud, clock, key, scheduled_time):
    """Handle one EventBridge Scheduler delivery. Returns the recorded result."""
    if key not in schedules.SCHEDULES:
        raise ValueError(f"unknown schedule {key!r}")
    occurrence = schedules.normalize_occurrence(scheduled_time)
    now = clock.now()
    if schedules.claim_run(store, key, occurrence, now) == "duplicate":
        return "duplicate"
    try:
        if schedules.SCHEDULES[key]["skippable"] and schedules.consume_skip(store, key, occurrence, now):
            result, detail = "skipped", "Skipped as requested on the dashboard."
        elif key == "stop":
            ok, detail = stop_server(store, cloud, clock, reason="schedule")
            result = "done" if ok else "failed"
        else:
            result, detail = scheduled_backup(store, cloud, clock, occurrence)
        schedules.finish_run(store, key, occurrence, result, clock.now(), detail)
        return result
    except Exception as e:
        schedules.finish_run(store, key, occurrence, "failed", clock.now(), f"{e.__class__.__name__}: {e}")
        raise


def scheduled_backup(store, cloud, clock, occurrence):
    ts = schedules.parse_iso(occurrence)
    instance = cloud.instance()
    if not instance or instance["state"] != "running":
        backups.record_skipped(store, "automatic", clock.now(), "Server machine was stopped.", ts=ts)
        return "skipped", "Server machine was stopped."
    try:
        item = backups.begin(store, cloud, "automatic", clock.now(), ts=ts, instance=instance)
    except backups.Busy as e:
        backups.record_skipped(store, "automatic", clock.now(), str(e), ts=ts)
        return "skipped", str(e)
    except backups.NotRunning as e:
        backups.record_skipped(store, "automatic", clock.now(), str(e), ts=ts)
        return "skipped", str(e)
    result = backups.run(store, cloud, item, clock)
    backups.apply_retention(store, cloud, clock.now())
    return ("done", "Backup saved.") if result["status"] == "succeeded" else ("failed", result.get("reason"))
