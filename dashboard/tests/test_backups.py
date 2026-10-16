import unittest

from fakes import T0, Browser, body, make_app

import backups
import schedules

HOUR = 3600


class ManualBackup(unittest.TestCase):
    def setUp(self):
        self.app, self.cloud, self.store, self.clock = make_app()
        self.b = Browser(self.app)

    def create_and_run(self):
        resp = self.b.post("/api/backups")
        self.assertEqual(resp["statusCode"], 202)
        bid = body(resp)["backup"]["id"]
        self.assertEqual(body(self.b.get(f"/api/backups/{bid}"))["backup"]["status"], "in_progress")
        self.app.task(self.cloud.async_payloads.pop(0))
        return bid

    def test_success_is_verified_and_recorded(self):
        bid = self.create_and_run()
        self.assertTrue(bid.endswith("-manual"))
        b = body(self.b.get(f"/api/backups/{bid}"))["backup"]
        self.assertEqual((b["status"], b["type"], b["size"], b["downloadSize"]), ("succeeded", "manual", 1200, 900))
        self.assertTrue(b["downloadable"])
        cmd = list(self.cloud.commands.values())[0]
        self.assertEqual((cmd["id"], cmd["stop"]), (bid, False))
        self.assertIsNone(self.store.get("LOCK", "backup"))

    def test_download_is_a_short_lived_link_to_the_savegame_only_archive(self):
        bid = self.create_and_run()
        resp = self.b.get(f"/api/backups/{bid}/download")
        self.assertEqual(resp["statusCode"], 302)
        self.assertIn(f"downloads/{bid}.tar.gz", resp["headers"]["Location"])
        self.assertNotIn(f"world/{bid}", resp["headers"]["Location"])

    def test_no_overlap(self):
        self.assertEqual(self.b.post("/api/backups")["statusCode"], 202)
        resp = self.b.post("/api/backups")
        self.assertEqual(resp["statusCode"], 409)
        self.assertIn("Another backup", body(resp)["error"])

    def test_worker_duplicate_delivery(self):
        self.b.post("/api/backups")
        payload = self.cloud.async_payloads[0]
        self.app.task(payload)
        self.assertEqual(self.app.task(payload)["result"], "ignored")
        self.assertEqual(len(self.cloud.commands), 1)

    def test_rejected_while_stopped(self):
        self.cloud.state = "stopped"
        self.assertEqual(self.b.post("/api/backups")["statusCode"], 409)
        self.assertEqual(body(self.b.get("/api/backups"))["backups"], [])

    def test_checksum_mismatch_is_a_failure(self):
        self.cloud.backup_behavior = "bad-checksum"
        bid = self.create_and_run()
        b = body(self.b.get(f"/api/backups/{bid}"))["backup"]
        self.assertEqual(b["status"], "failed")
        self.assertIn("checksum", b["reason"])
        self.assertFalse(b["downloadable"])
        self.assertEqual(self.b.get(f"/api/backups/{bid}/download")["statusCode"], 404)

    def test_script_failure(self):
        self.cloud.backup_behavior = "fail"
        b = body(self.b.get(f"/api/backups/{self.create_and_run()}"))["backup"]
        self.assertEqual(b["status"], "failed")
        self.assertIn("no savegame directory", b["reason"])

    def test_ssm_unreachable(self):
        self.cloud.fail.add("send_backup")
        b = body(self.b.get(f"/api/backups/{self.create_and_run()}"))["backup"]
        self.assertEqual(b["status"], "failed")
        self.assertIn("SSM", b["reason"])
        self.assertIsNone(self.store.get("LOCK", "backup"))

    def test_hung_command_times_out(self):
        self.cloud.backup_behavior = "hang"
        b = body(self.b.get(f"/api/backups/{self.create_and_run()}"))["backup"]
        self.assertEqual(b["status"], "failed")

    def test_pagination(self):
        for i in range(20):
            self.store.put({"pk": "BACKUP", "sk": f"202610{i + 10:02d}T000000Z-automatic", "id": f"202610{i + 10:02d}T000000Z-automatic",
                            "type": "automatic", "status": "skipped", "startedAt": "2026-10-15T00:00:00Z"})
        first = body(self.b.get("/api/backups"))
        self.assertEqual(len(first["backups"]), 15)
        second = body(self.b.get("/api/backups", query=f"cursor={first['next']}"))
        self.assertEqual(len(second["backups"]), 5)
        self.assertEqual(self.b.get("/api/backups", query="cursor=../x")["statusCode"], 400)


