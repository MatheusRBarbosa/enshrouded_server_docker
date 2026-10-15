import threading
import unittest

from fakes import T0, Browser, body, make_app

import ops
import schedules
import store as store_mod

STOP_NEXT = "2026-10-16T03:00:00Z"   # 00:00 in São Paulo (UTC-3)
STOP_AFTER = "2026-10-17T03:00:00Z"


class Cron(unittest.TestCase):
    def test_daily_in_sao_paulo(self):
        self.assertEqual(schedules.iso(schedules.next_occurrences("cron(0 0 * * ? *)", "America/Sao_Paulo", T0)[0]), STOP_NEXT)
        self.assertEqual(schedules.iso(schedules.next_occurrences("cron(0 10 * * ? *)", "America/Sao_Paulo", T0)[0]), "2026-10-16T13:00:00Z")

    def test_strictly_after_and_several(self):
        at = schedules.parse_iso(STOP_NEXT)
        nxt = schedules.next_occurrences("cron(0 0 * * ? *)", "America/Sao_Paulo", at, 2)
        self.assertEqual([schedules.iso(t) for t in nxt], ["2026-10-17T03:00:00Z", "2026-10-18T03:00:00Z"])

    def test_hourly(self):
        self.assertEqual(schedules.iso(schedules.next_occurrences("cron(30 * * * ? *)", "UTC", T0)[0]), "2026-10-15T19:30:00Z")

    def test_unsupported(self):
        for expr in ("rate(1 hour)", "cron(0 0 ? * MON *)", "cron(0 0 1 * ? *)", "at(2026-10-16T00:00:00)", None):
            self.assertIsNone(schedules.parse_cron(expr), expr)

    def test_fixed_offset_fallback(self):
        tz = schedules._FIXED_OFFSETS["America/Sao_Paulo"]
        self.assertEqual(tz, -3)


