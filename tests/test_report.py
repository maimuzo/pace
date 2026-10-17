"""pace.py report・show・shown の unittest。決まりは docs/plans/pace_design.md の7節・8節（性質5つ）・18〜22節。

流れは test_decycle.py の道具（FlowBase）で、テストが実行役の代わりに *.out.json を書き、init・next・report・show・shown を
subprocess で叩いて確かめる。囲みとインラインコードの形は import して関数を直に確かめる。
"""

import json
import os
import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "pace" / "scripts"))
import pace  # noqa: E402
from test_decycle import FlowBase, PLAN, parse, run  # noqa: E402


def accept_b(job):
    """B（決定Bを選ぶ。）にだけ accepted の反論を出す。"""
    if job["input"]["target"]["plan_text"] == "決定Bを選ぶ。":
        return {"defeaters": [{"target": job["input"]["target"]["id"], "type": "missing", "text": "反論",
                               "status": "accepted", "by": []}]}
    return {"defeaters": []}


class FormTest(unittest.TestCase):
    def test_fence_is_longer_than_inner_backticks(self):
        self.assertEqual(pace.fence("a ``` b", "text", "  "), ["  ````text", "  a ``` b", "  ````"])
        self.assertEqual(pace.fence("a", "diff", ""), ["```diff", "a", "```"])

    def test_inline(self):
        self.assertEqual(pace.inline("a\nb\r\nc"), "`a b c`")
        self.assertEqual(pace.inline("x `y` z"), "``x `y` z``")
        # 中身が backtick で始まるか終わるときは前後に空白を足す（GFM）。<!-- は字のまま囲みの中に入る
        self.assertEqual(pace.inline("`Tick` <!-- 隠れない -->"), "`` `Tick` <!-- 隠れない --> ``")
        self.assertEqual(pace.inline("a|b", table=True), "`a\\|b`")