class AutomaticBackup(unittest.TestCase):
    def setUp(self):
        self.app, self.cloud, self.store, self.clock = make_app()

    def run_hourly(self, when):
        return self.app.task({"task": "scheduled", "schedule": "backup", "scheduledTime": when})["result"]

    def test_hourly_backup(self):
        self.assertEqual(self.run_hourly("2026-10-15T19:30:00Z"), "done")
        b = self.store.get("BACKUP", "20261015T193000Z-automatic")
        self.assertEqual(b["status"], "succeeded")

    def test_skipped_while_stopped_is_reported(self):
        self.cloud.state = "stopped"
        self.assertEqual(self.run_hourly("2026-10-16T05:30:00Z"), "skipped")
        b = self.store.get("BACKUP", "20261016T053000Z-automatic")
        self.assertEqual((b["status"], b["reason"]), ("skipped", "Server machine was stopped."))
        self.assertEqual(self.cloud.commands, {})

    def test_skipped_while_another_runs(self):
        self.store.acquire_lock("backup", "other", self.clock.now(), 600)
        self.assertEqual(self.run_hourly("2026-10-15T19:30:00Z"), "skipped")
        self.assertIn("Another backup", self.store.get("BACKUP", "20261015T193000Z-automatic")["reason"])

    def test_duplicate_delivery(self):
        self.run_hourly("2026-10-15T19:30:00Z")
        self.assertEqual(self.run_hourly("2026-10-15T19:30:00Z"), "duplicate")
        self.assertEqual(len(self.cloud.commands), 1)


