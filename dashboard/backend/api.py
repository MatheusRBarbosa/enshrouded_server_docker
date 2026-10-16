"""HTTP API and background tasks of the dashboard Lambda.

Public (no session): GET / and static assets, GET /api/session (says 401 when logged out),
POST /api/login. Everything else under /api/ needs a valid session, and every POST also needs
the session's CSRF token and a same-origin request.

    GET  /api/status                      overview: machine, readiness, schedules, backups, errors
    POST /api/server/start                {"confirm": "start"}
    POST /api/server/stop                 {"confirm": "stop"}    container stop + backup + instance stop
    GET  /api/operations/<id>
    GET  /api/schedules
    POST /api/schedules/<key>/skip        {"occurrence": "<next occurrence>"}
    POST /api/schedules/<key>/unskip      {"occurrence": "..."}
    GET  /api/backups?cursor=
    POST /api/backups                     manual backup
    GET  /api/backups/<id>
    GET  /api/backups/<id>/download       302 to a 5-minute presigned URL (savegame only)
    GET  /api/logs?source=&minutes=&filter=
    GET  /api/metrics                     CPU and memory, last 6 hours (cached 2 min)
    GET  /api/settings

Tasks (direct invocations only; a Function URL request always arrives as an HTTP event):
    {"task": "scheduled", "schedule": "stop|backup", "scheduledTime": "..."}  EventBridge Scheduler
    {"task": "backup", "id": "..."}   {"task": "stop", "op": "..."}               async continuations
"""
import base64
import json
import os
import re
import time
import urllib.parse

import auth
import backups
import logs
import metrics
import ops
import runtime
import schedules
import settings

HERE = os.path.dirname(os.path.abspath(__file__))
FRONTEND = next(p for p in (os.path.join(HERE, "frontend"), os.path.join(HERE, "..", "frontend")) if os.path.isdir(p))
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
}
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; style-src 'self' https://fonts.googleapis.com; "
                               "font-src https://fonts.gstatic.com; img-src 'self' data:; "
                               "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Strict-Transport-Security": "max-age=31536000",
}
SNAPSHOT_KEY = "dashboard/snapshot.json"
SNAPSHOT_FRESH_SECONDS = 300
MAX_BODY = 8192
SETTINGS_CACHE_SECONDS = 10


class HttpError(Exception):
    def __init__(self, status, message, headers=None):
        super().__init__(message)
        self.status, self.message, self.headers = status, message, headers or {}


def json_response(status, data, cookies=None, headers=None):
    resp = {"statusCode": status, "body": json.dumps(data),
            "headers": {"Content-Type": "application/json", "Cache-Control": "no-store", **SECURITY_HEADERS, **(headers or {})}}
    if cookies:
        resp["cookies"] = cookies
    return resp


class Request:
    def __init__(self, event):
        http = event.get("requestContext", {}).get("http", {})
        self.method = http.get("method", "GET")
        self.path = event.get("rawPath") or "/"
        self.ip = http.get("sourceIp", "unknown")
        self.headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
        self.cookies = event.get("cookies") or [c for c in (self.headers.get("cookie") or "").split(";") if c]
        self.query = dict(urllib.parse.parse_qsl(event.get("rawQueryString") or ""))
        body = event.get("body") or ""
        if event.get("isBase64Encoded") and body:
            body = base64.b64decode(body).decode("utf-8", "replace")
        self.raw_body = body
        host = self.headers.get("host", "")
        self.origin = f"https://{host}" if host else None

    def json(self):
        if len(self.raw_body) > MAX_BODY:
            raise HttpError(413, "Request too large.")
        if self.raw_body and "application/json" not in self.headers.get("content-type", ""):
            raise HttpError(415, "Send JSON.")
        try:
            data = json.loads(self.raw_body or "{}")
        except ValueError:
            raise HttpError(400, "Invalid JSON.")
        if not isinstance(data, dict):
            raise HttpError(400, "Invalid JSON.")
        return data


