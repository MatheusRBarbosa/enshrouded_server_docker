"""Recurring automation: EventBridge Scheduler schedules, next occurrences, skips, run claims.

Schedules (group SCHEDULE_GROUP, created by dashboard/deploy.sh) invoke the dashboard Lambda with
{"task": "scheduled", "schedule": "<key>", "scheduledTime": "<aws.scheduler.scheduled-time>"}.

A skip is bound to one occurrence (its UTC time). The run for exactly that occurrence consumes
it with a conditional write; a retry of the same run sees "consumed" and skips again, and no
run can consume a skip meant for a different occurrence. Each run is also claimed in EXEC#<key>
so duplicate deliveries don't act twice; a claim whose lease ran out can be taken over.
"""
import datetime

SCHEDULES = {
    "stop": {"name": "enshrouded-stop", "label": "Stop server", "skippable": True},
    "backup": {"name": "enshrouded-backup", "label": "Hourly backup", "skippable": False},
}
RUN_LEASE_SECONDS = 900
SKIP_GRACE_SECONDS = 3600  # a pending skip whose occurrence is this far past was never used
_FIXED_OFFSETS = {"America/Sao_Paulo": -3, "UTC": 0, "Etc/UTC": 0}  # if tzdata is unavailable


def tzinfo(name):
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name or "UTC")
    except Exception:
        if name in _FIXED_OFFSETS:
            return datetime.timezone(datetime.timedelta(hours=_FIXED_OFFSETS[name]))
        raise


def iso(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text):
    return datetime.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def normalize_occurrence(text):
    return iso(parse_iso(text))


# --- cron ----------------------------------------------------------------------------------

def _field(spec, lo, hi):
    values = set()
    for part in spec.split(","):
        step = 1
        if "/" in part:
            part, step_s = part.split("/")
            step = int(step_s)
        if part in ("*", ""):
            a, b = lo, hi
        elif "-" in part:
            a, b = map(int, part.split("-"))
        else:
            a = int(part)
            b = hi if step > 1 else a
        if not (lo <= a <= b <= hi):
            raise ValueError(spec)
        values.update(range(a, b + 1, step))
    return sorted(values)


def parse_cron(expression):
    """Daily-style cron(min hour * * ? *) as [(hour, minute)], or None if unsupported."""
    if not expression or not expression.startswith("cron(") or not expression.endswith(")"):
        return None
    parts = expression[5:-1].split()
    if len(parts) != 6 or parts[2] not in ("*", "?") or parts[3] != "*" or parts[4] not in ("*", "?") or parts[5] != "*":
        return None
    try:
        minutes, hours = _field(parts[0], 0, 59), _field(parts[1], 0, 23)
    except ValueError:
        return None
    return [(h, m) for h in hours for m in minutes]


def next_occurrences(expression, timezone, after, count=1):
    """The next `count` run times (epoch seconds, strictly after `after`)."""
    times = parse_cron(expression)
    if not times:
        return []
    tz = tzinfo(timezone)
    day = datetime.datetime.fromtimestamp(after, tz).date()
    out = []
    for _ in range(3 + count):
        for h, m in times:
            ts = datetime.datetime(day.year, day.month, day.day, h, m, tzinfo=tz).timestamp()
            if ts > after:
                out.append(ts)
                if len(out) == count:
                    return out
        day += datetime.timedelta(days=1)
    return out


# --- Deployed schedules ---------------------------------------------------------------------

def describe(cloud, key, now, function_arn=None):
    meta = SCHEDULES[key]
    s = cloud.get_schedule(meta["name"])
    if s is None:
        return {"key": key, "label": meta["label"], "deployed": False, "skippable": meta["skippable"]}
    target = s.get("Target") or {}
    enabled = s.get("State") == "ENABLED"
    upcoming = next_occurrences(s.get("ScheduleExpression"), s.get("ScheduleExpressionTimezone"), now, 1)
    return {
        "key": key,
        "label": meta["label"],
        "deployed": True,
        "skippable": meta["skippable"],
        "enabled": enabled,
        "expression": s.get("ScheduleExpression"),
        "timezone": s.get("ScheduleExpressionTimezone") or "UTC",
        "targetsDashboard": function_arn is None or target.get("Arn") == function_arn,
        "hasRole": bool(target.get("RoleArn")),
        "next": iso(upcoming[0]) if enabled and upcoming else None,
        "nextIfEnabled": iso(upcoming[0]) if upcoming else None,
        "supported": parse_cron(s.get("ScheduleExpression")) is not None,
    }


