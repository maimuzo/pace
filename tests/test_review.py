"""pace.py next の反論の段（review）の unittest。決まりは docs/plans/pace_design.md の17節。

反論の答えの検証は import して関数を直に確かめる。流れは test_decycle.py の道具（FlowBase）で、テストが実行役の代わりに
*.out.json を書き、next・verify・report を subprocess で叩いて確かめる。
"""

import json
import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "pace" / "scripts"))
import pace  # noqa: E402
from test_decycle import FlowBase, link_for, parse, run  # noqa: E402

# 推論で使った前提 P_1・P_3、候補として足しただけの前提 P_2、ほかの決定 D_2、claim c1
INPUT = {"target": {"id": "D_1", "text": "t", "alternatives": ["a"], "plan_text": "p"},
         "nodes": [{"id": "P_1", "kind": "fact", "text": "f1", "plan_text": "f1"},
                   {"id": "P_3", "kind": "fact", "text": "f3", "plan_text": "f3"},
                   {"id": "D_2", "kind": "decision", "text": "d", "plan_text": "d"},
                   {"id": "c1", "kind": "claim", "text": "c"},
                   {"id": "P_2", "kind": "value", "text": "v", "plan_text": "v"}],
         "steps": [{"id": "s1", "from": ["P_1", "D_2"], "to": "c1", "warrant": "w"},
                   {"id": "s2", "from": ["c1", "P_3"], "to": "D_1", "warrant": "w"}]}


def defeater(target="D_1", status="accepted", by=(), **kw):
    d = {"target": target, "type": "missing", "text": "反論", "status": status, "by": list(by)}
    d.update(kw)
    return d


class CheckReviewTest(unittest.TestCase):
    def test_accepts_premise_based_refutations(self):
        out = pace.check_review({"defeaters": [
            defeater(), defeater("s1", "refuted", ["P_2"], by_quotes=["引用"], x=1),
            defeater("c1", "needs_human", ["P_1"], note="注")]}, INPUT)
        self.assertEqual(out["defeaters"][0], {"target": "D_1", "type": "missing", "text": "反論", "status": "accepted",
                                               "by": [], "note": ""})
        # 候補として足しただけの前提でも反証できる。by_quotes と知らないキーは捨てる
        self.assertEqual(out["defeaters"][1]["by"], ["P_2"])
        self.assertNotIn("by_quotes", out["defeaters"][1])
        # accepted と needs_human の by は捨てる（差し戻さない）
        self.assertEqual(out["defeaters"][2]["by"], [])
        self.assertEqual(pace.check_review({"defeaters": []}, INPUT), {"defeaters": []})

    def test_rejects(self):
        bad = [
            ({"defeaters": [defeater("D_2")]}, "ほかの決定への反論は出さない"),
            # 候補として足しただけの前提は target にしない（決定が拠っていない前提への反論を通さない）
            ({"defeaters": [defeater("P_2")]}, "ほかの決定への反論は出さない"),
            ({"defeaters": [defeater(type="style")]}, "type"),
            ({"defeaters": [defeater(status="done")]}, "status"),
            ({"defeaters": [defeater(text=" ")]}, "text"),
            ({"defeaters": [defeater(status="refuted")]}, "by が空"),
            ({"defeaters": [defeater(status="refuted", by=["c1"])]}, "前提"),
            ({"defeaters": [defeater(status="refuted", by=["D_2"])]}, "前提"),
            ({"defeaters": [defeater(status="refuted", by=["s1"])]}, "前提"),
            ({"defeaters": [defeater(status="refuted", by=["D_1"])]}, "前提"),
            # 反論の target の前提を、その反論の根拠にしない
            ({"defeaters": [defeater("P_1", "refuted", ["P_1"])]}, "循環"),
            # 同じ答えのほかの反論が target にしている前提も根拠にしない（refuted どうしの持ち合いも断つ）
            ({"defeaters": [defeater("P_1", "refuted", ["P_3"]), defeater("P_3", "refuted", ["P_1"])]}, "循環"),
            ({"defeaters": [defeater("P_1", "accepted"), defeater("s1", "refuted", ["P_1"])]}, "循環"),
            # 型を先に確かめる（set に照らして TypeError で落ちない）
            ({"defeaters": [defeater(target=["s1"])]}, "文字列ではありません"),
            ({"defeaters": [dict(defeater(), by="P_1")]}, "文字列の配列"),
            ({"defeaters": [defeater(note=1)]}, "note"),
            ({"defeaters": "x"}, "配列"),
            ([], "オブジェクト"),
        ]
        for answer, msg in bad:
            with self.assertRaisesRegex(ValueError, msg):
                pace.check_review(answer, INPUT)

    def test_versions_and_instructions(self):
        self.assertEqual(dict(pace.STAGE_VERSIONS)["review"], "2026-10-17.1")
        self.assertEqual(pace.STAGE_AGENT["review"], "pace-judge")
        self.assertNotIn(pace.REVIEW_STAGE, pace.JOB_STAGES)
        text = pace.INSTRUCTIONS["review"]
        self.assertNotIn("by_quotes", text)
        self.assertNotIn("既存の推論", text)
        self.assertIn("by にその前提の id を1つ以上挙げる", text)
        self.assertIn("accepted と needs_human の by は空にする", text)


