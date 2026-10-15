import json
import unittest

from fakes import T0, Browser, body, make_app

import logs

MS = 1000


class Logs(unittest.TestCase):
    def setUp(self):
        self.app, self.cloud, self.store, self.clock = make_app()
        self.b = Browser(self.app)
        now = int(T0 * MS)
        self.cloud.events = [
            {"stream": "game", "t": now - 3 * 3600 * MS, "m": "[I 00:00:01,000] too old"},
            {"stream": "game", "t": now - 120 * MS, "m": "[I 00:10:00,000] [Session] 'HostOnline' (up)!"},
            {"stream": "game", "t": now - 60 * MS, "m": "[E 00:11:00,000] [ecss] Could not find component"},
            {"stream": "game", "t": now - 30 * MS, "m": "[W 00:11:30,000] [session] Remove player"},
            {"stream": "container", "t": now - 10 * MS,
             "m": json.dumps({"log": "\x1b[0mSteamCMD update attempt 1 failed, retrying...\n", "stream": "stdout",
                              "time": "2026-10-15T18:59:50.1Z"})},
        ]

    def test_recent_game_lines_with_levels(self):
        data = body(self.b.get("/api/logs", query="source=game&minutes=30"))
        self.assertEqual([l["level"] for l in data["lines"]], ["info", "error", "warn"])
        self.assertNotIn("too old", json.dumps(data))

    def test_filter(self):
        data = body(self.b.get("/api/logs", query="source=game&minutes=30&filter=HOSTONLINE"))
        self.assertEqual(len(data["lines"]), 1)

    def test_container_json_and_ansi(self):
        line = body(self.b.get("/api/logs", query="source=container"))["lines"][0]
        self.assertEqual(line["text"], "SteamCMD update attempt 1 failed, retrying...")
        self.assertEqual(line["at"], "2026-10-15T18:59:50.1Z")

    def test_bounds(self):
        self.b.get("/api/logs", query="source=game&minutes=99999&limit=99999")
        call = [c for c in self.cloud.calls if isinstance(c, tuple)][-1]
        _, stream, start, end, max_events = call
        self.assertEqual(stream, "game")
        self.assertEqual(end - start, logs.MAX_MINUTES * 60 * MS)
        self.assertEqual(max_events, logs.MAX_FETCHED)
        data = body(self.b.get("/api/logs", query="source=game&minutes=0&limit=1"))
        self.assertEqual((data["minutes"], len(data["lines"])), (1, 1))

    def test_only_named_sources(self):
        for q in ("source=/aws/lambda/other", "source=*", "source=game&minutes=abc", "source=game&filter=" + "x" * 101):
            self.assertEqual(self.b.get("/api/logs", query=q)["statusCode"], 400, q)

    def test_recent_errors_on_overview(self):
        errors = body(self.b.get("/api/status"))["recentErrors"]
        self.assertEqual([e["text"] for e in errors], ["[E 00:11:00,000] [ecss] Could not find component"])

    def test_truncation_keeps_newest(self):
        now = int(T0 * MS)
        self.cloud.events = [{"stream": "game", "t": now - i * MS, "m": f"[I] line {i}"} for i in range(600, 0, -1)]
        data = body(self.b.get("/api/logs", query="source=game&limit=500"))
        self.assertTrue(data["truncated"])
        self.assertEqual(data["lines"][-1]["text"], "[I] line 1")
        self.assertEqual(len(data["lines"]), 500)


if __name__ == "__main__":
    unittest.main()