class ReportFlowTest(FlowBase):
    def report(self, ok=True):
        r = run("report", str(self.plan))
        if ok:
            self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def round_(self, review=accept_b):
        """init から DONE まで回し、report を打つ。"""
        self.init()
        for _ in range(30):
            bundles, last = parse(self.next().stdout)
            if last.startswith("DONE"):
                break
            self.answer(bundles, review=review)
        return self.report()

    def lines(self):
        return [json.loads(x) for x in (self.work / "rounds.jsonl").read_text().splitlines()]

    def edit(self, n):
        self.plan.write_text(PLAN + "追記%dの前提。\n" % n, encoding="utf-8")

    # --- 項目と NEXT（性質1） --------------------------------------------

    def test_report_counts_items_and_continues(self):
        r = self.round_()
        out = r.stdout.splitlines()
        self.assertTrue(out[0].startswith("FILE fixes ") and out[0].endswith("fixes.md"))
        # fixes は B の accepted、questions は A の value の gap（止めている決定は A と B の2件）
        self.assertEqual(out[-2:], ["PACE-STATUS: errors=0 failed_jobs=0 fixes=1 questions=1", "NEXT: continue"])
        q = (self.work / "questions.md").read_text(encoding="utf-8")
        self.assertIn("1. 価値判断: `足りない前提`（止めている決定 2件）", q)
        self.assertIn("この gap を持つ決定（行6）", q)
        self.assertIn("分類の段で出た別案: `別`", q)
        fx = (self.work / "fixes.md").read_text(encoding="utf-8")
        self.assertIn("1. 反論（accepted。missing）: `反論`", fx)
        rec = self.lines()[0]
        self.assertEqual((rec["kind"], rec["round"], rec["since_shown"], rec["fixes"], rec["questions"], rec["next"]),
                         ("report", 1, 1, 1, 1, "continue"))
        self.assertEqual(rec["defeaters"], {"accepted": 1, "refuted": 0, "needs_human": 0})

    def test_fixes_zero_shows(self):
        r = self.round_(review=lambda job: {"defeaters": []})
        self.assertEqual(r.stdout.splitlines()[-1], "NEXT: show")

    def test_same_plan_as_previous_round_shows(self):
        self.round_()
        r = self.round_()  # 直さずにもう1周
        self.assertEqual(r.stdout.splitlines()[-1], "NEXT: show")

    def test_three_rounds_since_shown_show(self):
        self.round_()
        self.edit(1)
        self.assertEqual(self.round_().stdout.splitlines()[-1], "NEXT: continue")
        self.edit(2)
        self.assertEqual(self.round_().stdout.splitlines()[-1], "NEXT: show")
        # shown を打たないまま4周目に入っても止まる（3以上）
        self.edit(3)
        self.assertEqual(self.round_().stdout.splitlines()[-1], "NEXT: show")
        self.assertEqual([x["since_shown"] for x in self.lines()], [1, 2, 3, 4])

    def test_first_round_after_shown_does_not_compare_with_before(self):
        self.round_()
        self.show_and_shown()
        r = self.round_()  # プランは同じだが、見せたあとの最初の周なので順1を当てない
        self.assertEqual(r.stdout.splitlines()[-1], "NEXT: continue")
        self.assertEqual(self.lines()[-1]["since_shown"], 1)

    def test_previous_round_with_failed_jobs_does_not_count_as_not_fixed(self):
        state = {"n": 0}

        def flaky(job):
            # 1周目は C の反論を失敗させる（答えを書かない）。2周目からは答える
            if state["n"] == 0 and job["input"]["target"]["plan_text"] == "決定Cを選ぶ。":
                return None
            return accept_b(job)
        r = self.round_(review=flaky)
        self.assertEqual(r.stdout.splitlines()[-2:], ["PACE-STATUS: errors=0 failed_jobs=1 fixes=1 questions=1",
                                                      "NEXT: continue"])
        state["n"] = 1
        # 前の周に失敗があったので、プランが同じでも順1を当てない
        self.assertEqual(self.round_(review=flaky).stdout.splitlines()[-1], "NEXT: continue")
        self.assertEqual(self.round_(review=flaky).stdout.splitlines()[-1], "NEXT: show")

    def test_failed_jobs_with_no_fixes_shows(self):
        r = self.round_(review=lambda job: None if job["input"]["target"]["plan_text"] == "決定Cを選ぶ。"
                        else {"defeaters": []})
        self.assertEqual(r.stdout.splitlines()[-2:], ["PACE-STATUS: errors=0 failed_jobs=1 fixes=0 questions=1",
                                                      "NEXT: show"])

    def test_report_twice_in_one_round_adds_no_line(self):
        first = self.round_().stdout
        second = self.report().stdout
        self.assertEqual(first, second)
        self.assertEqual(len(self.lines()), 1)

    def test_broken_rounds_lines_are_skipped(self):
        self.round_()
        path = self.work / "rounds.jsonl"
        path.write_text("壊れた行\n" + path.read_text() + '{"kind": "report"}\n', encoding="utf-8")
        self.edit(1)
        self.round_()
        self.assertEqual([x["round"] for x in self.lines()], [1, 2])
        self.assertNotIn("壊れた行", path.read_text(encoding="utf-8"))

    # --- 失敗したジョブ（性質3） --------------------------------------------

    def test_failed_jobs_are_not_fixes_or_questions(self):
        self.init()
        for _ in range(30):
            bundles, last = parse(self.next().stdout)
            if last.startswith("DONE"):
                break
            self.answer(bundles, decycle=lambda job: None)  # 輪を断つ段を失敗させる（A と B は U に入る）
        r = self.report()
        # A の value の gap は uncertain で数えない。B は U に入るので反論にもかけない
        self.assertEqual(r.stdout.splitlines()[-2], "PACE-STATUS: errors=0 failed_jobs=1 fixes=0 questions=0")
        self.show()
        show = (self.work / "show.md").read_text(encoding="utf-8")
        self.assertIn("失敗したジョブ: decycle_1_001_", show)
        self.assertIn("失敗の影響を受けうる決定（行6）", show)
        self.assertIn("失敗の影響を受けうる決定（行7）", show)

    def test_failed_classify_units_are_listed(self):
        self.plan.write_text("# 題\n\n## 設計\n\n前提の文。\n決定Aを選ぶ。\n\n## 別\n\n別の前提の文。\n", encoding="utf-8")
        self.init()
        texts = {u["id"]: u["text"] for u in pace.split_units(self.plan.read_text(encoding="utf-8"))}
        for _ in range(30):
            bundles, last = parse(self.next().stdout)
            if last.startswith("DONE"):
                break
            for _, _, paths in bundles:
                for p in paths:
                    job = json.loads(Path(p).read_text(encoding="utf-8"))
                    if job["stage"] == "classify" and job["variant"] == "A" and any(
                            texts[u["uid"]] == "別の前提の文。" for u in job["input"]["units"]):
                        continue
                    self.answer([("b", "a", [p])])
        r = self.report()
        self.assertEqual(r.stdout.splitlines()[-2], "PACE-STATUS: errors=0 failed_jobs=1 fixes=0 questions=0")
        report = (self.work / "report.md").read_text(encoding="utf-8")
        self.assertIn("確かめられなかった単位（行10）", report)
        self.assertIn("失敗の影響を受けうる決定（行6）", report)

    # --- show と shown（性質2・4） --------------------------------------------

    def show(self, ok=True):
        r = run("show", str(self.plan))
        if ok:
            self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def show_and_shown(self):
        out = self.show().stdout.splitlines()
        sha = out[-1].split()[1]
        r = run("shown", str(self.plan), sha)
        self.assertEqual(r.returncode, 0, r.stderr)
        return sha

    def test_show_is_the_same_bytes_and_has_no_absolute_path(self):
        self.round_()
        out = self.show().stdout.splitlines()
        self.assertTrue(out[0].startswith("FILE show ") and out[0].endswith("show.md"))
        self.assertEqual(out[1], "PACE-STATUS: errors=0 failed_jobs=0 fixes=1 questions=1")
        self.assertRegex(out[2], r"^SNAPSHOT [0-9a-f]{64}$")
        first = (self.work / "show.md").read_bytes()
        self.show()
        self.assertEqual((self.work / "show.md").read_bytes(), first)
        text = first.decode("utf-8")
        self.assertNotIn(str(self.dir), text)
        self.assertNotIn(os.path.realpath(str(self.dir)), text)
        self.assertTrue(text.startswith("# PACE の結果: plan.md\n"))
        self.assertTrue(text.endswith(pace.BOUNDARY + "\n"))
        for head in ("## 人間への質問（1件）", "## 残った直すもの（1件）", "## 確かめられなかったもの",
                     "## 最後に見せてからの差分", "## 注意"):
            self.assertIn(head, text)
        self.assertIn("初回（まだ人間に見せていない基準", text)
        self.assertEqual((self.work / "show_snapshot.md").read_bytes(), self.plan.read_bytes())

    def test_shown_moves_the_base_and_diff_follows(self):
        self.round_()
        sha = self.show_and_shown()
        self.assertEqual((self.work / "shown.md").read_bytes(), self.plan.read_bytes())
        self.assertFalse((self.work / "show_snapshot.md").exists())
        self.assertEqual(self.lines()[-1], {"kind": "shown", "after_round": 1, "snapshot_sha256": sha})
        # 同じコマンド行をもう一度打っても、写しが無いので何も変えずに 1
        r = run("shown", str(self.plan), sha)
        self.assertEqual(r.returncode, 1)
        self.assertIn("写し", r.stderr)
        self.edit(1)
        self.round_()
        self.show()
        show = (self.work / "show.md").read_text(encoding="utf-8")
        self.assertNotIn("初回", show)
        self.assertIn("+追記1の前提。", show)
        self.assertIn("```diff", show)

    def test_shown_rejects_and_changes_nothing(self):
        self.round_()
        self.show()
        before = (self.work / "shown.md").read_bytes()
        for args, msg in ((["0" * 64], "合いません"), (["abc"], "sha256 の形")):
            r = run("shown", str(self.plan), *args)
            self.assertEqual(r.returncode, 1)
            self.assertIn(msg, r.stderr)
        self.assertEqual((self.work / "shown.md").read_bytes(), before)
        self.assertTrue((self.work / "show_snapshot.md").exists())
        # symlink の shown.md は触らない
        sha = pace.hashlib.sha256(self.plan.read_bytes()).hexdigest()
        other = self.dir / "other.md"
        other.write_text("外", encoding="utf-8")
        (self.work / "shown.md").unlink()
        os.symlink(str(other), str(self.work / "shown.md"))
        r = run("shown", str(self.plan), sha)
        self.assertEqual(r.returncode, 1)
        self.assertIn("消してから init", r.stderr)
        self.assertEqual(other.read_text(encoding="utf-8"), "外")

    def test_init_recreated_base_is_first_time(self):
        self.round_()
        self.show_and_shown()
        (self.work / "shown.md").unlink()
        self.edit(1)
        self.round_()
        self.show()
        self.assertIn("初回（", (self.work / "show.md").read_text(encoding="utf-8"))

    def test_show_needs_review_done_and_shown_md(self):
        self.init()
        r = self.show(ok=False)
        self.assertEqual(r.returncode, 1)
        self.round_()
        self.show()
        (self.work / "shown.md").unlink()
        r = self.show(ok=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("init を打ち直して", r.stderr)
        # 1 で終わる道でも、古い写しは消える（前の show のコマンド行で繰り上げさせない）
        self.assertFalse((self.work / "show_snapshot.md").exists())

    # --- errors（性質5） ------------------------------------------------------

    def test_unreadable_plan(self):
        self.round_()
        self.show()  # 古い写しを残しておく
        self.plan.write_bytes(b"\xff\xfe")
        r = run("init", str(self.plan))
        self.assertEqual(r.stdout.splitlines(), [pace.UNREADABLE_STATUS, "NEXT: show"])
        r = self.show()
        self.assertEqual(r.stdout.splitlines()[1:], [pace.UNREADABLE_STATUS])
        self.assertEqual((self.work / "show.md").read_text(encoding="utf-8"), pace.unreadable_show("plan.md"))
        self.assertFalse((self.work / "show_snapshot.md").exists())

    def test_unreadable_plan_without_workdir(self):
        missing = self.dir / "missing.md"
        r = run("show", str(missing))
        self.assertEqual(r.returncode, 0, r.stderr)
        work = self.dir / ".pace" / "missing"
        self.assertEqual((self.dir / ".pace" / ".gitignore").read_text(), "*\n")
        self.assertTrue((work / "show.md").exists())
        self.assertFalse((work / "show_snapshot.md").exists())
        # source.json は書かない（あとの init が、別の名前のプランのものと取り違えない）
        self.assertFalse((work / "source.json").exists())
        missing.write_text(PLAN, encoding="utf-8")
        self.assertEqual(run("init", str(missing)).returncode, 0)

    def test_unreadable_plan_in_missing_directory(self):
        r = run("show", str(self.dir / "無い" / "plan.md"))
        self.assertEqual(r.returncode, 1)
        self.assertIn("書けませんでした", r.stderr)


if __name__ == "__main__":
    unittest.main()
