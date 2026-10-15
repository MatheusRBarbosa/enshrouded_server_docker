"""Turn an agent snapshot plus live AWS facts into the grouped settings model the page renders.

Pure functions only (no AWS, no I/O), so this is what the tests exercise.

Every row carries a status:
  applied     the running server reported this value itself (startup log, Steam)
  configured  read from enshrouded_server.json; the server doesn't echo it back
  pending     the file differs from what the running server uses; applies after a restart
  runtime     a fact about the running server or AWS, not a setting
  unknown     could not be determined
"""
import datetime
import math

import catalog
from sanitize import redact

STALE_AFTER_SECONDS = 600  # the agent reports every 2 minutes


def parse_time(value):
    if not value or value.startswith("0001-"):
        return None
    value = value.replace("Z", "+00:00")
    if "." in value:  # docker: nanoseconds, more than fromisoformat accepts on older Pythons
        head, tail = value.split(".", 1)
        frac = "".join(ch for ch in tail if ch.isdigit())
        value = f"{head}.{frac[:6]}{tail[len(frac):]}"
    return datetime.datetime.fromisoformat(value).timestamp()


def iso(ts):
    return None if ts is None else datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- Formatting ------------------------------------------------------------------------

def _num(x):
    return f"{x:.6g}"


def split_camel(text):
    return catalog.humanize(text) if text and text[0].isupper() and any(c.islower() for c in text) else text


def fmt(value, kind):
    if value is None:
        return "Not set"
    if kind == "factor" and isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"{_num(value * 100)}%"
    if kind == "duration" and isinstance(value, (int, float)):
        return f"{_num(value / 60e9)} min"
    if kind == "onoff" or isinstance(value, bool):
        return "On" if value else "Off"
    if kind == "enum" and isinstance(value, str):
        return split_camel(value)
    if isinstance(value, list):
        return ", ".join(map(str, value)) if value else "None"
    if isinstance(value, float):
        return _num(value)
    return str(value)


def same(a, b):
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return math.isclose(a, b, rel_tol=1e-5, abs_tol=1e-9)
    return a == b


def row(spec, value, status, note=None, source=None, key=None, pending=None, kind=None, change=None):
    kind = kind or spec.get("fmt", "text")
    r = {
        "key": key or spec["key"],
        "label": spec["label"],
        "description": spec.get("description"),
        "section": spec.get("section"),
        "value": value,
        "display": fmt(value, kind),
        "status": status,
        "statusNote": note,
        "source": source,
        "change": change or spec.get("change", catalog.RESTART),
    }
    if pending is not None:
        r["pending"] = {"value": pending, "display": fmt(pending, kind)}
    default = spec.get("default", catalog.NO_DEFAULT)
    if default is not catalog.NO_DEFAULT and status != "unknown":
        r["default"] = {"value": default, "display": fmt(default, kind), "isDefault": same(value, default)}
    return r


def runtime_row(key, label, value, source, note=None, description=None, display=None, status="runtime"):
    r = row({"key": key, "label": label, "description": description}, value, status if value is not None else "unknown",
            note=note if value is not None else (note or "Not available"), source=source, change=catalog.READ_ONLY)
    if display is not None and value is not None:
        r["display"] = display
    return r


# --- Model -------------------------------------------------------------------------------

class Context:
    def __init__(self, snapshot, runtime, now):
        self.snap = snapshot or {}
        self.rt = runtime or {}
        self.now = now
        self.config = redact(self.snap.get("config") or {})
        loaded = self.snap.get("loadedConfig") or {}
        self.loaded = redact(loaded.get("config")) if loaded.get("config") else None
        self.changed = self.snap.get("configChangedSinceStart")
        self.process = self.snap.get("serverProcess") or {}
        self.running = bool(self.process.get("running"))
        log = self.snap.get("log") or {}
        self.log_run = log.get("run")
        # Only trust the log for the run it describes: this one, or the last one if none is running
        usable = self.log_run == "current" or (not self.running and self.log_run == "previous")
        self.log = log if usable else {}
        self.inspected = parse_time(self.snap.get("inspectedAt"))
        self.age = None if self.inspected is None else max(0, now - self.inspected)
        self.instance = self.rt.get("instance") or {}
        self.instance_state = self.instance.get("state")
        self.live = bool(self.snap) and self.instance_state in ("running", None) and self.age is not None \
            and self.age <= STALE_AFTER_SECONDS

    def run_word(self):
        return "current server run" if self.log_run == "current" else "last server run"


