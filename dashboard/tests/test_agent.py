import json
import os
import unittest

from helpers import NOW, SECRETS, SERVER_START, SNAPSHOT_FIXTURE, ServerDir, agent, fixture_snapshot


class DecodeLogValue(unittest.TestCase):
    def test_hex_floats(self):
        self.assertEqual(agent.decode_log_value("3fc00000"), 1.5)
        self.assertEqual(agent.decode_log_value("3f666666"), 0.9)
        self.assertEqual(agent.decode_log_value("40000000"), 2.0)  # hex, not forty million

    def test_plain_values(self):
        self.assertEqual(agent.decode_log_value("1"), 1)
        self.assertEqual(agent.decode_log_value({"value": 600000000000}), 600000000000)
        self.assertEqual(agent.decode_log_value("Normal"), "Normal")
        self.assertIs(agent.decode_log_value(True), True)
        self.assertEqual(agent.decode_log_value(11), 11)


class ParseLog(unittest.TestCase):
    def setUp(self):
        self.d = ServerDir()
        self.log = agent.read_log(os.path.join(self.d.data, "logs", "enshrouded_server.log"))

    def tearDown(self):
        self.d.cleanup()

    def test_startup_facts(self):
        self.assertEqual(self.log["version"], "a07fae726ce00447d7ba92972c872505fb2b9468")
        self.assertEqual(self.log["branch"], "master")
        self.assertEqual(self.log["svn"], "1265863")
        self.assertEqual(self.log["preset"], "Custom")
        self.assertEqual(self.log["publicIp"], "203.0.113.10")
        self.assertTrue(self.log["hostOnline"])
        self.assertEqual(self.log["startedAt"], SERVER_START - 1)

    def test_applied_game_settings(self):
        gs = self.log["gameSettings"]
        self.assertEqual(gs["enemyDamageFactor"], 1.5)
        self.assertEqual(gs["foodBuffDurationFactor"], 0.9)
        self.assertEqual(gs["plantGrowthSpeedFactor"], 2.0)
        self.assertEqual(gs["dayTimeDuration"], 1800000000000)
        self.assertEqual(gs["randomSpawnerAmount"], "Many")
        self.assertIs(gs["enableStarvingDebuff"], False)
        self.assertEqual(gs["flameLevelFogRemoval"], 11)

    def test_no_player_names(self):
        self.assertNotIn("FixturePlayer", json.dumps(self.log))


class Sanitize(unittest.TestCase):
    def test_passwords_and_bans(self):
        cfg = {"userGroups": [{"name": "Admin", "password": "x1"}, {"name": "Visitor", "password": ""}],
               "bannedAccounts": ["1", "2", "3"], "extra": {"steamApiKey": "k", "authToken": "t", "ok": 1}}
        clean, redacted = agent.sanitize_config(cfg)
        self.assertEqual(clean["userGroups"], [{"name": "Admin", "passwordSet": True}, {"name": "Visitor", "passwordSet": False}])
        self.assertEqual(clean["bannedAccountsCount"], 3)
        self.assertNotIn("bannedAccounts", clean)
        self.assertEqual(clean["extra"], {"ok": 1})
        self.assertEqual(sorted(redacted), ["extra.authToken", "extra.steamApiKey"])
        self.assertEqual(cfg["userGroups"][0]["password"], "x1")  # input untouched

    def test_legacy_bans_key(self):
        clean, _ = agent.sanitize_config({"bans": ["1"]})
        self.assertEqual(clean, {"bansCount": 1})


class BuildSnapshot(unittest.TestCase):
    def setUp(self):
        self.d = ServerDir()

    def tearDown(self):
        self.d.cleanup()

    def test_no_secrets_leave_the_host(self):
        body = json.dumps(self.d.snapshot())
        for secret in SECRETS:
            self.assertNotIn(secret, body)

    def test_running_server_with_unchanged_config(self):
        snap = self.d.snapshot()
        self.assertEqual(snap["log"]["run"], "current")
        self.assertIs(snap["configChangedSinceStart"], False)
        self.assertEqual(snap["loadedConfig"]["config"], snap["config"])
        self.assertEqual(snap["build"]["buildId"], "25936466")
        self.assertEqual(snap["configFile"]["path"], "/srv/enshrouded/enshrouded_server.json")

    def test_config_edited_after_start_keeps_loaded_copy(self):
        self.d.snapshot()  # first run caches what the server loaded
        self.d.edit_config(lambda c: c.update(slotCount=8), SERVER_START + 3600)
        snap = self.d.snapshot(now=SERVER_START + 3700)
        self.assertIs(snap["configChangedSinceStart"], True)
        self.assertEqual(snap["config"]["slotCount"], 8)
        self.assertEqual(snap["loadedConfig"]["config"]["slotCount"], 16)

    def test_config_edited_before_first_inspection(self):
        self.d.edit_config(lambda c: c.update(slotCount=8), SERVER_START + 3600)
        snap = self.d.snapshot(now=SERVER_START + 3700)
        self.assertIs(snap["configChangedSinceStart"], True)
        self.assertIsNone(snap["loadedConfig"])

    def test_restart_replaces_cache(self):
        self.d.snapshot()
        later = SERVER_START + 7200
        self.d.edit_config(lambda c: c.update(slotCount=8), later + 1)
        snap = self.d.snapshot(now=later + 60, process_start=later)
        self.assertEqual(snap["loadedConfig"]["config"]["slotCount"], 8)
        self.assertEqual(snap["log"]["run"], "previous")  # fixture log is from the earlier run

    def test_server_not_running(self):
        snap = self.d.snapshot(process_start=None)
        self.assertFalse(snap["serverProcess"]["running"])
        self.assertEqual(snap["log"]["run"], "previous")
        self.assertIsNone(snap["loadedConfig"])

    def test_committed_fixture_is_current(self):
        with open(SNAPSHOT_FIXTURE) as f:
            committed = json.load(f)
        self.assertEqual(committed, fixture_snapshot(),
                         "Regenerate with: python3 dashboard/tests/helpers.py")


if __name__ == "__main__":
    unittest.main()