class Retention(unittest.TestCase):
    def setUp(self):
        self.app, self.cloud, self.store, self.clock = make_app()

    def add(self, hours_ago, status, with_objects=True):
        ts = T0 - hours_ago * HOUR
        bid = backups.new_id("automatic", ts)
        item = {"pk": "BACKUP", "sk": bid, "id": bid, "type": "automatic", "status": status, "startedAt": schedules.iso(ts)}
        if status == "succeeded" and with_objects:
            item.update(key=f"world/{bid}.tar.gz", downloadKey=f"downloads/{bid}.tar.gz", finishedAt=schedules.iso(ts + 30))
            for k in (item["key"], item["downloadKey"]):
                self.cloud.objects[k] = {"size": 1, "metadata": {}, "lastModified": ts}
        elif status != "in_progress":
            item["finishedAt"] = schedules.iso(ts + 30)
        self.store.put(item)
        return bid

    def add_successes(self, count, oldest_hours_ago):
        """`count` hourly successes, oldest first, the oldest `oldest_hours_ago` h ago."""
        return [self.add(oldest_hours_ago - n, "succeeded") for n in range(count)]

    def add_cli_backup(self, hours_ago):
        """An archive from the command-line timer: world/<stamp>.tar.gz, no record."""
        key = f"world/{backups._stamp(T0 - hours_ago * HOUR)}.tar.gz"
        self.cloud.objects[key] = {"size": 1, "metadata": {}, "lastModified": T0 - hours_ago * HOUR}
        return key

    def assert_kept(self, bid):
        self.assertIsNotNone(self.store.get("BACKUP", bid))
        self.assertIn(f"world/{bid}.tar.gz", self.cloud.objects)
        self.assertIn(f"downloads/{bid}.tar.gz", self.cloud.objects)

    def assert_gone(self, bid):
        self.assertIsNone(self.store.get("BACKUP", bid))
        self.assertNotIn(f"world/{bid}.tar.gz", self.cloud.objects)
        self.assertNotIn(f"downloads/{bid}.tar.gz", self.cloud.objects)

    def test_keeps_newest_24_however_old(self):
        ids = self.add_successes(26, oldest_hours_ago=125)  # every one of them older than 100 h
        removed = backups.apply_retention(self.store, self.cloud, T0)
        for bid in ids[:2]:
            self.assert_gone(bid)
        for bid in ids[2:]:
            self.assert_kept(bid)
        self.assertEqual(sorted(removed["records"]), sorted(ids[:2]))

    def test_a_long_gap_does_not_expire_backups(self):
        # Friday's backups, nothing all weekend, then Monday's first one
        ids = self.add_successes(5, oldest_hours_ago=74) + [self.add(0, "succeeded")]
        self.assertEqual(backups.apply_retention(self.store, self.cloud, T0), {"records": [], "objects": []})
        for bid in ids:
            self.assert_kept(bid)

    def test_failed_and_skipped_runs_follow_the_cutoff(self):
        old_failure = self.add(60, "failed")
        self.add_successes(10, oldest_hours_ago=34)
        backups.apply_retention(self.store, self.cloud, T0)
        self.assertIsNotNone(self.store.get("BACKUP", old_failure))  # fewer than 24 successes: nothing goes
        self.add_successes(15, oldest_hours_ago=24)  # 25 successes now; the cutoff is the one 33 h ago
        recent_skip = self.add(5, "skipped")
        backups.apply_retention(self.store, self.cloud, T0)
        self.assertIsNone(self.store.get("BACKUP", old_failure))
        self.assertIsNotNone(self.store.get("BACKUP", recent_skip))
        self.assertEqual(len([i for i in self.store.query("BACKUP")[0] if i["status"] == "succeeded"]), 24)

    def test_in_progress_is_protected_and_stale_ones_fail(self):
        running = self.add(0.1, "in_progress")
        stale = self.add(60, "in_progress")
        backups.apply_retention(self.store, self.cloud, T0)
        self.assertEqual(self.store.get("BACKUP", running)["status"], "in_progress")
        self.assertEqual(self.store.get("BACKUP", stale)["status"], "failed")

    def test_orphan_objects(self):
        old = self.add_cli_backup(100)
        old_download = old.replace("world/", "downloads/")
        self.cloud.objects[old_download] = dict(self.cloud.objects[old])
        self.add(1, "succeeded")
        backups.apply_retention(self.store, self.cloud, T0)
        self.assertIn(old, self.cloud.objects)  # fewer than 24 successes: nothing goes
        self.add_successes(25, oldest_hours_ago=50)  # 26 successes; the cutoff is the one 48 h ago
        recent = self.add_cli_backup(7)
        backups.apply_retention(self.store, self.cloud, T0)
        self.assertNotIn(old, self.cloud.objects)
        self.assertNotIn(old_download, self.cloud.objects)
        self.assertIn(recent, self.cloud.objects)

    def test_never_deletes_newest_world_object(self):
        self.cloud.objects["world/20261001T000000Z.tar.gz"] = {"size": 1, "metadata": {}, "lastModified": T0 - 100 * HOUR}
        backups.apply_retention(self.store, self.cloud, T0)
        self.assertIn("world/20261001T000000Z.tar.gz", self.cloud.objects)
        # Also past the cutoff, when the orphan's key sorts last (say a hand-made upload with a future stamp)
        self.add_successes(25, oldest_hours_ago=50)
        self.cloud.objects["world/29991231T000000Z.tar.gz"] = {"size": 1, "metadata": {}, "lastModified": T0 - 100 * HOUR}
        backups.apply_retention(self.store, self.cloud, T0)
        self.assertIn("world/29991231T000000Z.tar.gz", self.cloud.objects)
        self.assertNotIn("world/20261001T000000Z.tar.gz", self.cloud.objects)


class Health(unittest.TestCase):
    def setUp(self):
        self.app, self.cloud, self.store, self.clock = make_app()
        self.r = Retention()
        self.r.store, self.r.cloud = self.store, self.cloud

    def test_states(self):
        self.assertEqual(backups.health(self.store, T0, "running")["state"], "none")
        self.r.add(1, "succeeded")
        self.assertEqual(backups.health(self.store, T0, "running")["state"], "healthy")
        self.assertEqual(backups.health(self.store, T0, "stopped")["state"], "paused")
        self.assertEqual(backups.health(self.store, T0 + 3 * HOUR, "running")["state"], "stale")
        self.r.add(0.5, "failed")
        self.r.add(0.2, "failed")
        h = backups.health(self.store, T0, "running")
        self.assertEqual((h["state"], h["recentFailures"]), ("failing", 2))


if __name__ == "__main__":
    unittest.main()
