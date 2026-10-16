import copy
import json
import unittest

from helpers import NOW, RUNNING, SECRETS, SERVER_START, ServerDir, fixture_snapshot

import settings


def rows(model):
    return {r["key"]: r for g in model["groups"] for r in g["settings"]}


class Formatting(unittest.TestCase):
    def test_values(self):
        self.assertEqual(settings.fmt(1.5, "factor"), "150%")
        self.assertEqual(settings.fmt(0.9, "factor"), "90%")
        self.assertEqual(settings.fmt(1800000000000, "duration"), "30 min")
        self.assertEqual(settings.fmt(150000000000, "duration"), "2.5 min")
        self.assertEqual(settings.fmt(False, "onoff"), "Off")
        self.assertEqual(settings.fmt("LoseSomeProgress", "enum"), "Lose some progress")
        self.assertEqual(settings.fmt([], "list"), "None")

    def test_docker_timestamps(self):
        self.assertEqual(settings.parse_time("2026-10-15T18:09:54.799214346Z"), settings.parse_time("2026-10-15T18:09:54.799214Z"))
        self.assertIsNone(settings.parse_time("0001-01-01T00:00:00Z"))


class RunningServer(unittest.TestCase):
    def setUp(self):
        self.model = settings.build_model(fixture_snapshot(), RUNNING, NOW)
        self.rows = rows(self.model)

    def test_status_and_header(self):
        self.assertTrue(self.model["live"])
        self.assertEqual(self.model["server"]["status"]["state"], "online")
        self.assertEqual(self.model["server"]["name"], "Fixture World")
        self.assertEqual(self.model["server"]["address"], "203.0.113.10:15637")
        self.assertIn("Changes every time the server starts", self.rows["address"]["statusNote"])
        self.assertEqual(self.model["pendingRestart"], 0)
        self.assertEqual(self.model["notices"], [])

    def test_groups(self):
        self.assertEqual([g["id"] for g in self.model["groups"]], ["general", "gameplay", "players", "networking", "advanced"])

    def test_game_settings_come_from_the_log(self):
        r = self.rows["gameSettings.enemyDamageFactor"]
        self.assertEqual((r["status"], r["display"], r["source"]), ("applied", "150%", "Server startup log"))
        self.assertEqual(r["default"], {"value": 1, "display": "100%", "isDefault": False})
        self.assertTrue(self.rows["gameSettings.enemyHealthFactor"]["default"]["isDefault"])
        self.assertEqual(self.rows["gameSettingsPreset"]["status"], "applied")

    def test_new_game_setting_shows_without_catalog_entry(self):
        r = self.rows["gameSettings.flameLevelFogRemoval"]
        self.assertEqual((r["label"], r["display"], r["section"]), ("Flame level fog removal", "11", "Other"))
        self.assertNotIn("default", r)

    def test_file_settings(self):
        r = self.rows["slotCount"]
        self.assertEqual((r["status"], r["display"]), ("configured", "16"))
        self.assertEqual(self.rows["queryPort"]["status"], "applied")  # Steam lists that port

    def test_access(self):
        self.assertEqual(self.rows["userGroups[].password"]["display"], "No password for: Visitor")
        admin = self.rows["userGroups[name=Admin]"]
        self.assertIn({"label": "Password required", "on": True}, admin["chips"])
        self.assertTrue(admin["default"]["isDefault"])
        self.assertEqual(self.rows["bannedAccountsCount"]["display"], "2")
        self.assertEqual(self.rows["bannedAccountsCount"]["change"], "in-game")

    def test_versions(self):
        self.assertEqual(self.rows["log.svn"]["display"], "1265863")
        self.assertEqual(self.rows["log.version"]["display"], "a07fae726ce0 (master)")
        self.assertEqual(self.rows["steam.buildid"]["value"], "25936466")


