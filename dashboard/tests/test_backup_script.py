import json
import os
import tempfile
import unittest
from unittest import mock

import helpers  # noqa: F401 - sets sys.path

import enshrouded_dashboard_backup as script


class ConsistentCopy(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.src = os.path.join(self.tmp, "savegame")
        os.makedirs(os.path.join(self.src, "backup"))
        for name in ("world0.ksd", "backup/world0.ksd.backup-0"):
            with open(os.path.join(self.src, name), "w") as f:
                f.write("data")

    def test_stable_files_copy_first_time(self):
        self.assertEqual(script.consistent_copy(self.src, os.path.join(self.tmp, "out"), sleep=lambda s: None), 1)
        self.assertEqual(open(os.path.join(self.tmp, "out", "world0.ksd")).read(), "data")

    def test_retries_when_the_server_saves_mid_copy(self):
        real_copy = script.shutil.copytree
        calls = []

        def copy_while_saving(src, dst, *args, **kwargs):
            real_copy(src, dst, *args, **kwargs)
            calls.append(1)
            if len(calls) == 1:  # the game writes the world during the first copy
                with open(os.path.join(self.src, "world0.ksd"), "w") as f:
                    f.write("newer data")
        with mock.patch.object(script.shutil, "copytree", copy_while_saving):
            attempts = script.consistent_copy(self.src, os.path.join(self.tmp, "out"), sleep=lambda s: None)
        self.assertEqual(attempts, 2)
        self.assertEqual(open(os.path.join(self.tmp, "out", "world0.ksd")).read(), "newer data")

    def test_gives_up(self):
        def always_changing(src, dst, *args, **kwargs):
            os.makedirs(dst)
            with open(os.path.join(self.src, "world0.ksd"), "a") as f:
                f.write("x")
        with mock.patch.object(script.shutil, "copytree", always_changing):
            with self.assertRaises(RuntimeError):
                script.consistent_copy(self.src, os.path.join(self.tmp, "out"), attempts=3, sleep=lambda s: None)


class Arguments(unittest.TestCase):
    def test_rejects_bad_ids(self):
        for bid, kind in (("x; rm -rf /", "manual"), ("20261015T190000Z-manual", "automatic"), ("20261015T190000Z-other", "manual")):
            with mock.patch("builtins.print") as out:
                code = script.main(["--id", bid, "--type", kind])
            self.assertEqual(code, 2)
            self.assertFalse(json.loads(out.call_args[0][0])["ok"])

    def test_document_pattern_matches_script(self):
        doc = json.load(open(os.path.join(helpers.DASHBOARD, "agent", "ssm-backup-document.json")))
        import re
        pattern = re.compile(doc["parameters"]["BackupId"]["allowedPattern"])
        for bid in ("20261015T190000Z-manual", "20261015T190000Z-shutdown"):
            self.assertTrue(pattern.match(bid) and script.ID_PATTERN.match(bid))
        self.assertFalse(pattern.match("20261015T190000Z-manual; reboot"))


if __name__ == "__main__":
    unittest.main()
