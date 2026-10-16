"""In-memory stand-ins for AWS, the clock and HTTP events."""
import copy
import datetime
import hashlib
import json

import api
import store as store_mod
from helpers import fixture_snapshot

HOST = "abc123.lambda-url.us-east-2.on.aws"
ORIGIN = f"https://{HOST}"
ENV = {"DASHBOARD_USERNAME": "friends", "DASHBOARD_PASSWORD": "correct horse battery",
       "DASHBOARD_SESSION_SECRET": "s" * 48, "FUNCTION_ARN": "arn:aws:lambda:us-east-2:111111111111:function:enshrouded-dashboard"}
T0 = datetime.datetime(2026, 10, 15, 19, 0, tzinfo=datetime.timezone.utc).timestamp()  # 16:00 in São Paulo


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


def schedule(expr, enabled=True, tz="America/Sao_Paulo", target=ENV["FUNCTION_ARN"]):
    return {"ScheduleExpression": expr, "ScheduleExpressionTimezone": tz, "State": "ENABLED" if enabled else "DISABLED",
            "FlexibleTimeWindow": {"Mode": "OFF"},
            "Target": {"Arn": target, "RoleArn": "arn:aws:iam::111111111111:role/enshrouded-scheduler", "Input": "{}"}}


class Cloud:
    def __init__(self, clock, state="running"):
        self.clock = clock
        self.state = state
        self.calls = []
        self.objects = {}  # key -> {"size", "metadata", "lastModified"}
        self.snapshot = fixture_snapshot()
        self.snapshot["inspectedAt"] = datetime.datetime.fromtimestamp(T0 - 60, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.events = []  # {"stream", "t", "m"}
        self.async_payloads = []
        self.backup_behavior = "success"
        self.commands = {}
        self.fail = set()  # method names that raise
        self.metric_points = None  # {"cpu": [[ms, v]], "memory": [...]}; None = synthetic data
        self.metric_queries = []
        self.schedules = {
            "enshrouded-stop": schedule("cron(0 0 * * ? *)"),
            "enshrouded-backup": schedule("cron(30 * * * ? *)"),
        }

    def _maybe_fail(self, name):
        self.calls.append(name)
        if name in self.fail:
            raise RuntimeError(f"AWS said no to {name} (arn:aws:iam::111111111111:role/secret)")

    def instance(self):
        self._maybe_fail("instance")
        return {"id": "i-0123", "state": self.state, "type": "m6a.xlarge", "launchTime": T0 - 3600,
                "publicIp": "203.0.113.10" if self.state in ("pending", "running") else None, "region": "us-east-2", "securityGroups": ["sg-1"]}

    def start_instance(self, instance_id):
        self._maybe_fail("start_instance")
        self.state = "pending"

    def stop_instance(self, instance_id):
        self._maybe_fail("stop_instance")
        self.state = "stopping"

    def udp_open_to_all(self, groups):
        return [15637]

    def send_backup(self, instance_id, backup_id, backup_type, stop_server):
        self._maybe_fail("send_backup")
        cid = f"cmd-{len(self.commands)}"
        self.commands[cid] = {"id": backup_id, "type": backup_type, "stop": stop_server, "polls": 0}
        return cid

    def command_result(self, command_id, instance_id):
        c = self.commands[command_id]
        c["polls"] += 1
        if self.backup_behavior == "hang" or c["polls"] < 2:
            return "InProgress", "", ""
        if self.backup_behavior == "fail":
            return "Failed", json.dumps({"ok": False, "error": "no savegame directory"}), "boom"
        bid = c["id"]
        result = {"ok": True, "id": bid, "files": 10, "attempts": 1, "serverStopped": c["stop"]}
        for prefix, k, size in (("world/", "", 1200), ("downloads/", "download", 900)):
            key = f"{prefix}{bid}.tar.gz"
            sha = hashlib.sha256(key.encode()).hexdigest()
            stored_sha = "0" * 64 if self.backup_behavior == "bad-checksum" else sha
            self.objects[key] = {"size": size, "metadata": {"sha256": stored_sha}, "lastModified": self.clock.now()}
            result["downloadKey" if k else "key"] = key
            result["downloadSize" if k else "size"] = size
            result["downloadSha256" if k else "sha256"] = sha
        return "Success", "log line\n" + json.dumps(result) + "\n", ""

    def head(self, key):
        o = self.objects.get(key)
        return None if o is None else {"size": o["size"], "metadata": dict(o["metadata"])}

    def list_objects(self, prefix):
        return [{"key": k, "size": o["size"], "lastModified": o["lastModified"]}
                for k, o in sorted(self.objects.items()) if k.startswith(prefix)]

    def delete_objects(self, keys):
        for k in keys:
            self.objects.pop(k, None)

    def presign(self, key, filename, seconds=300):
        return f"https://bucket.s3.amazonaws.com/{key}?X-Amz-Expires={seconds}&sig=x"

    def read_json(self, key):
        self._maybe_fail("read_json")
        return copy.deepcopy(self.snapshot)

    def log_events(self, stream_prefix, start_ms, end_ms, max_events):
        self.calls.append(("log_events", stream_prefix, start_ms, end_ms, max_events))
        out = [{"t": e["t"], "m": e["m"]} for e in self.events
               if e["stream"].startswith(stream_prefix) and start_ms <= e["t"] <= end_ms]
        return out[:max_events]

    def metric_data(self, queries, start, end):
        """Synthetic usage over the window: CPU every 5 min, memory every minute (like CloudWatch)."""
        self._maybe_fail("metric_data")
        self.metric_queries.append(queries)
        if self.metric_points is not None:
            return copy.deepcopy(self.metric_points)
        out = {}
        for q in queries:
            period = q["MetricStat"]["Period"]
            first = int(start // period + 1) * period
            out[q["Id"]] = [[t * 1000, (35.0 if q["Id"] == "cpu" else 52.0) + (t // period % 7) * 1.5]
                            for t in range(first, int(end), period)]
        return out

    def get_schedule(self, name):
        s = self.schedules.get(name)
        return copy.deepcopy(s) if s else None

    def run_async(self, payload):
        self._maybe_fail("run_async")
        self.async_payloads.append(payload)


def make_app(state="running", env=None):
    clock = Clock()
    cloud = Cloud(clock, state)
    store = store_mod.MemoryStore()
    app = api.App(store, cloud, clock, env or ENV)
    return app, cloud, store, clock


def event(method, path, body=None, cookies=None, headers=None, query="", ip="198.51.100.7"):
    h = {"host": HOST, "content-type": "application/json"}
    if method != "GET":
        h["origin"] = ORIGIN
        h["sec-fetch-site"] = "same-origin"
    h.update(headers or {})
    return {"version": "2.0", "rawPath": path, "rawQueryString": query, "headers": h, "cookies": cookies or [],
            "body": json.dumps(body) if body is not None else "", "isBase64Encoded": False,
            "requestContext": {"http": {"method": method, "sourceIp": ip}}}


class Browser:
    """Logs in once and sends the cookie + CSRF token like the page does."""

    def __init__(self, app):
        self.app = app
        resp = app.http(event("POST", "/api/login", {"username": ENV["DASHBOARD_USERNAME"], "password": ENV["DASHBOARD_PASSWORD"]}))
        assert resp["statusCode"] == 200, resp
        self.cookie = resp["cookies"][0].split(";")[0]
        self.csrf = json.loads(resp["body"])["csrf"]

    def get(self, path, query=""):
        return self.app.http(event("GET", path, cookies=[self.cookie], query=query))

    def post(self, path, body=None, headers=None):
        h = {"x-csrf-token": self.csrf, **(headers or {})}
        return self.app.http(event("POST", path, body or {}, cookies=[self.cookie], headers=h))


def body(resp):
    return json.loads(resp["body"])
