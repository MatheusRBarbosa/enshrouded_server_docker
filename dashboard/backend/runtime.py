"""Gather the snapshot and live facts (AWS, Steam) for the Server Settings section.

Each source fails independently: a failure becomes a short message in runtime["errors"] (never
the raw AWS error, which names the account and role) and the page shows the rest.
"""
import json
import urllib.request

STEAM_API = "https://api.steampowered.com/ISteamApps/GetServersAtAddress/v1/?addr={}"
# Dedicated servers register under the game's app id (1203620), not the server tool's (2278520)
APP_IDS = {1203620, 2278520}


def parse_steam_listing(data):
    servers = [{"addr": s.get("addr"), "gameport": s.get("gameport")}
               for s in data.get("response", {}).get("servers", [])
               if s.get("appid") in APP_IDS or str(s.get("gamedir", "")).lower() == "enshrouded"]
    return {"listed": bool(servers), "servers": servers}


def steam_listing(ip, timeout=3):
    with urllib.request.urlopen(STEAM_API.format(ip), timeout=timeout) as resp:
        return parse_steam_listing(json.load(resp))


def collect(cloud, snapshot_key):
    """(snapshot, runtime facts) for settings.build_model."""
    errors = []
    runtime = {"errors": errors}
    snapshot = None
    try:
        snapshot = cloud.read_json(snapshot_key)
    except Exception:  # noqa: BLE001
        errors.append("Could not read the latest settings snapshot.")
    groups = []
    try:
        instance = cloud.instance()
        if instance:
            groups = instance.pop("securityGroups", [])
        runtime["instance"] = instance
    except Exception:  # noqa: BLE001
        errors.append("Could not read the server machine's state from AWS.")
    try:
        ports = cloud.udp_open_to_all(groups)
        runtime["firewall"] = None if ports is None else {"udpOpenToAll": ports}
    except Exception:  # noqa: BLE001
        errors.append("Could not read the firewall rules from AWS.")
    ip = (runtime.get("instance") or {}).get("publicIp")
    if ip and (runtime.get("instance") or {}).get("state") == "running":
        try:
            runtime["steam"] = steam_listing(ip)
        except Exception:  # noqa: BLE001
            runtime["steam"] = {}
    return snapshot, runtime