_LOOKUP = object()


def top_level_row(ctx, spec, value, loaded_value=_LOOKUP):
    """Status of a key the server reads from the file but doesn't echo back. loaded_value is the
    same setting in the config the server loaded (looked up by key unless given)."""
    key = spec["key"]
    if spec.get("change") == catalog.IN_GAME:
        return row(spec, value, "configured", "Kept up to date by the server while it runs.", "enshrouded_server.json")
    if not ctx.running:
        return row(spec, value, "configured", "The server isn't running; this applies when it starts.", "enshrouded_server.json")
    if ctx.changed is False:
        return row(spec, value, "configured", "Loaded at server start; the file hasn't changed since.", "enshrouded_server.json")
    if ctx.loaded is not None:
        running = ctx.loaded.get(key) if loaded_value is _LOOKUP else loaded_value
        if same(running, value):
            return row(spec, value, "configured", "Loaded at server start.", "enshrouded_server.json")
        return row(spec, running, "pending", "Changed in the file after the server started; applies after a restart.",
                   "enshrouded_server.json", pending=value)
    return row(spec, value, "pending", "The file changed after the server started, so this may not be active until a restart.",
               "enshrouded_server.json")


def gameplay_rows(ctx):
    rows = []
    file_preset = ctx.config.get("gameSettingsPreset")
    log_preset = ctx.log.get("preset")
    file_gs = ctx.config.get("gameSettings") or {}
    log_gs = ctx.log.get("gameSettings") or {}
    spec = catalog.TOP_LEVEL["gameSettingsPreset"]
    if log_preset is not None:
        if file_preset is not None and file_preset != log_preset:
            rows.append(row(spec, log_preset, "pending", "The file sets a different preset; applies after a restart.",
                            "Server startup log", pending=file_preset))
        else:
            rows.append(row(spec, log_preset, "applied", f"Reported by the server at the start of the {ctx.run_word()}.",
                            "Server startup log"))
    elif file_preset is not None:
        rows.append(top_level_row(ctx, spec, file_preset))
    file_rules = file_preset == "Custom"

    keys = list(catalog.GAME_SETTINGS) + [k for k in list(log_gs) + list(file_gs) if k not in catalog.GAME_SETTINGS]
    seen = set()
    for key in keys:
        if key in seen or (key not in log_gs and key not in file_gs):
            continue
        seen.add(key)
        spec = catalog.GAME_SETTINGS.get(key) or catalog.unknown_setting(key, "gameplay", "Other")
        path = f"gameSettings.{key}"
        if key in log_gs:
            applied = log_gs[key]
            if key in file_gs and not same(file_gs[key], applied):
                if file_rules:
                    rows.append(row(spec, applied, "pending", "Changed in the file; applies after a restart.",
                                    "Server startup log", key=path, pending=file_gs[key]))
                else:
                    rows.append(row(spec, applied, "applied",
                                    f"The {log_preset or file_preset} preset overrides the file value {fmt(file_gs[key], spec['fmt'])}.",
                                    "Server startup log", key=path))
                continue
            rows.append(row(spec, applied, "applied", f"Reported by the server at the start of the {ctx.run_word()}.",
                            "Server startup log", key=path))
        else:
            loaded_gs = (ctx.loaded or {}).get("gameSettings") or {}
            r = top_level_row(ctx, dict(spec, key=path), file_gs[key], loaded_gs.get(key))
            if not file_rules and file_preset:
                r["statusNote"] = f"Ignored while the preset is {file_preset}."
            rows.append(r)
    order = {name: i for i, name in enumerate(catalog.SECTIONS)}
    rows.sort(key=lambda r: order.get(r["section"], len(order)))
    return rows


