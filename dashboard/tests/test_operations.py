import unittest

from fakes import Browser, body, make_app


class Status(unittest.TestCase):
    def setUp(self):
        self.app, self.cloud, self.store, self.clock = make_app()
        self.b = Browser(self.app)

    def test_ready_needs_a_fresh_report_from_the_game(self):
        data = body(self.b.get("/api/status"))
        self.assertEqual(data["instance"]["state"], "running")
        self.assertEqual(data["server"]["state"], "ready")
        self.assertEqual(data["instance"]["address"], "203.0.113.10")  # default port left out

    def test_running_machine_is_not_ready_without_game_report(self):
        self.cloud.snapshot["inspectedAt"] = "2026-10-15T17:00:00Z"  # before the machine started (18:00)
        self.assertEqual(body(self.b.get("/api/status"))["server"]["state"], "unknown")
        self.cloud.snapshot["inspectedAt"] = "2026-10-15T18:59:00Z"
        self.cloud.snapshot["log"]["hostOnline"] = False
        self.assertEqual(body(self.b.get("/api/status"))["server"]["state"], "starting")
        self.cloud.snapshot["log"]["hostOnline"] = True
        self.cloud.snapshot["container"]["running"] = False
        self.assertEqual(body(self.b.get("/api/status"))["server"]["state"], "offline")

    def test_stopped(self):
        self.cloud.state = "stopped"
        data = body(self.b.get("/api/status"))
        self.assertEqual(data["server"]["state"], "stopped")
        self.assertEqual(data["recentErrors"], [])
        self.assertIsNone(data["instance"]["address"])  # no Elastic IP: the address is assigned at start

    def test_address_is_the_current_public_ip(self):
        self.cloud.instance = lambda: {"id": "i-0123", "state": "running", "type": "m6a.xlarge", "launchTime": 0,
                                       "publicIp": "198.51.100.7", "region": "sa-east-1", "securityGroups": []}
        self.assertEqual(body(self.b.get("/api/status"))["instance"]["address"], "198.51.100.7")

    def test_partial_failures_are_reported_not_fatal(self):
        self.cloud.fail.add("read_json")
        resp = self.b.get("/api/status")
        self.assertEqual(resp["statusCode"], 200)
        self.assertIn("Could not read the latest report from the game server.", body(resp)["errors"])


class Power(unittest.TestCase):
    def setUp(self):
        self.app, self.cloud, self.store, self.clock = make_app()
        self.b = Browser(self.app)

    def drain(self):
        while self.cloud.async_payloads:
            self.app.task(self.cloud.async_payloads.pop(0))

    def test_stop_requires_confirmation(self):
        self.assertEqual(self.b.post("/api/server/stop", {})["statusCode"], 400)
        self.assertEqual(self.cloud.async_payloads, [])

    def test_stop_flow(self):
        resp = self.b.post("/api/server/stop", {"confirm": "stop"})
        self.assertEqual(resp["statusCode"], 202)
        op_id = body(resp)["operation"]["id"]
        self.assertEqual(self.cloud.state, "running")  # nothing happens until the worker runs
        self.drain()
        op = body(self.b.get(f"/api/operations/{op_id}"))
        self.assertEqual(op["status"], "succeeded")
        self.assertIn("backup saved", op["message"])
        self.assertEqual(self.cloud.state, "stopping")
        backups = body(self.b.get("/api/backups"))["backups"]
        self.assertEqual((backups[0]["type"], backups[0]["status"], backups[0]["serverStopped"]), ("shutdown", "succeeded", True))

    def test_stop_worker_runs_once(self):
        self.b.post("/api/server/stop", {"confirm": "stop"})
        payload = self.cloud.async_payloads[0]
        self.drain()
        self.cloud.state = "running"
        self.assertEqual(self.app.task(payload)["result"], "ignored")
        self.assertEqual(self.cloud.calls.count("stop_instance"), 1)

    def test_stop_rejected_when_not_running(self):
        self.cloud.state = "stopped"
        self.assertEqual(self.b.post("/api/server/stop", {"confirm": "stop"})["statusCode"], 409)

    def test_concurrent_power_operations(self):
        self.store.acquire_lock("power", "someone-else", self.clock.now(), 600)
        self.assertEqual(self.b.post("/api/server/stop", {"confirm": "stop"})["statusCode"], 409)
        self.cloud.state = "stopped"
        self.assertEqual(self.b.post("/api/server/start", {"confirm": "start"})["statusCode"], 409)
        self.assertNotIn("start_instance", self.cloud.calls)

    def test_start(self):
        self.cloud.state = "stopped"
        resp = self.b.post("/api/server/start", {"confirm": "start"})
        self.assertEqual(resp["statusCode"], 202)
        self.assertEqual(body(resp)["operation"]["status"], "succeeded")
        self.assertEqual(self.cloud.state, "pending")
        self.assertEqual(self.b.post("/api/server/start", {})["statusCode"], 400)

    def test_start_ec2_error(self):
        self.cloud.state = "stopped"
        self.cloud.fail.add("start_instance")
        resp = self.b.post("/api/server/start", {"confirm": "start"})
        self.assertEqual(resp["statusCode"], 500)
        self.assertNotIn("arn:", resp["body"])
        op = body(self.b.get("/api/status"))["operation"]
        self.assertEqual(op["status"], "failed")
        self.assertIsNone(self.store.get("LOCK", "power"))  # lock released

    def test_stop_ec2_error_marks_operation_failed(self):
        self.cloud.fail.add("stop_instance")
        self.b.post("/api/server/stop", {"confirm": "stop"})
        with self.assertRaises(RuntimeError):
            self.drain()
        self.assertEqual(body(self.b.get("/api/status"))["operation"]["status"], "failed")

    def test_async_dispatch_failure(self):
        self.cloud.fail.add("run_async")
        self.assertEqual(self.b.post("/api/server/stop", {"confirm": "stop"})["statusCode"], 502)
        self.assertEqual(body(self.b.get("/api/status"))["operation"]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