class ReviewFlowTest(FlowBase):
    """基本の流れ（A は gap を持つ、B は A を理由にし gap を持たない、C は前提から根に届く）。"""

    def review_jobs(self, waves):
        return [json.loads(Path(p).read_text(encoding="utf-8")) for w in waves for _, _, ps in w for p in ps
                if Path(p).name.startswith("review_")]

    def test_review_targets_local_decisions_without_gaps(self):
        self.init()
        last, waves = self.drive()
        self.assertEqual(last, "DONE decisions=3 premises=3 failed_jobs=0")
        jobs = self.review_jobs(waves)
        ids = self.ids()
        # A は gap を持つので反論にかけない。B（needs_gap だが local で gap 無し）と C（rooted）をかける
        self.assertEqual(sorted(j["input"]["target"]["id"] for j in jobs),
                         sorted([ids["決定Bを選ぶ。"], ids["決定Cを選ぶ。"]]))
        dec = [w for w in waves if any(Path(p).name.startswith("review_") for _, _, ps in w for p in ps)][0]
        self.assertEqual([a for _, a, _ in dec], ["pace:pace-judge"])
        self.assertRegex(Path(dec[0][2][0]).name, r"^review_D_[0-9a-f]{8}_1_[0-9a-f]{8}\.json$")
        self.assertEqual(jobs[0]["instructions"], pace.INSTRUCTIONS["review"])
        b = next(j for j in jobs if j["input"]["target"]["id"] == ids["決定Bを選ぶ。"])
        # B の推論に出ない候補の前提も、反証の根拠に使えるよう入力に入る
        kinds = {n["plan_text"]: n["kind"] for n in b["input"]["nodes"]}
        self.assertEqual(kinds["決定Aを選ぶ。"], "decision")
        self.assertEqual(kinds["前提の文。"], "fact")
        self.assertEqual(b["input"]["steps"][0]["id"], "s1")
        # verify は反論の段を見ない（#6 の出力のまま）
        self.assertTrue(self.verify().stdout.startswith("VERIFY decisions=3 rooted=1 needs_gap=2"))

    def test_decision_with_a_gap_is_not_reviewed_and_same_questions_merge(self):
        """C は前提から根に届くが value の gap も持つ（rooted で local）。gap があるので反論にかけない。A と同じ本文の
        gap は questions で1項目にまとまる。"""
        def link(job):
            answer = link_for(job)
            if job["input"]["target"]["plan_text"] == "決定Cを選ぶ。":
                answer["gaps"] = [{"id": "g1", "ptype": "value", "text": "足りない前提"}]
                answer["steps"].append({"from": ["g1"], "to": job["input"]["target"]["id"], "warrant": "別案を退ける"})
            return answer
        self.init()
        last, waves = self.drive(link=link)
        ids = self.ids()
        self.assertEqual([j["input"]["target"]["id"] for j in self.review_jobs(waves)], [ids["決定Bを選ぶ。"]])
        r = run("report", str(self.plan))
        self.assertEqual(r.stdout.splitlines()[-2], "PACE-STATUS: errors=0 failed_jobs=0 fixes=0 questions=1")
        q = (self.work / "questions.md").read_text(encoding="utf-8")
        self.assertEqual(q.count("この gap を持つ決定"), 2)

    def test_tampered_cache_answer_is_checked_again(self):
        self.init()
        self.drive_with_review(lambda job: {"defeaters": []})
        for p in (self.work / "cache").rglob("*"):
            if p.is_file() and "defeaters_answer" in p.read_text(encoding="utf-8", errors="replace"):
                obj = json.loads(p.read_text(encoding="utf-8"))
                obj["defeaters_answer"] = {"defeaters": [{"target": "D_x", "type": "logic", "text": "t",
                                                          "status": "accepted", "by": []}]}
                p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
        r = run("report", str(self.plan))
        self.assertEqual(r.returncode, 1)
        self.assertIn("反論の答えがありません", r.stderr)

    def test_rewritten_earlier_stage_stops_the_review_wave(self):
        self.init()
        for _ in range(30):
            bundles, last = parse(self.next().stdout)
            if any(Path(p).name.startswith("review_") for _, _, ps in bundles for p in ps):
                break
            self.answer(bundles)
        (self.work / "jobs" / "link_r1_2" / "created.json").unlink()
        r = self.next(ok=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("反論の段の前の段が揃っていません", r.stderr)

    def test_verify_does_not_wait_for_review(self):
        self.init()
        for _ in range(30):
            bundles, last = parse(self.next().stdout)
            paths = [p for _, _, ps in bundles for p in ps]
            if paths and Path(paths[0]).name.startswith("review_"):
                self.verify()  # 反論の段のジョブが残っていても、verify は 0 で終わる
                r = run("report", str(self.plan))
                self.assertEqual(r.returncode, 1)
                self.assertIn("next を DONE まで回して", r.stderr)
                return
            self.answer(bundles)
        self.fail("反論の波が出ない")

    def test_review_failure_is_unverified_only_for_that_decision(self):
        self.init()
        ids = self.ids_after_classify()
        c = ids["決定Cを選ぶ。"]
        last, _ = self.drive_with_review(lambda job: None if job["input"]["target"]["id"] == c else {"defeaters": []})
        self.assertEqual(last, "DONE decisions=3 premises=3 failed_jobs=1")
        rec = json.loads((self.work / "failed_jobs.jsonl").read_text().splitlines()[0])
        self.assertEqual((rec["stage"], rec["units"], rec["decision"]), ("review", [], c))
        r = run("report", str(self.plan))
        self.assertEqual(r.returncode, 0, r.stderr)
        report = (self.work / "report.md").read_text(encoding="utf-8")
        self.assertIn("反論を確かめられなかった決定", report)
        # C の状態はふつうに数える（verify も同じ）
        self.assertIn("DECISION %s rooted local" % c, self.verify().stdout)

    def test_lost_review_answer_after_done_stops(self):
        self.init()
        self.drive()
        rnd = pace.Round(str(self.plan), readonly=True)
        c = self.ids()["決定Cを選ぶ。"]
        key = rnd.cache_key("review", rnd.review_input(c))
        (self.work / "cache" / (key + ".json")).unlink()
        r = self.next(ok=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("反論の答えがありません", r.stderr)
        self.assertIn("init を打ち直して", r.stderr)
        r = run("report", str(self.plan))
        self.assertEqual(r.returncode, 1)
        # init を打ち直せば作り直す
        self.init()
        last, _ = self.drive()
        self.assertEqual(last, "DONE decisions=3 premises=3 failed_jobs=0")

    def test_rewritten_review_job_is_rejected(self):
        self.init()
        for _ in range(30):
            bundles, last = parse(self.next().stdout)
            paths = [p for _, _, ps in bundles for p in ps]
            if paths and Path(paths[0]).name.startswith("review_"):
                job = json.loads(Path(paths[0]).read_text(encoding="utf-8"))
                job["input"]["nodes"] = job["input"]["nodes"][:1]
                Path(paths[0]).write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
                r = self.next(ok=False)
                self.assertEqual(r.returncode, 1)
                self.assertIn("形が違います", r.stderr)
                return
            self.answer(bundles)
        self.fail("反論の波が出ない")

    def test_bad_answer_is_sent_back(self):
        self.init()
        for _ in range(30):
            bundles, last = parse(self.next().stdout)
            paths = [p for _, _, ps in bundles for p in ps]
            if paths and Path(paths[0]).name.startswith("review_"):
                for p in paths:
                    job = json.loads(Path(p).read_text(encoding="utf-8"))
                    bad = {"defeaters": [defeater(job["input"]["target"]["id"], "refuted", ["c9"])]}
                    Path(job["out_path"]).write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8")
                bundles, _ = parse(self.next().stdout)
                job = json.loads(Path(bundles[0][2][0]).read_text(encoding="utf-8"))
                self.assertEqual(job["attempts"], 1)
                self.assertIn("前提", job["previous_errors"][0])
                return
            self.answer(bundles)
        self.fail("反論の波が出ない")

    # --- 道具 ---------------------------------------------------------------

    def ids_after_classify(self):
        """分類の答えを書いたあとの決定の ID（原文 → ID）。決定の ID は単位の本文で決まるので、分割から作れる。"""
        out = {}
        for u in pace.split_units(self.plan.read_text(encoding="utf-8")):
            if u["text"].startswith("決定"):
                out[u["text"]] = "D_%s_1" % u["id"][1:]
        return out

    def drive_with_review(self, review, limit=30):
        for _ in range(limit):
            bundles, last = parse(self.next().stdout)
            if last.startswith("DONE"):
                return last, None
            self.answer(bundles, review=review)
        self.fail("DONE に届かない")


if __name__ == "__main__":
    unittest.main()