def group_permissions(config, name):
    for g in (config or {}).get("userGroups") or []:
        if isinstance(g, dict) and g.get("name") == name:
            perms = {k: g.get(k) for k, _ in catalog.GROUP_PERMISSIONS if k in g}
            perms["reservedSlots"] = g.get("reservedSlots", 0)
            return perms
    return None


def players_rows(ctx):
    rows = []
    for key in ("slotCount",):
        if key in ctx.config:
            rows.append(top_level_row(ctx, catalog.TOP_LEVEL[key], ctx.config[key]))
    groups = [g for g in ctx.config.get("userGroups") or [] if isinstance(g, dict)]
    if groups:
        open_groups = [g.get("name", "?") for g in groups if not g.get("passwordSet")]
        if open_groups:
            text = f"No password for: {', '.join(open_groups)}"
        else:
            text = f"Yes, all {len(groups)} groups need a password"
        rows.append(runtime_row("userGroups[].password", "Password protection", not open_groups, "enshrouded_server.json",
                                description="Each group has its own password; the one you enter decides your permissions. "
                                            "Passwords are never shown here. Ask an admin.",
                                display=text, status="configured"))
        rows[-1]["change"] = catalog.RESTART
        for g in groups:
            name = g.get("name", "?")
            allowed = [label for k, label in catalog.GROUP_PERMISSIONS if g.get(k)]
            denied = [label for k, label in catalog.GROUP_PERMISSIONS if k in g and not g.get(k)]
            perms = group_permissions(ctx.config, name)
            spec = {"key": f"userGroups[name={name}]", "label": f"{name} group", "fmt": "text",
                    "description": "Permissions for players who join with this group's password."}
            r = top_level_row(ctx, spec, perms, group_permissions(ctx.loaded, name))
            r["display"] = ", ".join(allowed) if allowed else "Play only"
            r["chips"] = ([{"label": "Password required" if g.get("passwordSet") else "No password", "on": bool(g.get("passwordSet"))}]
                          + [{"label": label, "on": True} for label in allowed]
                          + [{"label": label, "on": False} for label in denied])
            if perms["reservedSlots"]:
                r["chips"].append({"label": f"{perms['reservedSlots']} reserved slots", "on": True})
            r.pop("pending", None)
            default = catalog.GROUP_DEFAULTS.get(name)
            if default:
                is_default = all(same(perms.get(k), v) for k, v in default.items())
                r["default"] = {"value": default, "display": "Game default permissions", "isDefault": is_default}
            rows.append(r)
    for key in ("bannedAccountsCount", "bansCount", "enableTextChat", "enableVoiceChat", "voiceChatMode"):
        if key in ctx.config:
            rows.append(top_level_row(ctx, catalog.TOP_LEVEL[key], ctx.config[key]))
    return rows