class App:
    def __init__(self, store, cloud, clock=None, env=os.environ):
        self.store, self.cloud, self.clock, self.env = store, cloud, clock or ops.Clock(), env
        try:
            self.auth = auth.Config(env.get("DASHBOARD_USERNAME"), env.get("DASHBOARD_PASSWORD"),
                                    env.get("DASHBOARD_SESSION_SECRET"))
        except ValueError:
            self.auth = None  # fail closed: nothing behind login works
        self.function_arn = env.get("FUNCTION_ARN")
        self.own_origin = None  # derived from the Host header (the Function URL); dev_server overrides it
        self._settings_cache = (0.0, None)

    # --- HTTP -------------------------------------------------------------------------------
    def http(self, event):
        req = Request(event)
        if self.own_origin:
            req.origin = self.own_origin
        try:
            if not req.path.startswith("/api/"):
                return self.static(req)
            return self.api(req)
        except HttpError as e:
            return json_response(e.status, {"error": e.message}, headers=e.headers)
        except Exception as e:  # noqa: BLE001 - never leak internals to the browser
            print(f"ERROR {req.method} {req.path}: {e.__class__.__name__}: {e}")
            return json_response(500, {"error": "Something went wrong on the server."})

    def static(self, req):
        if req.method not in ("GET", "HEAD") or req.path not in ASSETS:
            return {"statusCode": 404, "headers": {"Content-Type": "text/plain", **SECURITY_HEADERS}, "body": "Not found"}
        name, content_type = ASSETS[req.path]
        with open(os.path.join(FRONTEND, name), encoding="utf-8") as f:
            body = f.read()
        cache = "no-cache" if name == "index.html" else "public, max-age=300"
        return {"statusCode": 200, "headers": {"Content-Type": content_type, "Cache-Control": cache, **SECURITY_HEADERS},
                "body": body}

    def session(self, req):
        if self.auth is None:
            raise HttpError(503, "Dashboard login is not configured.")
        s = auth.verify(self.auth, auth.token_from_cookies(req.cookies), self.clock.now(), self.store)
        if s is None:
            raise HttpError(401, "Please log in.")
        if req.method != "GET":
            fetch_site = req.headers.get("sec-fetch-site")
            if (fetch_site and fetch_site not in ("same-origin", "none")) or \
                    not auth.csrf_ok(s, req.headers.get("x-csrf-token"), req.headers.get("origin"), req.origin):
                raise HttpError(403, "Request blocked (CSRF check failed). Reload the page.")
        return s

    def api(self, req):
        path, m = req.path.rstrip("/"), req.method
        if len(req.raw_body) > MAX_BODY:
            raise HttpError(413, "Request too large.")
        if path == "/api/login" and m == "POST":
            return self.login(req)
        if path == "/api/session" and m == "GET":
            s = self.session(req)
            return json_response(200, {"authenticated": True, "user": self.auth.username, "csrf": s["csrf"],
                                       "expiresAt": schedules.iso(s["exp"])})
        s = self.session(req)
        routes = [
            ("POST", r"/api/logout", lambda a: self.logout(s)),
            ("GET", r"/api/status", lambda a: self.status()),
            ("POST", r"/api/server/start", lambda a: self.server_start(req)),
            ("POST", r"/api/server/stop", lambda a: self.server_stop(req)),
            ("GET", r"/api/operations/(?P<id>[\w:\-]+)", lambda a: self.operation(a["id"])),
            ("GET", r"/api/schedules", lambda a: self.schedules()),
            ("POST", r"/api/schedules/(?P<key>\w+)/skip", lambda a: self.schedule_skip(req, a["key"], True)),
            ("POST", r"/api/schedules/(?P<key>\w+)/unskip", lambda a: self.schedule_skip(req, a["key"], False)),
            ("GET", r"/api/backups", lambda a: self.backup_list(req)),
            ("POST", r"/api/backups", lambda a: self.backup_create()),
            ("GET", r"/api/backups/(?P<id>[\w\-]+)/download", lambda a: self.backup_download(a["id"])),
            ("GET", r"/api/backups/(?P<id>[\w\-]+)", lambda a: self.backup_get(a["id"])),
            ("GET", r"/api/logs", lambda a: self.logs(req)),
            ("GET", r"/api/metrics", lambda a: json_response(200, metrics.fetch(self.store, self.cloud, self.clock.now()))),
            ("GET", r"/api/settings", lambda a: self.settings()),
        ]
        path_matched = False
        for method, pattern, fn in routes:
            match = re.fullmatch(pattern, path)
            if match:
                path_matched = True
                if method == m:
                    return fn(match.groupdict())
        raise HttpError(405 if path_matched else 404, "Method not allowed." if path_matched else "Not found.")

    # --- Auth -------------------------------------------------------------------------------
    def login(self, req):
        if self.auth is None:
            raise HttpError(503, "Dashboard login is not configured.")
        origin = req.headers.get("origin")
        if origin and origin != req.origin:
            raise HttpError(403, "Request blocked.")
        now = self.clock.now()
        wait = auth.throttled(self.store, req.ip, now)
        if wait:
            raise HttpError(429, f"Too many failed logins. Try again in {wait // 60 + 1} min.", {"Retry-After": str(wait)})
        data = req.json()
        if not auth.check_credentials(self.auth, data.get("username"), data.get("password")):
            auth.record_failure(self.store, req.ip, now)
            raise HttpError(401, "Wrong username or password.")
        token, s = auth.issue(self.auth, now)
        return json_response(200, {"authenticated": True, "user": self.auth.username, "csrf": s["csrf"],
                                    "expiresAt": schedules.iso(s["exp"])}, cookies=[auth.session_cookie(token)])

    def logout(self, s):
        auth.revoke(self.store, s)
        return json_response(200, {"authenticated": False}, cookies=[auth.clear_cookie()])

    # --- Overview ---------------------------------------------------------------------------
    def readiness(self, instance, snapshot, now):
        if not instance:
            return {"state": "missing", "label": "Not found", "detail": "No server machine found."}
        state = instance["state"]
        if state != "running":
            labels = {"stopped": "Stopped", "stopping": "Stopping", "pending": "Starting machine", "shutting-down": "Shutting down"}
            return {"state": state, "label": labels.get(state, state.capitalize()), "detail": f"The server machine is {state}."}
        inspected = settings.parse_time((snapshot or {}).get("inspectedAt"))
        if inspected is None or inspected < instance["launchTime"]:
            booted = now - instance["launchTime"]
            if booted < 15 * 60:
                return {"state": "starting", "label": "Starting", "detail": "Machine is up; waiting for the game server to report."}
            return {"state": "unknown", "label": "Not reporting", "detail": "The game server hasn't reported since the machine started."}
        if now - inspected > SNAPSHOT_FRESH_SECONDS:
            return {"state": "unknown", "label": "Not reporting",
                    "detail": f"No report from the game server for {int((now - inspected) // 60)} min."}
        container = snapshot.get("container") or {}
        log = snapshot.get("log") or {}
        if not container.get("running"):
            return {"state": "offline", "label": "Game offline", "detail": f"Game container is {container.get('status') or 'missing'}."}
        if (snapshot.get("serverProcess") or {}).get("running") and log.get("run") == "current" and log.get("hostOnline"):
            return {"state": "ready", "label": "Ready", "detail": "The game server is online and accepting players."}
        return {"state": "starting", "label": "Starting", "detail": "Updating the game or loading the world."}

    def status(self):
        now = self.clock.now()
        errors, out = [], {"checkedAt": schedules.iso(now)}
        instance = snapshot = None
        try:
            instance = self.cloud.instance()
        except Exception:  # noqa: BLE001
            errors.append("Could not read the server machine's state from AWS.")
        try:
            snapshot = self.cloud.read_json(SNAPSHOT_KEY)
        except Exception:  # noqa: BLE001
            errors.append("Could not read the latest report from the game server.")
        out["instance"] = None if not instance else {
            "state": instance["state"], "type": instance["type"], "launchTime": schedules.iso(instance["launchTime"]),
            # No Elastic IP: a new public IP on every start, none while stopped. The game's default
            # query port is left out, so players only need the IP.
            "address": instance.get("publicIp")}
        out["server"] = self.readiness(instance, snapshot, now) if instance or not errors else \
            {"state": "unknown", "label": "Unknown", "detail": "Could not determine the state."}
        out["serverName"] = ((snapshot or {}).get("config") or {}).get("name")
        out["reportedAt"] = (snapshot or {}).get("inspectedAt")
        try:
            sched = {k: schedules.describe(self.cloud, k, now, self.function_arn) for k in schedules.SCHEDULES}
            pending = [s for s in schedules.list_skips(self.store, now) if s["status"] == "pending"]
            for k in ("stop",):
                sched[k]["skipNext"] = any(p["schedule"] == k and p["occurrence"] == sched[k].get("next") for p in pending)
            out["schedules"] = sched
        except Exception:  # noqa: BLE001
            errors.append("Could not read the schedules.")
        try:
            out["backups"] = backups.health(self.store, now, (instance or {}).get("state"))
        except Exception:  # noqa: BLE001
            errors.append("Could not read the backup history.")
        out["operation"] = ops.latest_op(self.store)
        try:
            out["recentErrors"] = logs.recent_errors(self.cloud, now) if (instance or {}).get("state") == "running" else []
        except Exception:  # noqa: BLE001
            out["recentErrors"] = None
            errors.append("Could not read recent logs.")
        out["errors"] = errors
        return json_response(200, out)

    # --- Power ------------------------------------------------------------------------------
    def server_start(self, req):
        if req.json().get("confirm") != "start":
            raise HttpError(400, "Confirmation missing.")
        op = ops.new_op(self.store, "start", self.clock.now())
        ok, msg = ops.start_server(self.store, self.cloud, self.clock, op_id=op["id"])
        return json_response(202 if ok else 409, {"operation": ops.public_op(self.store.get("OP", op["id"])), "message": msg})

    def server_stop(self, req):
        if req.json().get("confirm") != "stop":
            raise HttpError(400, "Confirmation missing.")
        instance = self.cloud.instance()
        if not instance or instance["state"] != "running":
            raise HttpError(409, f"The server machine is {(instance or {}).get('state', 'missing')}.")
        if self.store.get("LOCK", "power") and self.store.get("LOCK", "power")["expiresAt"] > self.clock.now():
            raise HttpError(409, "Another start or stop is already in progress.")
        op = ops.new_op(self.store, "stop", self.clock.now())
        try:
            self.cloud.run_async({"task": "stop", "op": op["id"]})
        except Exception:  # noqa: BLE001
            ops._op_update(self.store, op["id"], self.clock.now(), "failed", message="Could not start the stop operation.")
            raise HttpError(502, "Could not start the stop operation.")
        return json_response(202, {"operation": ops.public_op(self.store.get("OP", op["id"]))})

    def operation(self, op_id):
        op = self.store.get("OP", op_id)
        if not op:
            raise HttpError(404, "Operation not found.")
        return json_response(200, ops.public_op(op))

    # --- Schedules --------------------------------------------------------------------------
    def schedules(self):
        now = self.clock.now()
        data = {k: schedules.describe(self.cloud, k, now, self.function_arn) for k in schedules.SCHEDULES}
        for k in data:
            data[k]["recentRuns"] = schedules.recent_runs(self.store, k)
        return json_response(200, {"schedules": data, "skips": schedules.list_skips(self.store, now)[:20],
                                   "checkedAt": schedules.iso(now)})

    def _schedule(self, key):
        if key not in schedules.SCHEDULES:
            raise HttpError(404, "Unknown schedule.")
        return schedules.describe(self.cloud, key, self.clock.now(), self.function_arn)

    def schedule_skip(self, req, key, skip):
        info = self._schedule(key)
        if not info.get("skippable"):
            raise HttpError(400, "This schedule can't be skipped.")
        occurrence = req.json().get("occurrence")
        try:
            occurrence = schedules.normalize_occurrence(occurrence)
        except (AttributeError, ValueError, TypeError):
            raise HttpError(400, "Missing or invalid occurrence.")
        now = self.clock.now()
        if skip:
            if not info.get("enabled") or info.get("next") != occurrence:
                raise HttpError(409, "That isn't the next scheduled run any more. Reload and try again.")
            item = schedules.request_skip(self.store, key, occurrence, now)
            if item.get("status") != "pending":
                raise HttpError(409, f"That run was already {item.get('status')}.")
        elif not schedules.cancel_skip(self.store, key, occurrence, now):
            raise HttpError(409, "There is no pending skip for that run.")
        return json_response(200, {"skips": schedules.list_skips(self.store, now)[:20]})

    # --- Backups ----------------------------------------------------------------------------
    def backup_list(self, req):
        cursor = req.query.get("cursor") or None
        if cursor and not re.fullmatch(r"[\w\-]{1,64}", cursor):
            raise HttpError(400, "Invalid cursor.")
        items, nxt = backups.page(self.store, limit=15, cursor=cursor)
        return json_response(200, {"backups": items, "next": nxt})

    def backup_create(self):
        now = self.clock.now()
        try:
            item = backups.begin(self.store, self.cloud, "manual", now)
        except backups.NotRunning as e:
            raise HttpError(409, str(e))
        except backups.Busy as e:
            raise HttpError(409, str(e))
        try:
            self.cloud.run_async({"task": "backup", "id": item["id"]})
        except Exception:  # noqa: BLE001
            backups._fail(self.store, item["id"], now, "Could not start the backup worker.")
            self.store.release_lock("backup", item["id"])
            raise HttpError(502, "Could not start the backup.")
        return json_response(202, {"backup": backups.public(item)})

    def backup_get(self, bid):
        item = self.store.get("BACKUP", bid)
        if not item:
            raise HttpError(404, "Backup not found.")
        return json_response(200, {"backup": backups.public(item)})

    def backup_download(self, bid):
        item = self.store.get("BACKUP", bid)
        if not item or item.get("status") != "succeeded" or not item.get("downloadKey"):
            raise HttpError(404, "No downloadable file for that backup.")
        url = self.cloud.presign(item["downloadKey"], f"enshrouded-savegame-{bid}.tar.gz")
        return {"statusCode": 302, "headers": {"Location": url, "Cache-Control": "no-store", **SECURITY_HEADERS}, "body": ""}

    # --- Logs and settings ------------------------------------------------------------------
    def logs(self, req):
        try:
            data = logs.fetch(self.cloud, req.query.get("source", "game"), req.query.get("minutes", 30),
                              req.query.get("filter", ""), self.clock.now(), req.query.get("limit", 200))
        except logs.BadRequest as e:
            raise HttpError(400, str(e))
        return json_response(200, data)

    def settings(self):
        at, body = self._settings_cache
        now = self.clock.now()
        if body is None or now - at >= SETTINGS_CACHE_SECONDS:
            snapshot, rt = runtime.collect(self.cloud, SNAPSHOT_KEY)
            body = settings.build_model(snapshot, rt, now)
            self._settings_cache = (now, body)
        return json_response(200, body)

    # --- Tasks ------------------------------------------------------------------------------
    def task(self, event):
        kind = event.get("task")
        if kind == "scheduled":
            result = ops.scheduled(self.store, self.cloud, self.clock, event.get("schedule"), event.get("scheduledTime"))
            print(f"scheduled {event.get('schedule')} {event.get('scheduledTime')}: {result}")
            return {"result": result}
        if kind == "backup":
            item = self.store.get("BACKUP", str(event.get("id")))
            if not item or item["status"] != "in_progress" or item.get("commandId"):
                return {"result": "ignored"}  # duplicate delivery
            result = backups.run(self.store, self.cloud, item, self.clock)
            backups.apply_retention(self.store, self.cloud, self.clock.now())
            print(f"backup {item['id']}: {result['status']} {result.get('reason', '')}")
            return {"result": result["status"]}
        if kind == "stop":
            op = self.store.get("OP", str(event.get("op")))
            if not op or op["status"] != "queued":
                return {"result": "ignored"}
            ok, msg = ops.stop_server(self.store, self.cloud, self.clock, op_id=op["id"])
            print(f"stop {op['id']}: {msg}")
            return {"result": "done" if ok else "failed"}
        raise ValueError(f"unknown task {kind!r}")