class Skips(unittest.TestCase):
    def setUp(self):
        self.store = store_mod.MemoryStore()

    def test_skip_applies_only_to_its_occurrence_and_schedule(self):
        schedules.request_skip(self.store, "stop", STOP_NEXT, T0)
        self.assertFalse(schedules.consume_skip(self.store, "backup", STOP_NEXT, T0))
        self.assertFalse(schedules.consume_skip(self.store, "stop", STOP_AFTER, T0))
        self.assertTrue(schedules.consume_skip(self.store, "stop", STOP_NEXT, T0))

    def test_retry_of_same_run_stays_skipped(self):
        schedules.request_skip(self.store, "stop", STOP_NEXT, T0)
        self.assertTrue(schedules.consume_skip(self.store, "stop", STOP_NEXT, T0))
        self.assertTrue(schedules.consume_skip(self.store, "stop", STOP_NEXT, T0 + 60))
        item = self.store.get("SKIP", f"stop#{STOP_NEXT}")
        self.assertEqual((item["status"], item["consumedAt"]), ("consumed", schedules.iso(T0)))

    def test_concurrent_consumers(self):
        schedules.request_skip(self.store, "stop", STOP_NEXT, T0)
        results = []
        threads = [threading.Thread(target=lambda: results.append(schedules.consume_skip(self.store, "stop", STOP_NEXT, T0)))
                   for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(results, [True] * 8)

    def test_cancel_and_rearm(self):
        schedules.request_skip(self.store, "stop", STOP_NEXT, T0)
        self.assertTrue(schedules.cancel_skip(self.store, "stop", STOP_NEXT, T0))
        self.assertFalse(schedules.consume_skip(self.store, "stop", STOP_NEXT, T0))
        self.assertFalse(schedules.cancel_skip(self.store, "stop", STOP_NEXT, T0))
        self.assertEqual(schedules.request_skip(self.store, "stop", STOP_NEXT, T0)["status"], "pending")

    def test_consumed_skip_cannot_be_cancelled_or_reset(self):
        schedules.request_skip(self.store, "stop", STOP_NEXT, T0)
        schedules.consume_skip(self.store, "stop", STOP_NEXT, T0)
        self.assertFalse(schedules.cancel_skip(self.store, "stop", STOP_NEXT, T0))
        self.assertEqual(schedules.request_skip(self.store, "stop", STOP_NEXT, T0)["status"], "consumed")

    def test_listing_marks_unused_past_skips_expired(self):
        schedules.request_skip(self.store, "stop", STOP_NEXT, T0)
        later = schedules.parse_iso(STOP_NEXT) + schedules.SKIP_GRACE_SECONDS + 1
        self.assertEqual(schedules.list_skips(self.store, later)[0]["status"], "expired")


class RunClaims(unittest.TestCase):
    def setUp(self):
        self.store = store_mod.MemoryStore()

    def test_duplicate_delivery(self):
        self.assertEqual(schedules.claim_run(self.store, "stop", STOP_NEXT, T0), "new")
        self.assertEqual(schedules.claim_run(self.store, "stop", STOP_NEXT, T0 + 5), "duplicate")
        schedules.finish_run(self.store, "stop", STOP_NEXT, "done", T0 + 10)
        self.assertEqual(schedules.claim_run(self.store, "stop", STOP_NEXT, T0 + 20), "duplicate")

    def test_crashed_run_can_be_taken_over_once(self):
        schedules.claim_run(self.store, "stop", STOP_NEXT, T0)
        later = T0 + schedules.RUN_LEASE_SECONDS + 1
        self.assertEqual(schedules.claim_run(self.store, "stop", STOP_NEXT, later), "new")
        self.assertEqual(schedules.claim_run(self.store, "stop", STOP_NEXT, later), "duplicate")

    def test_failed_run_can_be_retried(self):
        schedules.claim_run(self.store, "stop", STOP_NEXT, T0)
        schedules.finish_run(self.store, "stop", STOP_NEXT, "failed", T0 + 1)
        self.assertEqual(schedules.claim_run(self.store, "stop", STOP_NEXT, T0 + 60), "new")


class ScheduledRuns(unittest.TestCase):
    def setUp(self):
        self.app, self.cloud, self.store, self.clock = make_app()

    def run_task(self, key, when):
        return self.app.task({"task": "scheduled", "schedule": key, "scheduledTime": when})["result"]

    def test_stop_backs_up_then_stops(self):
        self.assertEqual(self.run_task("stop", STOP_NEXT), "done")
        cmd = list(self.cloud.commands.values())[0]
        self.assertEqual((cmd["type"], cmd["stop"]), ("shutdown", True))
        self.assertEqual(self.cloud.state, "stopping")
        self.assertLess(self.cloud.calls.index("send_backup"), self.cloud.calls.index("stop_instance"))

    def test_duplicate_stop_delivery_acts_once(self):
        self.run_task("stop", STOP_NEXT)
        self.cloud.state = "running"
        self.assertEqual(self.run_task("stop", STOP_NEXT), "duplicate")
        self.assertEqual(self.cloud.calls.count("stop_instance"), 1)

    def test_skipped_stop_does_nothing_and_next_night_still_stops(self):
        schedules.request_skip(self.store, "stop", STOP_NEXT, T0)
        self.assertEqual(self.run_task("stop", STOP_NEXT), "skipped")
        self.assertNotIn("stop_instance", self.cloud.calls)
        self.assertEqual(schedules.recent_runs(self.store, "stop")[0]["result"], "skipped")
        self.assertEqual(self.run_task("stop", STOP_AFTER), "done")
        self.assertIn("stop_instance", self.cloud.calls)

    def test_stop_still_stops_when_backup_fails(self):
        self.cloud.backup_behavior = "fail"
        self.assertEqual(self.run_task("stop", STOP_NEXT), "done")
        self.assertEqual(self.cloud.state, "stopping")
        self.assertIn("backup failed", schedules.recent_runs(self.store, "stop")[0]["detail"])

    def test_stop_when_already_stopped_is_a_noop(self):
        self.cloud.state = "stopped"
        self.assertEqual(self.run_task("stop", STOP_NEXT), "done")
        self.assertNotIn("stop_instance", self.cloud.calls)

    def test_ec2_error_is_recorded_and_retry_allowed(self):
        self.cloud.fail.add("stop_instance")
        with self.assertRaises(RuntimeError):
            self.run_task("stop", STOP_NEXT)
        self.assertEqual(schedules.recent_runs(self.store, "stop")[0]["result"], "failed")
        self.cloud.fail.clear()
        self.assertEqual(self.run_task("stop", STOP_NEXT), "done")  # Lambda's async retry

    def test_no_automatic_start(self):
        for key in ("start", "reboot"):
            with self.assertRaises(ValueError):
                self.run_task(key, STOP_NEXT)
        self.assertNotIn("start", schedules.SCHEDULES)


class ScheduleApi(unittest.TestCase):
    def setUp(self):
        self.app, self.cloud, self.store, self.clock = make_app()
        self.b = Browser(self.app)

    def test_deployed_configuration_is_reported(self):
        data = body(self.b.get("/api/schedules"))["schedules"]
        self.assertEqual((data["stop"]["expression"], data["stop"]["timezone"], data["stop"]["next"]),
                         ("cron(0 0 * * ? *)", "America/Sao_Paulo", STOP_NEXT))
        self.assertEqual(sorted(data), ["backup", "stop"])
        self.assertTrue(data["stop"]["targetsDashboard"])

    def test_target_mismatch_is_visible(self):
        self.cloud.schedules["enshrouded-stop"]["Target"]["Arn"] = "arn:aws:lambda:us-east-2:1:function:other"
        self.assertFalse(body(self.b.get("/api/schedules"))["schedules"]["stop"]["targetsDashboard"])

    def test_schedules_cannot_be_turned_off_from_the_dashboard(self):
        self.assertEqual(self.b.post("/api/schedules/stop/enabled", {"enabled": False})["statusCode"], 404)
        self.assertEqual({k: v["State"] for k, v in self.cloud.schedules.items()},
                         {k: "ENABLED" for k in self.cloud.schedules})

    def test_schedule_disabled_in_aws_is_shown(self):
        self.cloud.schedules["enshrouded-stop"]["State"] = "DISABLED"
        data = body(self.b.get("/api/schedules"))["schedules"]
        self.assertEqual((data["stop"]["enabled"], data["stop"]["next"]), (False, None))
        self.assertTrue(data["backup"]["enabled"])

    def test_skip_and_unskip_next_stop(self):
        self.assertEqual(self.b.post("/api/schedules/stop/skip", {"occurrence": STOP_NEXT})["statusCode"], 200)
        self.assertTrue(body(self.b.get("/api/status"))["schedules"]["stop"]["skipNext"])
        self.assertEqual(self.b.post("/api/schedules/stop/unskip", {"occurrence": STOP_NEXT})["statusCode"], 200)
        self.assertFalse(body(self.b.get("/api/status"))["schedules"]["stop"]["skipNext"])
        self.assertEqual(self.b.post("/api/schedules/start/skip", {"occurrence": STOP_NEXT})["statusCode"], 404)

    def test_skip_rejects_stale_or_disabled(self):
        self.assertEqual(self.b.post("/api/schedules/stop/skip", {"occurrence": "2026-10-17T03:00:00Z"})["statusCode"], 409)
        self.cloud.schedules["enshrouded-stop"]["State"] = "DISABLED"
        self.assertEqual(self.b.post("/api/schedules/stop/skip", {"occurrence": STOP_NEXT})["statusCode"], 409)
        self.assertEqual(self.b.post("/api/schedules/backup/skip", {"occurrence": STOP_NEXT})["statusCode"], 400)
        self.assertEqual(self.b.post("/api/schedules/stop/skip", {"occurrence": "nope"})["statusCode"], 400)

    def test_manual_start_stop_never_touch_schedules(self):
        before = {k: dict(v) for k, v in self.cloud.schedules.items()}
        self.b.post("/api/server/stop", {"confirm": "stop"})
        for payload in self.cloud.async_payloads:
            self.app.task(payload)
        self.cloud.state = "stopped"
        self.b.post("/api/server/start", {"confirm": "start"})
        self.assertEqual({k: v["State"] for k, v in self.cloud.schedules.items()}, {k: v["State"] for k, v in before.items()})


if __name__ == "__main__":
    unittest.main()