def networking_rows(ctx):
    rows = []
    port = ctx.config.get("queryPort")
    eip = ctx.rt.get("elasticIp") or ctx.instance.get("publicIp")
    log_ip = ctx.log.get("publicIp")
    steam = ctx.rt.get("steam")
    if eip:
        note = "Elastic IP: stays the same across restarts."
        if log_ip and log_ip == eip:
            note += " The server reported the same address to Steam."
        address = f"{eip}:{port}" if port else eip
        rows.append(runtime_row("address", "Server address", address, "AWS EC2", note,
                                description="Copy this if you add the server by address."))
    if port is not None:
        spec = catalog.TOP_LEVEL["queryPort"]
        listed = [s for s in (steam or {}).get("servers", []) if s.get("addr", "").endswith(f":{port}")]
        if listed and ctx.running:
            rows.append(row(spec, port, "applied", "Steam lists the server on this port.", "Steam server list"))
        else:
            rows.append(top_level_row(ctx, spec, port))
    if "ip" in ctx.config:
        rows.append(top_level_row(ctx, catalog.TOP_LEVEL["ip"], ctx.config["ip"]))
    container = ctx.snap.get("container") or {}
    if container.get("ports") is not None:
        mapping = ", ".join(f"{p['host']} → {p['container']}" for p in container["ports"]) or "None"
        rows.append(runtime_row("container.ports", "Container port mapping", mapping, "Docker",
                                description="Host port forwarded into the game container."))
    fw = ctx.rt.get("firewall")
    if fw is not None:
        open_udp = fw.get("udpOpenToAll", [])
        text = (f"UDP {', '.join(map(str, open_udp))} open to everyone" if open_udp else "No UDP port open to everyone")
        if port is not None and port not in open_udp:
            text += f" (query port {port} is not!)"
        rows.append(runtime_row("securityGroup", "Firewall", open_udp, "AWS security group", display=text,
                                description="Inbound rules on the server's AWS security group."))
    if steam is not None and "listed" in steam:
        rows.append(runtime_row("steam.listed", "Steam server list", steam["listed"], "Steam Web API",
                                display="Listed" if steam["listed"] else "Not listed",
                                note="Checked against Steam's master server list for this address."))
    elif eip:
        rows.append(runtime_row("steam.listed", "Steam server list", None, "Steam Web API", note="Steam didn't answer."))
    return rows


def general_rows(ctx):
    rows = []
    if "name" in ctx.config:
        rows.append(top_level_row(ctx, catalog.TOP_LEVEL["name"], ctx.config["name"]))
    run_note = None if ctx.log_run == "current" else "From the last time the server ran."
    rows.append(runtime_row("log.svn", "Game version", ctx.log.get("svn"), "Server startup log", run_note,
                            description="The game's internal version number (SVN revision)."))
    version = ctx.log.get("version")
    rows.append(runtime_row("log.version", "Server build", version, "Server startup log", run_note,
                            display=f"{version[:12]} ({ctx.log.get('branch')})" if version and ctx.log.get("branch") else (version or "")[:12]))
    build = ctx.snap.get("build") or {}
    rows.append(runtime_row("steam.buildid", "Steam build ID", build.get("buildId"), "Steam app manifest",
                            "Up to date with the build Steam offered at the last start." if build.get("buildId") and build.get("buildId") == build.get("targetBuildId") else None,
                            description="The game updates every time the server starts."))
    rows.append(time_row("steam.lastUpdated", "Game files updated", build.get("lastUpdated"), "Steam app manifest"))
    rows.append(time_row("serverProcess.startedAt", "Server started", ctx.process.get("startedAt"), "Server process",
                         missing="Not running"))
    if "tags" in ctx.config:
        rows.append(top_level_row(ctx, catalog.TOP_LEVEL["tags"], ctx.config["tags"]))
    return rows


def time_row(key, label, value, source, missing=None, description=None):
    if value is None and missing:
        r = runtime_row(key, label, missing, source, description=description)
        r["value"] = None
        return r
    r = runtime_row(key, label, value, source, description=description)
    if value is not None:
        r["time"] = value
    return r