class PendingChanges(unittest.TestCase):
    def setUp(self):
        self.d = ServerDir()
        self.d.snapshot()  # agent ran while the file was still the loaded one

    def tearDown(self):
        self.d.cleanup()

    def test_game_setting_changed_in_file(self):
        self.d.edit_config(lambda c: c["gameSettings"].update(enemyDamageFactor=2), SERVER_START + 3600)
        model = settings.build_model(self.d.snapshot(now=SERVER_START + 3700), RUNNING, SERVER_START + 3700)
        r = rows(model)["gameSettings.enemyDamageFactor"]
        self.assertEqual((r["status"], r["display"], r["pending"]["display"]), ("pending", "150%", "200%"))
        self.assertEqual(model["pendingRestart"], 1)
        self.assertEqual(rows(model)["slotCount"]["status"], "configured")

    def test_top_level_changed_in_file(self):
        self.d.edit_config(lambda c: c.update(slotCount=8), SERVER_START + 3600)
        r = rows(settings.build_model(self.d.snapshot(now=SERVER_START + 3700), RUNNING, SERVER_START + 3700))["slotCount"]
        self.assertEqual((r["status"], r["display"], r["pending"]["display"]), ("pending", "16", "8"))

    def test_named_preset_overrides_file_values(self):
        snap = fixture_snapshot()
        snap["log"]["preset"] = "Hard"
        snap["config"]["gameSettingsPreset"] = "Hard"
        snap["config"]["gameSettings"]["enemyDamageFactor"] = 3
        r = rows(settings.build_model(snap, RUNNING, NOW))["gameSettings.enemyDamageFactor"]
        self.assertEqual((r["status"], r["display"]), ("applied", "150%"))
        self.assertIn("Hard preset overrides", r["statusNote"])


class Availability(unittest.TestCase):
    def test_instance_stopped_shows_last_known(self):
        rt = copy.deepcopy(RUNNING)
        rt["instance"]["state"] = "stopped"
        del rt["steam"]
        model = settings.build_model(fixture_snapshot(), rt, NOW + 86400)
        self.assertFalse(model["live"])
        self.assertEqual(model["server"]["status"]["state"], "stopped")
        self.assertEqual(model["notices"][0]["level"], "warn")
        self.assertIn(fixture_snapshot()["inspectedAt"], model["notices"][0]["text"])
        self.assertEqual(rows(model)["gameSettings.enemyDamageFactor"]["display"], "150%")

    def test_stale_snapshot(self):
        model = settings.build_model(fixture_snapshot(), RUNNING, NOW + 3600)
        self.assertEqual(model["server"]["status"]["state"], "unknown")
        self.assertFalse(model["live"])

    def test_no_snapshot(self):
        model = settings.build_model(None, RUNNING, NOW)
        self.assertEqual(model["groups"], [])
        self.assertEqual(model["notices"][0]["level"], "info")

    def test_container_stopped(self):
        snap = fixture_snapshot()
        snap["container"].update(running=False, status="exited")
        snap["serverProcess"] = {"running": False, "startedAt": None}
        model = settings.build_model(snap, RUNNING, NOW)
        self.assertEqual(model["server"]["status"]["state"], "offline")
        self.assertEqual(rows(model)["serverProcess.startedAt"]["display"], "Not running")

    def test_old_log_while_running_is_not_trusted(self):
        snap = fixture_snapshot()
        snap["log"]["run"] = "previous"
        model = settings.build_model(snap, RUNNING, NOW)
        self.assertEqual(rows(model)["gameSettings.enemyDamageFactor"]["status"], "configured")
        self.assertEqual(model["server"]["status"]["state"], "starting")

    def test_aws_errors_become_notices(self):
        model = settings.build_model(fixture_snapshot(), {"errors": ["Could not read the firewall rules from AWS."]}, NOW)
        self.assertIn("firewall", model["notices"][-1]["text"])


class NoSecrets(unittest.TestCase):
    def test_backend_redacts_even_if_agent_did_not(self):
        snap = fixture_snapshot()
        snap["config"]["userGroups"][0]["password"] = "fixture-Admin-SECRET"
        snap["config"]["adminToken"] = "fixture-Friend-SECRET"
        snap["loadedConfig"]["config"]["userGroups"][0]["password"] = "fixture-Guest-SECRET"
        body = json.dumps(settings.build_model(snap, RUNNING, NOW))
        for secret in SECRETS:
            self.assertNotIn(secret, body)


if __name__ == "__main__":
    unittest.main()
