"""Shared test setup: import paths and a snapshot built by the real agent from fixtures/server."""
import datetime
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DASHBOARD = os.path.dirname(HERE)
sys.path[:0] = [os.path.join(DASHBOARD, "agent"), os.path.join(DASHBOARD, "backend")]

import enshrouded_inspect as agent  # noqa: E402

FIXTURE_SERVER = os.path.join(HERE, "fixtures", "server")
SNAPSHOT_FIXTURE = os.path.join(HERE, "fixtures", "snapshot.json")
# Matches "Current UTC date" in fixtures/server/logs/enshrouded_server.log
SERVER_START = datetime.datetime(2026, 10, 15, 18, 11, 30, tzinfo=datetime.timezone.utc).timestamp()
NOW = SERVER_START + 600
SECRETS = ["fixture-Admin-SECRET", "fixture-Friend-SECRET", "fixture-Guest-SECRET",
           "76561190000000001", "76561190000000002", "FixturePlayer"]
CONTAINER = {"exists": True, "status": "running", "running": True, "startedAt": "2026-10-15T18:09:54.799214346Z",
             "image": "enshrouded:local", "restartPolicy": "unless-stopped", "stopTimeout": 90,
             "ports": [{"container": "15637/udp", "host": "15637", "hostIp": "0.0.0.0"}],
             "mounts": [{"source": "/srv/enshrouded", "destination": "/home/steam/enshrouded"}]}
RUNNING = {
    "errors": [],
    "instance": {"state": "running", "type": "m6a.xlarge", "region": "us-east-2", "publicIp": "203.0.113.10"},
    "elasticIp": "203.0.113.10",
    "firewall": {"udpOpenToAll": [15637]},
    "steam": {"listed": True, "servers": [{"addr": "203.0.113.10:15637", "gameport": 15636}]},
}


class ServerDir:
    """A temporary copy of fixtures/server whose files can be edited per test."""

    def __init__(self):
        self.root = tempfile.mkdtemp()
        self.data = os.path.join(self.root, "srv")
        shutil.copytree(FIXTURE_SERVER, self.data)
        self.state = os.path.join(self.root, "state", "loaded-config.json")
        self.config_path = os.path.join(self.data, "enshrouded_server.json")
        self.touch_config(SERVER_START + 1)  # the server rewrites its config right after starting

    def touch_config(self, ts):
        os.utime(self.config_path, (ts, ts))

    def edit_config(self, fn, ts):
        with open(self.config_path) as f:
            cfg = json.load(f)
        fn(cfg)
        with open(self.config_path, "w") as f:
            json.dump(cfg, f)
        self.touch_config(ts)

    def snapshot(self, now=NOW, process_start=SERVER_START, container=CONTAINER):
        snap = agent.build_snapshot(self.data, now=now, process_start=process_start,
                                    container=container, state_file=self.state)
        return json.loads(json.dumps(snap).replace(self.data, "/srv/enshrouded"))

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


def fixture_snapshot():
    d = ServerDir()
    try:
        return d.snapshot()
    finally:
        d.cleanup()


if __name__ == "__main__":
    # Regenerate fixtures/snapshot.json (used by dev_server.py) after changing the agent
    with open(SNAPSHOT_FIXTURE, "w") as f:
        json.dump(fixture_snapshot(), f, indent=1, sort_keys=True)
        f.write("\n")
    print(f"wrote {SNAPSHOT_FIXTURE}")