# --- Skips ----------------------------------------------------------------------------------

def request_skip(store, key, occurrence, now, by="dashboard"):
    sk = f"{key}#{occurrence}"
    item = {"pk": "SKIP", "sk": sk, "schedule": key, "occurrence": occurrence, "status": "pending",
            "createdAt": iso(now), "by": by, "ttl": int(parse_iso(occurrence) + 30 * 86400)}
    if store.put(item, if_absent=True):
        return item
    # Re-arm a cancelled skip; anything else (pending/consumed) stays as it is
    store.update("SKIP", sk, {"status": "pending", "createdAt": iso(now), "by": by}, expect={"status": "cancelled"})
    return store.get("SKIP", sk)


def cancel_skip(store, key, occurrence, now):
    return store.update("SKIP", f"{key}#{occurrence}", {"status": "cancelled", "cancelledAt": iso(now)},
                        expect={"status": "pending"})


def consume_skip(store, key, occurrence, now):
    """True if the run for this occurrence must be skipped. Safe to call repeatedly."""
    sk = f"{key}#{occurrence}"
    item = store.get("SKIP", sk)
    if item is None:
        return False
    if item["status"] == "pending":
        if store.update("SKIP", sk, {"status": "consumed", "consumedAt": iso(now)}, expect={"status": "pending"}):
            return True
        item = store.get("SKIP", sk) or {}
    return item.get("status") == "consumed"


def list_skips(store, now):
    items, _ = store.query("SKIP", newest_first=True)
    out = []
    for i in items:
        status = i["status"]
        if status == "pending" and parse_iso(i["occurrence"]) + SKIP_GRACE_SECONDS < now:
            status = "expired"  # schedule was off or the run never arrived
        out.append({"schedule": i["schedule"], "occurrence": i["occurrence"], "status": status,
                    "createdAt": i.get("createdAt"), "consumedAt": i.get("consumedAt")})
    return out


# --- Run claims -----------------------------------------------------------------------------

def claim_run(store, key, occurrence, now):
    """'new' if this invocation should do the work, 'duplicate' otherwise."""
    pk = f"EXEC#{key}"
    item = {"pk": pk, "sk": occurrence, "status": "started", "startedAt": iso(now),
            "leaseUntil": int(now + RUN_LEASE_SECONDS), "ttl": int(now + 30 * 86400)}
    if store.put(item, if_absent=True):
        return "new"
    current = store.get(pk, occurrence) or {}
    retry = {"status": "started", "leaseUntil": int(now + RUN_LEASE_SECONDS), "retriedAt": iso(now)}
    # A crashed run (lease ran out) or a failed one (Lambda retries it) may be taken over, once per claim
    if current.get("status") == "started" and current.get("leaseUntil", 0) < now:
        if store.update(pk, occurrence, retry, expect={"status": "started", "leaseUntil": current["leaseUntil"]}):
            return "new"
    elif current.get("status") == "done" and current.get("result") == "failed":
        if store.update(pk, occurrence, retry, expect={"status": "done", "result": "failed",
                                                       "finishedAt": current.get("finishedAt")}):
            return "new"
    return "duplicate"


def finish_run(store, key, occurrence, result, now, detail=None):
    changes = {"status": "done", "result": result, "finishedAt": iso(now)}
    if detail:
        changes["detail"] = detail[:500]
    store.update(f"EXEC#{key}", occurrence, changes)


def recent_runs(store, key, limit=5):
    items, _ = store.query(f"EXEC#{key}", newest_first=True, limit=limit)
    return [{"occurrence": i["sk"], "status": i["status"], "result": i.get("result"),
             "finishedAt": i.get("finishedAt"), "detail": i.get("detail")} for i in items]