def advanced_rows(ctx):
    rows = []
    cf = ctx.snap.get("configFile") or {}
    if cf.get("path"):
        note = "The server confirmed loading this file at startup." if ctx.log.get("configParsed") else None
        rows.append(runtime_row("configFile.path", "Config file", cf["path"], "Agent", note,
                                description=f"On the host. Inside the container: {cf.get('containerPath')}",
                                status="configured"))
        rows[-1]["change"] = catalog.READ_ONLY
        rows.append(time_row("configFile.modifiedAt", "Config file changed", cf.get("modifiedAt"), "Agent",
                             description="The server rewrites the file when it starts."))
    for key in ("saveDirectory", "logDirectory"):
        if key in ctx.config:
            rows.append(top_level_row(ctx, catalog.TOP_LEVEL[key], ctx.config[key]))
    known = set(catalog.TOP_LEVEL) | {"gameSettings", "userGroups"}
    for key, value in ctx.config.items():
        if key not in known:
            rows.append(top_level_row(ctx, catalog.unknown_setting(key, "advanced"), value))
    c = ctx.snap.get("container") or {}
    if c.get("exists"):
        rows.append(runtime_row("container.status", "Container", c.get("status"), "Docker",
                                description=f"Image {c.get('image')}, restart policy {c.get('restartPolicy')}."))
    elif c.get("exists") is False:
        rows.append(runtime_row("container.status", "Container", "missing", "Docker"))
    if ctx.instance:
        rows.append(runtime_row("instance", "Server machine", f"{ctx.instance.get('type')} · {ctx.instance.get('state')}",
                                "AWS EC2", description=f"Region {ctx.instance.get('region')}."))
    return rows


def server_status(ctx):
    c = ctx.snap.get("container") or {}
    if ctx.instance_state and ctx.instance_state != "running":
        return {"state": "stopped", "label": f"Machine {ctx.instance_state}", "detail": "The server machine is not running."}
    if not ctx.snap:
        return {"state": "unknown", "label": "No data yet", "detail": "The server hasn't reported its settings yet."}
    if not ctx.live:
        return {"state": "unknown", "label": "Not reporting", "detail": f"No report from the server for {int(ctx.age // 60)} min."}
    if c.get("exists") is False or (c.get("exists") and not c.get("running")):
        return {"state": "offline", "label": "Offline", "detail": f"Game container is {c.get('status') or 'missing'}."}
    if ctx.running and ctx.log_run == "current" and ctx.log.get("hostOnline"):
        return {"state": "online", "label": "Online", "detail": "The server is up and accepting players."}
    return {"state": "starting", "label": "Starting", "detail": "Updating the game or loading the world."}


def notices(ctx, status):
    out = []
    if not ctx.snap:
        out.append({"level": "info", "text": "No settings snapshot yet. The server sends one every 2 minutes once the dashboard agent is installed."})
        return out
    if not ctx.live:
        when = ctx.snap.get("inspectedAt")
        out.append({"level": "warn", "text": f"Showing the last settings read from the server, at {when}. "
                                             "They may not reflect changes made since then."})
    if any(r for r in ctx.snap.get("errors") or []):
        out.append({"level": "warn", "text": "Some settings could not be read: " + "; ".join(ctx.snap["errors"])})
    for err in ctx.rt.get("errors") or []:
        out.append({"level": "warn", "text": err})
    return out


def build_model(snapshot, runtime, now, checked_at=None):
    snapshot = redact(snapshot) if snapshot else None
    ctx = Context(snapshot, runtime, now)
    status = server_status(ctx)
    groups = []
    if snapshot:
        builders = {"general": general_rows, "gameplay": gameplay_rows, "players": players_rows,
                    "networking": networking_rows, "advanced": advanced_rows}
        for gid, title in catalog.GROUPS:
            rows = [r for r in builders[gid](ctx) if r]
            if rows:
                groups.append({"id": gid, "title": title, "settings": rows})
    pending = sum(1 for g in groups for r in g["settings"] if r["status"] == "pending")
    model = {
        "schema": 1,
        "checkedAt": iso(checked_at if checked_at is not None else now),
        "inspectedAt": ctx.snap.get("inspectedAt"),
        "live": ctx.live,
        "server": {
            "name": ctx.config.get("name"),
            "address": next((r["display"] for g in groups for r in g["settings"] if r["key"] == "address"), None),
            "status": status,
        },
        "pendingRestart": pending,
        "notices": notices(ctx, status),
        "groups": groups,
    }
    return redact(model)
