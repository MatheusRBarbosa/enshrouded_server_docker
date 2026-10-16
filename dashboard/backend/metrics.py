"""CPU and memory history for the Overview charts, from CloudWatch metrics.

  cpu     AWS/EC2 CPUUtilization: EC2's free basic monitoring, one point every 5 minutes
  memory  Enshrouded/mem_used_percent: EC2 can't see inside the machine, so the CloudWatch agent
          on the instance publishes it every minute (dashboard/agent/cloudwatch-agent.json)

Both are queried for the current instance only. CloudWatch bills every metric read
(GetMetricData has no free tier), so one result is cached in the store for CACHE_SECONDS and
shared by everyone looking at the page.
"""
import schedules

WINDOW_HOURS = 6
CACHE_SECONDS = 120
NAMESPACE = "Enshrouded"
SERIES = {
    "cpu": {"label": "CPU", "namespace": "AWS/EC2", "metric": "CPUUtilization", "period": 300},
    "memory": {"label": "Memory", "namespace": NAMESPACE, "metric": "mem_used_percent", "period": 60},
}
CACHE_KEY = ("CACHE", "metrics")


def queries(instance_id):
    return [{"Id": key, "MetricStat": {
                "Metric": {"Namespace": s["namespace"], "MetricName": s["metric"],
                           "Dimensions": [{"Name": "InstanceId", "Value": instance_id}]},
                "Period": s["period"], "Stat": "Average"}}
            for key, s in SERIES.items()]


def fetch(store, cloud, now):
    """{"state", "from", "to", "series": {key: {label, period, points: [[ms, percent], ...]}}, "errors"}"""
    out = {"state": None, "from": schedules.iso(now - WINDOW_HOURS * 3600), "to": schedules.iso(now),
           "series": {k: {"label": s["label"], "period": s["period"], "points": []} for k, s in SERIES.items()},
           "errors": []}
    try:
        instance = cloud.instance()
    except Exception:  # noqa: BLE001
        out["errors"].append("Could not read the server machine's state from AWS.")
        return out
    if not instance:
        return out
    out["state"] = instance["state"]

    try:
        cached = store.get(*CACHE_KEY)
    except Exception:  # noqa: BLE001
        cached = None
    if cached and cached.get("instanceId") == instance["id"] and now - cached["fetchedAt"] < CACHE_SECONDS:
        return dict(cached["data"], state=instance["state"])

    try:
        raw = cloud.metric_data(queries(instance["id"]), now - WINDOW_HOURS * 3600, now)
    except Exception:  # noqa: BLE001
        out["errors"].append("Could not read CPU and memory usage from AWS.")
        return out
    for key, series in out["series"].items():
        points = sorted((int(t), round(min(max(float(v), 0.0), 100.0), 1)) for t, v in raw.get(key, []))
        series["points"] = [list(p) for p in points]
    try:
        store.put({"pk": CACHE_KEY[0], "sk": CACHE_KEY[1], "instanceId": instance["id"], "fetchedAt": now,
                   "data": out, "ttl": int(now) + 3600})
    except Exception:  # noqa: BLE001
        pass  # the cache only saves money; never fail the request over it
    return out
