"""pace.py init の unittest。決まりは docs/plans/pace_design.md の9節。"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PACE = Path(__file__).resolve().parents[1] / "plugins" / "pace" / "scripts" / "pace.py"

UNREADABLE = "PACE-STATUS: errors=1 failed_jobs=0 fixes=0 questions=0\nNEXT: show\n"


def run(*args):
    return subprocess.run([sys.executable, str(PACE), *args], capture_output=True, text=True)


class InitTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.plan = self.dir / "plan.md"
        self.plan.write_text("# 題\n\n本文。\n", encoding="utf-8")
        self.pace = self.dir / ".pace"
        self.work = self.pace / "plan"

    def tearDown(self):
        self._tmp.cleanup()

    def init(self, plan=None):
        return run("init", str(plan or self.plan))

    def test_creates_gitignore_workdir_and_units(self):
        r = self.init()
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "units=2\n", ""))
        self.assertEqual((self.pace / ".gitignore").read_text(), "*\n")
        self.assertEqual(json.loads((self.work / "source.json").read_text()), {"plan_file": "plan.md"})
        self.assertEqual((self.work / "plan.md").read_bytes(), self.plan.read_bytes())
        units = [json.loads(l) for l in (self.work / "units.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([(u["kind"], u["text"]) for u in units], [("heading", "題"), ("sentence", "本文。")])
        self.assertEqual((self.work / "shown.md").read_bytes(), self.plan.read_bytes())

    def test_keeps_records_and_recreates_others(self):
        self.init()
        (self.work / "cache").mkdir()
        (self.work / "cache" / "k").write_text("v")
        for name in ("rounds.jsonl", "show_snapshot.md"):
            (self.work / name).write_text("keep")
        (self.work / "shown.md").write_text("見せた版")
        (self.work / "jobs").mkdir()
        (self.work / "jobs" / "j.json").write_text("{}")
        (self.work / "report.md").write_text("old")
        self.plan.write_text("# 題\n\n直した。\n", encoding="utf-8")
        self.assertEqual(self.init().returncode, 0)
        names = sorted(p.name for p in self.work.iterdir())
        self.assertEqual(names, ["cache", "plan.md", "rounds.jsonl", "show_snapshot.md", "shown.md",
                                 "source.json", "units.jsonl"])
        self.assertEqual((self.work / "cache" / "k").read_text(), "v")
        self.assertEqual((self.work / "shown.md").read_text(), "見せた版")
        self.assertIn("直した。", (self.work / "units.jsonl").read_text(encoding="utf-8"))
        self.assertTrue((self.work / "source.json").exists())

    def test_symlink_inside_workdir_is_unlinked_not_followed(self):
        self.init()
        outside = self.dir / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("x")
        os.symlink(str(outside), str(self.work / "link"))
        self.assertEqual(self.init().returncode, 0)
        self.assertFalse((self.work / "link").exists())
        self.assertEqual((outside / "keep.txt").read_text(), "x")

    def test_bom_is_removed(self):
        self.plan.write_bytes("﻿一。".encode("utf-8"))
        self.assertEqual(self.init().stdout, "units=1\n")
        unit = json.loads((self.work / "units.jsonl").read_text(encoding="utf-8"))
        self.assertEqual(unit["text"], "一。")

    def test_unreadable_plan_touches_nothing(self):
        bad = self.dir / "bad.md"
        bad.write_bytes(b"\xff\xfe\x00")
        (self.dir / "sub.md").write_text("x")
        for target in (self.dir / "nothing.md", self.dir, bad, self.dir / "sub.md" / "x.md"):
            r = self.init(target)
            self.assertEqual((r.returncode, r.stdout, r.stderr), (0, UNREADABLE, ""), target)
        self.assertFalse(self.pace.exists())

    def test_unreadable_plan_keeps_other_plans_workdir(self):
        self.init()
        r = self.init(self.dir / "plan.txt")
        self.assertEqual((r.returncode, r.stdout), (0, UNREADABLE))
        self.assertTrue((self.work / "units.jsonl").exists())

    def test_other_extension_conflicts(self):
        self.init()
        other = self.dir / "plan.txt"
        other.write_text("別のプラン。", encoding="utf-8")
        before = (self.work / "units.jsonl").read_bytes()
        r = self.init(other)
        self.assertEqual(r.returncode, 1)
        self.assertIn("plan.md の作業ディレクトリです", r.stderr)
        self.assertEqual((self.work / "units.jsonl").read_bytes(), before)
        # 控えたファイルが無くなっても引き継がない
        self.plan.unlink()
        self.assertEqual(self.init(other).returncode, 1)
        self.assertEqual((self.work / "units.jsonl").read_bytes(), before)

    def test_same_file_with_other_spelling_passes(self):
        self.init()
        alias = self.dir / "alias.md"
        os.link(str(self.plan), str(alias))
        (self.pace / "alias").mkdir()
        (self.pace / "alias" / "source.json").write_text('{"plan_file": "plan.md"}')
        self.assertEqual(self.init(alias).returncode, 0)

    def test_broken_source_json(self):
        self.init()
        for body in ("{", '{"plan_file": 1}', '{"plan_file": "../x.md"}', "[]"):
            (self.work / "source.json").write_text(body)
            r = self.init()
            self.assertEqual(r.returncode, 1, body)
            self.assertTrue((self.work / "units.jsonl").exists())

    def test_symlink_pace_dir_is_refused(self):
        real = self.dir / "real"
        real.mkdir()
        os.symlink(str(real), str(self.pace))
        r = self.init()
        self.assertEqual(r.returncode, 1)
        self.assertEqual(list(real.iterdir()), [])

    def test_workdir_that_is_a_file_is_refused(self):
        self.pace.mkdir()
        self.work.write_text("x")
        self.assertEqual(self.init().returncode, 1)
        self.assertEqual(self.work.read_text(), "x")

    @unittest.skipIf(os.geteuid() == 0, "root は書き込みの権限を無視する")
    def test_unwritable_is_failure(self):
        self.pace.mkdir()
        os.chmod(str(self.pace), 0o500)
        try:
            r = self.init()
        finally:
            os.chmod(str(self.pace), 0o700)
        self.assertEqual(r.returncode, 1)
        self.assertIn("書けませんでした", r.stderr)

    def test_multi_dot_name(self):
        plan = self.dir / "a.b.md"
        plan.write_text("一。", encoding="utf-8")
        self.assertEqual(self.init(plan).returncode, 0)
        self.assertTrue((self.pace / "a.b" / "units.jsonl").exists())

    def test_names_that_would_escape_workdir_are_refused(self):
        self.init()
        neighbor = self.dir / "neighbor.txt"
        neighbor.write_text("x")
        for name in ("..md", "...md", ".gitignore.md"):
            plan = self.dir / name
            plan.write_text("一。", encoding="utf-8")
            r = self.init(plan)
            self.assertEqual(r.returncode, 1, name)
            self.assertIn("作業ディレクトリの名前を作れない", r.stderr)
        self.assertEqual(neighbor.read_text(), "x")
        self.assertTrue((self.work / "units.jsonl").exists())
        self.assertEqual((self.pace / ".gitignore").read_text(), "*\n")

    def test_version_is_unchanged(self):
        r = run("--version")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(len(r.stdout.splitlines()), 5)


if __name__ == "__main__":
    unittest.main()
