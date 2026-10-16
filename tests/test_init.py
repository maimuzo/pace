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
        # 権限のテストが環境の umask に左右されないよう、022 に揃える（subprocess に引き継がれる）
        self._umask = os.umask(0o022)
        self.addCleanup(os.umask, self._umask)
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
        # 1行1単位。splitlines() は U+2028 なども行の終わりにするので、"\n" だけで分ける
        lines = (self.work / "units.jsonl").read_text(encoding="utf-8").split("\n")
        self.assertEqual(lines[-1], "")
        units = [json.loads(l) for l in lines[:-1]]
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
        # ディレクトリをプランとして渡すと、作業ディレクトリは親の .pace/ の下になるはずだった
        self.assertFalse((self.dir.parent / ".pace" / self.dir.name).exists())

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
        before = (self.work / "units.jsonl").read_bytes()
        (self.work / "report.md").write_text("old")
        for body in ("{", '{"plan_file": 1}', '{"plan_file": "../x.md"}', "[]"):
            (self.work / "source.json").write_text(body)
            r = self.init()
            self.assertEqual(r.returncode, 1, body)
            self.assertEqual((self.work / "units.jsonl").read_bytes(), before)
            self.assertEqual((self.work / "source.json").read_text(), body)
            self.assertTrue((self.work / "report.md").exists())

    def test_symlink_pace_dir_is_refused(self):
        real = self.dir / "real"
        real.mkdir()
        os.symlink(str(real), str(self.pace))
        r = self.init()
        self.assertEqual(r.returncode, 1)
        self.assertEqual(list(real.iterdir()), [])

    def test_symlink_workdir_is_refused(self):
        other = self.dir / "other"
        other.mkdir()
        (other / "keep.txt").write_text("x")
        self.pace.mkdir()
        os.symlink(str(other), str(self.work))
        r = self.init()
        self.assertEqual(r.returncode, 1)
        self.assertIn("symlink", r.stderr)
        self.assertEqual(sorted(p.name for p in other.iterdir()), ["keep.txt"])

    def test_pace_dir_that_is_a_file_is_refused(self):
        self.pace.write_text("x")
        self.assertEqual(self.init().returncode, 1)
        self.assertEqual(self.pace.read_text(), "x")

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
        # 作られていない一時ファイルでなく、書こうとしたファイルを指す
        self.assertNotIn(".tmp", r.stderr)
        self.assertIn(".gitignore", r.stderr)

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

    def test_source_json_keeps_the_original_name(self):
        plan = self.dir / "design.markdown"
        plan.write_text("一。", encoding="utf-8")
        self.assertEqual(self.init(plan).returncode, 0)
        self.assertEqual(json.loads((self.pace / "design" / "source.json").read_text()),
                         {"plan_file": "design.markdown"})
        self.assertEqual(self.init(plan).returncode, 0)

    def test_dot_name_keeps_its_name(self):
        plan = self.dir / ".md"
        plan.write_text("一。", encoding="utf-8")
        self.assertEqual(self.init(plan).returncode, 0)
        self.assertTrue((self.pace / ".md" / "units.jsonl").exists())

    def test_symlink_loop_is_unreadable(self):
        loop = self.dir / "loop.md"
        os.symlink(str(loop), str(loop))
        r = self.init(loop)
        self.assertEqual((r.returncode, r.stdout), (0, UNREADABLE))

    def test_source_json_that_is_a_symlink(self):
        self.init()
        source = self.work / "source.json"
        outside = self.dir / "outside.json"
        outside.write_text('{"plan_file": "plan.md"}')
        source.unlink()
        os.symlink(str(outside), str(source))
        r = self.init()
        self.assertEqual(r.returncode, 1)
        self.assertIn("ふつうのファイルではありません", r.stderr)

    def test_source_json_that_is_too_deep(self):
        self.init()
        (self.work / "source.json").write_text("[" * 100000 + "]" * 100000)
        r = self.init()
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("Traceback", r.stderr)

    def test_plan_copies_keep_plan_mode(self):
        # プランの中身を持つもの（写し・units.jsonl・作業ディレクトリ）は、元のプランの権限を持ち主のほかには広げない
        os.chmod(str(self.plan), 0o600)
        self.init()
        for name in ("plan.md", "shown.md", "units.jsonl"):
            self.assertEqual(os.stat(str(self.work / name)).st_mode & 0o777, 0o600, name)
        for path in (self.pace, self.work):
            self.assertEqual(os.stat(str(path)).st_mode & 0o777, 0o700, path)

    def test_mode_follows_umask(self):
        os.umask(0o077)
        self.init()
        self.assertEqual(os.stat(str(self.work / "units.jsonl")).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(str(self.pace)).st_mode & 0o777, 0o700)

    def test_group_shared_plan_keeps_group_access(self):
        os.umask(0o002)
        os.chmod(str(self.plan), 0o664)
        self.init()
        self.assertEqual(os.stat(str(self.work / "units.jsonl")).st_mode & 0o777, 0o664)
        self.assertEqual(os.stat(str(self.work)).st_mode & 0o777, 0o775)
        # .pace/ には group の書く権限を渡さない（別のプランの作業ディレクトリを差し替えさせない）
        self.assertEqual(os.stat(str(self.pace)).st_mode & 0o777, 0o755)

    def test_plan_without_owner_bits_still_gives_owner_access(self):
        # 他人の持ち物で group だけが読める 0044 のプランを回すと、写しの持ち主は init を打った利用者になる。
        # 同じ利用者では作れない場面なので、権限を決める関数を直に確かめる
        sys.dont_write_bytecode = True
        sys.path.insert(0, str(PACE.parent))
        try:
            import pace
        finally:
            sys.path.pop(0)
        self.assertEqual(pace.modes_for(0o044), (0o644, 0o755, 0o755))
        self.assertEqual(pace.modes_for(0o600), (0o600, 0o700, 0o700))
        self.assertEqual(pace.modes_for(0o664), (0o664, 0o775, 0o755))
        self.assertEqual(pace.modes_for(0o400), (0o600, 0o700, 0o700))

    def test_kept_symlink_is_not_followed(self):
        self.init()
        outside = self.dir / "outside.txt"
        outside.write_text("x")
        os.chmod(str(outside), 0o644)
        shown = self.work / "shown.md"
        shown.unlink()
        os.symlink(str(outside), str(shown))
        os.chmod(str(self.plan), 0o600)
        self.assertEqual(self.init().returncode, 0)
        self.assertEqual(os.stat(str(outside)).st_mode & 0o777, 0o644)
        self.assertTrue(shown.is_symlink())

    def test_kept_hard_link_is_not_narrowed(self):
        self.init()
        outside = self.dir / "outside.txt"
        outside.write_text("x")
        os.chmod(str(outside), 0o644)
        snap = self.work / "show_snapshot.md"
        os.link(str(outside), str(snap))
        os.chmod(str(self.plan), 0o600)
        self.assertEqual(self.init().returncode, 0)
        self.assertEqual(os.stat(str(outside)).st_mode & 0o777, 0o644)

    def test_uppercase_gitignore_name_is_refused(self):
        plan = self.dir / ".GITIGNORE.md"
        plan.write_text("一。", encoding="utf-8")
        r = self.init(plan)
        self.assertEqual(r.returncode, 1)
        self.assertIn("作業ディレクトリの名前を作れない", r.stderr)
        self.assertFalse(self.pace.exists())

    def test_old_show_snapshot_is_narrowed(self):
        self.init()
        snap = self.work / "show_snapshot.md"
        snap.write_text("写し")
        os.chmod(str(snap), 0o644)
        os.chmod(str(self.plan), 0o600)
        self.init()
        self.assertEqual(os.stat(str(snap)).st_mode & 0o777, 0o600)

    def test_old_shown_md_is_narrowed(self):
        self.init()
        self.assertEqual(os.stat(str(self.work / "shown.md")).st_mode & 0o777, 0o644)
        os.chmod(str(self.plan), 0o600)
        self.init()
        self.assertEqual(os.stat(str(self.work / "shown.md")).st_mode & 0o777, 0o600)

    @unittest.skipIf(os.geteuid() == 0, "root は権限を無視する")
    def test_unenterable_pace_dir_is_failure_without_traceback(self):
        self.pace.mkdir()
        os.chmod(str(self.pace), 0o000)
        try:
            r = self.init()
        finally:
            os.chmod(str(self.pace), 0o700)
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("Traceback", r.stderr)

    def test_source_json_that_is_a_fifo(self):
        self.init()
        source = self.work / "source.json"
        source.unlink()
        os.mkfifo(str(source))
        r = subprocess.run([sys.executable, str(PACE), "init", str(self.plan)], capture_output=True, text=True,
                           timeout=10)
        self.assertEqual(r.returncode, 1)

    def test_fifo_is_unreadable(self):
        fifo = self.dir / "pipe.md"
        os.mkfifo(str(fifo))
        r = subprocess.run([sys.executable, str(PACE), "init", str(fifo)], capture_output=True, text=True,
                           timeout=10)
        self.assertEqual((r.returncode, r.stdout), (0, UNREADABLE))
        self.assertFalse(self.pace.exists())

    @unittest.skipIf(os.geteuid() == 0, "root は読む権限を無視する")
    def test_plan_without_read_permission_is_failure(self):
        os.chmod(str(self.plan), 0o200)
        try:
            r = self.init()
        finally:
            os.chmod(str(self.plan), 0o600)
        self.assertEqual(r.returncode, 1)
        self.assertIn("プランを読めませんでした", r.stderr)
        self.assertFalse(self.pace.exists())

    def test_refusal_does_not_write_gitignore(self):
        self.pace.mkdir()
        self.work.write_text("x")
        self.assertEqual(self.init().returncode, 1)
        self.assertFalse((self.pace / ".gitignore").exists())

    def test_mode_of_existing_file_is_kept(self):
        # 作り直さずに書き換えるファイル（.gitignore・source.json）は、元の権限を写す
        self.init()
        for path in (self.pace / ".gitignore", self.work / "source.json"):
            os.chmod(str(path), 0o640)
        self.init()
        for path in (self.pace / ".gitignore", self.work / "source.json"):
            self.assertEqual(os.stat(str(path)).st_mode & 0o777, 0o640, path)

    def test_version_is_unchanged(self):
        r = run("--version")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(len(r.stdout.splitlines()), 5)


if __name__ == "__main__":
    unittest.main()
