"""pace.py next の輪を断つ反復と、verify（結合と検証）の unittest。決まりは docs/plans/pace_design.md の16節。

強連結成分・輪を断つ答えの検証・強さと効きの計算は import して関数を直に確かめる。流れは test_link.py と同じく、
テストが実行役（subagent）の代わりに *.out.json を書き、next と verify を subprocess で叩いて確かめる。
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "pace" / "scripts"))
import pace  # noqa: E402

PACE = Path(__file__).resolve().parents[1] / "plugins" / "pace" / "scripts" / "pace.py"

PLAN = """# 題

## 設計

前提の文。
決定Aを選ぶ。
決定Bを選ぶ。
決定Cを選ぶ。
"""

# 根拠付けで、決定（原文）ごとに理由にする候補の原文。A と B は互いを理由にし合う（輪）。C は前提から根に届く
SUPPORT = {"決定Aを選ぶ。": ["決定Bを選ぶ。"], "決定Bを選ぶ。": ["決定Aを選ぶ。"], "決定Cを選ぶ。": ["前提の文。"]}


def run(*args):
    return subprocess.run([sys.executable, str(PACE), *args], capture_output=True, text=True)


def parse(out):
    bundles = []
    last = None
    for line in out.splitlines():
        if line.startswith("BUNDLE "):
            _, bid, agent = line.split(" ", 2)
            bundles.append((bid, agent, []))
        elif line.startswith("JOB "):
            bundles[-1][2].append(line[len("JOB "):])
        else:
            last = line
    return bundles, last


def link_for(job):
    """SUPPORT の候補が入力にあれば、それを理由にする。外されていれば gap にする。"""
    target = job["input"]["target"]
    ids = {g["plan_text"]: n["id"] for g in job["input"]["candidates"] for n in g["nodes"]}
    froms = [ids[t] for t in SUPPORT[target["plan_text"]] if t in ids]
    if froms:
        return {"claims": [], "gaps": [], "need": [],
                "steps": [{"from": froms, "to": target["id"], "warrant": "理由"}]}
    return {"claims": [], "gaps": [{"id": "g1", "ptype": "value", "text": "足りない前提"}], "need": [],
            "steps": [{"from": ["g1"], "to": target["id"], "warrant": "理由"}]}


def cut_b_to_a(job):
    """B → A の辺を切る。"""
    ids = {n["plan_text"]: n["id"] for n in job["input"]["nodes"]}
    return {"cuts": [{"from": ids["決定Bを選ぶ。"], "to": ids["決定Aを選ぶ。"], "reason": "並列の決定で理由ではない"}]}


class GraphTest(unittest.TestCase):
    """強連結成分・輪を断つ答えの検証・強さ・効き（import して確かめる）。"""

    def test_strong_components(self):
        nodes = ["a", "b", "c", "d", "e"]
        comps = pace.strong_components(nodes, [("b", "a"), ("a", "b"), ("c", "d"), ("d", "e"), ("e", "c"), ("a", "c")])
        self.assertEqual(comps, [["a", "b"], ["c", "d", "e"]])
        self.assertEqual(pace.strong_components(nodes, [("a", "b"), ("b", "c")]), [])

    def test_strong_components_deep_chain_does_not_recurse(self):
        n = 5000
        nodes = ["n%d" % i for i in range(n)]
        edges = [(nodes[i], nodes[i + 1]) for i in range(n - 1)] + [(nodes[-1], nodes[0])]
        self.assertEqual(len(pace.strong_components(nodes, edges)[0]), n)

    def decycle_input(self):
        return {"nodes": [{"id": "D_1", "text": "a", "plan_text": "a"}, {"id": "D_2", "text": "b", "plan_text": "b"}],
                "edges": [{"from": "D_1", "to": "D_2", "warrants": ["w"]}, {"from": "D_2", "to": "D_1", "warrants": ["w"]}]}

    def test_check_decycle(self):
        inp = self.decycle_input()
        ok = pace.check_decycle({"cuts": [{"from": "D_2", "to": "D_1", "reason": "r", "x": 1}]}, inp)
        self.assertEqual(ok, {"cuts": [{"from": "D_2", "to": "D_1", "reason": "r"}]})
        bad = [
            ({"cuts": []}, "空でない配列"),
            ({"cuts": [{"from": "D_1", "to": "D_3", "reason": "r"}]}, "input.edges にありません"),
            ({"cuts": [{"from": "D_2", "to": "D_1", "reason": "r"}, {"from": "D_2", "to": "D_1", "reason": "r"}]}, "2回"),
            ({"cuts": [{"from": "D_2", "to": "D_1", "reason": " "}]}, "reason"),
        ]
        for answer, msg in bad:
            with self.assertRaisesRegex(ValueError, msg):
                pace.check_decycle(answer, inp)
        three = {"nodes": [{"id": x, "text": x, "plan_text": x} for x in ("D_1", "D_2", "D_3")],
                 "edges": [{"from": f, "to": t, "warrants": []} for f, t in
                           (("D_1", "D_2"), ("D_2", "D_1"), ("D_2", "D_3"), ("D_3", "D_2"))]}
        with self.assertRaisesRegex(ValueError, "輪が残ります"):
            pace.check_decycle({"cuts": [{"from": "D_2", "to": "D_1", "reason": "r"}]}, three)

    @staticmethod
    def answer(froms, gaps=()):
        return {"claims": [], "gaps": [{"id": g, "ptype": "fact", "text": "t"} for g in gaps], "need": [],
                "steps": [{"from": list(froms), "to": None, "warrant": "w"}]}

    def answers(self, spec):
        out = {}
        for d, (froms, gaps) in spec.items():
            a = self.answer(froms, gaps)
            a["steps"][0]["to"] = d
            out[d] = a
        return out

    def test_strengths(self):
        ids = ["A", "B", "C", "D", "E"]
        answers = self.answers({"A": (["B"], ()), "B": (["A"], ()), "C": (["P"], ()), "D": (["C", "g1"], ["g1"]),
                                "E": (["C", "A"], ())})
        v = pace.strengths(ids, answers, premises=frozenset({"P"}))
        self.assertEqual(v, {"A": pace.UNGROUNDED, "B": pace.UNGROUNDED, "C": pace.ROOTED, "D": pace.NEEDS_GAP,
                             "E": pace.UNGROUNDED})
        # 輪の中でも、外の前提に根を持つ決定を理由にした決定は根に届く
        answers["A"]["steps"].append({"from": ["P"], "to": "A", "warrant": "w"})
        v = pace.strengths(ids, answers, premises=frozenset({"P"}))
        self.assertEqual((v["A"], v["B"], v["E"]), (pace.ROOTED, pace.ROOTED, pace.ROOTED))

    def test_leverage(self):
        ids = ["A", "B", "C", "U"]
        answers = self.answers({"A": (["g1"], ["g1"]), "B": (["A"], ()), "C": (["g1", "g2"], ["g1", "g2"]),
                                "U": (["g1"], ["g1"])})
        status = {"A": "needs_gap", "B": "needs_gap", "C": "ungrounded", "U": "unverified"}
        lev = pace.leverage(ids, answers, status)
        self.assertEqual(lev, {("A", "g1"): 2})


class DecycleFlowTest(unittest.TestCase):
    def setUp(self):
        self._umask = os.umask(0o022)
        self.addCleanup(os.umask, self._umask)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.plan = self.dir / "plan.md"
        self.plan.write_text(PLAN, encoding="utf-8")
        self.work = self.dir / ".pace" / "plan"

    def init(self):
        r = run("init", str(self.plan))
        self.assertEqual(r.returncode, 0, r.stderr)

    def next(self, ok=True):
        r = run("next", str(self.plan))
        if ok:
            self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def answer(self, bundles, link=link_for, decycle=cut_b_to_a):
        texts = {u["id"]: u["text"] for u in pace.split_units(self.plan.read_text(encoding="utf-8"))}
        for _, _, paths in bundles:
            for p in paths:
                job = json.loads(Path(p).read_text(encoding="utf-8"))
                if job["stage"] == "classify":
                    ans = {"units": [{"uid": u["uid"], "labels": [
                        {"kind": "decision", "text": texts[u["uid"]], "alternatives": ["別"]}
                        if texts[u["uid"]].startswith("決定") else
                        {"kind": "premise", "ptype": "fact", "text": texts[u["uid"]]}]}
                        for u in job["input"]["units"]]}
                elif job["stage"].startswith("link_"):
                    ans = link(job)
                elif job["stage"].startswith("decycle_"):
                    ans = decycle(job)
                else:
                    ans = {"labels": []}
                if ans is not None:
                    Path(job["out_path"]).write_text(json.dumps(ans, ensure_ascii=False), encoding="utf-8")

    def drive(self, link=link_for, decycle=cut_b_to_a, limit=30):
        waves = []
        for _ in range(limit):
            bundles, last = parse(self.next().stdout)
            if last.startswith("DONE"):
                return last, waves
            waves.append(bundles)
            self.answer(bundles, link, decycle)
        self.fail("DONE に届かない")

    def ids(self):
        rnd = pace.Round(str(self.plan), readonly=True)
        return {rnd.units[rnd.pos[n["unit"]]]["text"]: n["id"] for n in rnd.decisions()}

    def verify(self, ok=True):
        r = run("verify", str(self.plan))
        if ok:
            self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def test_cycle_is_cut_and_relinked(self):
        self.init()
        last, waves = self.drive()
        self.assertEqual(last, "DONE decisions=3 premises=3 failed_jobs=0")
        stages = [sorted({json.loads(Path(p).read_text(encoding="utf-8"))["stage"] for _, _, ps in w for p in ps})
                  for w in waves]
        self.assertIn(["decycle_1"], stages)
        self.assertIn(["link_r1_2"], stages)
        dec = [w for w, s in zip(waves, stages) if s == ["decycle_1"]][0]
        self.assertEqual([a for _, a, _ in dec], ["pace:pace-judge"])
        job = json.loads(Path(dec[0][2][0]).read_text(encoding="utf-8"))
        self.assertRegex(Path(dec[0][2][0]).name, r"^decycle_1_001_[0-9a-f]{8}\.json$")
        self.assertEqual(job["instructions"], pace.INSTRUCTIONS["decycle"])
        self.assertEqual([n["plan_text"] for n in job["input"]["nodes"]], ["決定Aを選ぶ。", "決定Bを選ぶ。"])
        self.assertEqual(len(job["input"]["edges"]), 2)
        # 回2は切られた A だけを根拠付けし直し、B を外して excluded_note に入れる
        relink = [w for w, s in zip(waves, stages) if s == ["link_r1_2"]][0]
        self.assertEqual(sum(len(ps) for _, _, ps in relink), 1)
        job = json.loads(Path(relink[0][2][0]).read_text(encoding="utf-8"))
        ids = self.ids()
        self.assertEqual(job["input"]["target"]["id"], ids["決定Aを選ぶ。"])
        self.assertEqual(job["input"]["excluded_note"], [{"text": "決定Bを選ぶ。", "plan_text": "決定Bを選ぶ。"}])
        self.assertNotIn(ids["決定Bを選ぶ。"], [n["id"] for g in job["input"]["candidates"] for n in g["nodes"]])
        # 回2で輪が無いので decycle_2 は0件で終わり、後の段は作らない
        self.assertEqual(json.loads((self.work / "jobs" / "decycle_2" / "created.json").read_text())["jobs"], [])
        self.assertFalse((self.work / "jobs" / "link_r1_3").exists())
        out = self.verify().stdout.splitlines()
        self.assertEqual(out[0], "VERIFY decisions=3 rooted=1 needs_gap=2 ungrounded=0 unverified=0 local=2 cycles=0 "
                                 "gaps=1 uncertain=0 unverified_units=0")
        self.assertIn("DECISION %s rooted local" % ids["決定Cを選ぶ。"], out)
        self.assertIn("DECISION %s needs_gap" % ids["決定Aを選ぶ。"], out)
        # B は A（needs_gap）を理由にするので needs_gap だが、A を rooted とみなせば自前の推論は完結する
        self.assertIn("DECISION %s needs_gap local" % ids["決定Bを選ぶ。"], out)
        self.assertIn("GAP G_%s_g1 value leverage=2" % ids["決定Aを選ぶ。"], out)
        # DONE のあとに打ち直しても同じ DONE
        self.assertEqual(parse(self.next().stdout)[1], last)

    def test_memory_applies_from_the_start_in_next_round(self):
        self.init()
        self.drive()
        self.init()
        last, waves = self.drive()
        self.assertEqual(waves, [])  # 全部キャッシュに当たり、輪も最初から無い
        self.assertEqual(last, "DONE decisions=3 premises=3 failed_jobs=0")
        rnd = pace.Round(str(self.plan), readonly=True)
        ids = self.ids()
        inp = rnd.link_input("link_r1_1", ids["決定Aを選ぶ。"])
        self.assertEqual(inp["excluded_note"], [{"text": "決定Bを選ぶ。", "plan_text": "決定Bを選ぶ。"}])
        self.assertEqual(rnd.final_pass(), 1)

    def test_lost_memory_after_done_stops(self):
        self.init()
        self.drive()
        rnd = pace.Round(str(self.plan), readonly=True)
        node = rnd.node_by_id(self.ids()["決定Aを選ぶ。"])
        (self.work / "cache" / (rnd.memory_key(node) + ".json")).unlink()
        r = self.next(ok=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("init を打ち直して", r.stderr)
        r = self.verify(ok=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("init を打ち直して", r.stderr)

    def test_decycle_failure_is_unverified_not_cycle(self):
        self.init()
        last, _ = self.drive(decycle=lambda job: None)
        self.assertEqual(last, "DONE decisions=3 premises=3 failed_jobs=1")
        rec = json.loads((self.work / "failed_jobs.jsonl").read_text().splitlines()[0])
        ids = self.ids()
        self.assertEqual(rec["stage"], "decycle_1")
        self.assertEqual(rec["units"], [])
        self.assertEqual(rec["decisions"], [ids["決定Aを選ぶ。"], ids["決定Bを選ぶ。"]])
        out = self.verify().stdout.splitlines()
        self.assertTrue(out[0].startswith("VERIFY decisions=3 rooted=1 needs_gap=0 ungrounded=0 unverified=2"), out[0])
        self.assertIn("cycles=0", out[0])
        self.assertFalse([x for x in out if x.startswith("CYCLE")])

    def test_link_failure_is_unverified(self):
        self.init()

        def fail_c(job):
            return None if job["input"]["target"]["plan_text"] == "決定Cを選ぶ。" else link_for(job)
        last, _ = self.drive(link=fail_c)
        self.assertEqual(last, "DONE decisions=3 premises=3 failed_jobs=1")
        out = self.verify().stdout.splitlines()
        self.assertIn("DECISION %s unverified" % self.ids()["決定Cを選ぶ。"], out)

    def test_verify_before_done_and_reads_only(self):
        self.init()
        r = self.verify(ok=False)
        self.assertEqual(r.returncode, 1)
        self.assertFalse((self.work / "jobs" / "round.json").exists())
        self.next()
        r = self.verify(ok=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("next を DONE まで回して", r.stderr)
        self.drive()
        before = (self.work / "nodes.jsonl").read_bytes()
        self.verify()
        self.assertEqual((self.work / "nodes.jsonl").read_bytes(), before)

    def test_rewritten_decycle_job_is_rejected(self):
        self.init()
        for _ in range(10):
            bundles, last = parse(self.next().stdout)
            paths = [p for _, _, ps in bundles for p in ps]
            if paths and Path(paths[0]).name.startswith("decycle_"):
                job = json.loads(Path(paths[0]).read_text(encoding="utf-8"))
                job["input"]["edges"] = job["input"]["edges"][:1]
                Path(paths[0]).write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
                r = self.next(ok=False)
                self.assertEqual(r.returncode, 1)
                self.assertIn("形が違います", r.stderr)
                return
            self.answer(bundles)
        self.fail("輪を断つ波が出ない")

    def test_versions_and_instructions(self):
        self.assertEqual(dict(pace.STAGE_VERSIONS)["link"], "2026-10-17.1")
        self.assertEqual(dict(pace.STAGE_VERSIONS)["decycle"], "2026-10-17.1")
        self.assertIn("input.excluded_note", pace.INSTRUCTIONS["link"])
        self.assertEqual(pace.STAGE_AGENT["decycle"], "pace-judge")
        self.assertEqual(pace.JOB_STAGES[-1], "link_r2_6")
        self.assertEqual(sum(1 for s in pace.JOB_STAGES if s.startswith("decycle_")), 5)


if __name__ == "__main__":
    unittest.main()
