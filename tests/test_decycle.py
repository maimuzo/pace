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
    froms = [ids[t] for t in SUPPORT.get(target["plan_text"], []) if t in ids]
    if froms:
        return {"claims": [], "gaps": [], "need": [],
                "steps": [{"from": froms, "to": target["id"], "warrant": "理由"}]}
    return {"claims": [], "gaps": [{"id": "g1", "ptype": "value", "text": "足りない前提"}], "need": [],
            "steps": [{"from": ["g1"], "to": target["id"], "warrant": "理由"}]}


def cut_b_to_a(job):
    """B → A の辺を切る。"""
    ids = {n["plan_text"]: n["id"] for n in job["input"]["nodes"]}
    return {"cuts": [{"from": ids["決定Bを選ぶ。"], "to": ids["決定Aを選ぶ。"], "reason": "並列の決定で理由ではない"}]}


def ring_link(job):
    """決定 i は、候補にある決定 i+1, i+2, … のうち最初のものを理由にする（7つの決定の輪。切るたびに輪が1つ縮む）。"""
    target = job["input"]["target"]
    ids = {g["plan_text"]: n["id"] for g in job["input"]["candidates"] for n in g["nodes"]}
    i = int(target["plan_text"][2])
    for off in range(1, 7):
        text = "決定%dを選ぶ。" % ((i + off - 1) % 7 + 1)
        if text in ids:
            return {"claims": [], "gaps": [], "need": [],
                    "steps": [{"from": [ids[text]], "to": target["id"], "warrant": "理由"}]}
    return {"claims": [], "gaps": [{"id": "g1", "ptype": "fact", "text": "t"}], "need": [],
            "steps": [{"from": ["g1"], "to": target["id"], "warrant": "理由"}]}


def cut_first(job):
    e = job["input"]["edges"][0]
    return {"cuts": [{"from": e["from"], "to": e["to"], "reason": "理由ではない"}]}


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

    def test_r2_trimmed_to_nothing_is_seen_by_pick_additions(self):
        """字数で r2 を作れなかった決定（足す候補はあったが全部削った）を、pick_additions と trim_r2 で見分けられる。"""
        units = pace.split_units("# 題\n\n## 節\n\n決める。\n\n## 遠く\n\n" + "".join("埋め%d。\n\n" % i for i in range(9))
                                 + "理由の文がここにある。\n")
        nodes = []
        for u in units:
            kind = "decision" if u["text"] == "決める。" else "premise"
            node = {"id": ("D_" if kind == "decision" else "P_") + u["id"][1:] + "_1", "kind": kind, "text": u["text"],
                    "unit": u["id"], "line": u["line"], "sec": u["sec"], "path": u["path"]}
            if kind == "decision":
                node["alternatives"] = ["別"]
            else:
                node["ptype"] = "fact"
            nodes.append(node)
        target = next(n for n in nodes if n["kind"] == "decision")
        r1 = pace.select_candidates(units, nodes, target)
        answer = {"claims": [], "gaps": [], "steps": [], "need": ["理由の文"]}
        added = pace.pick_additions(units, nodes, target, r1, answer)
        self.assertTrue(added)
        self.assertIsNotNone(pace.expand_candidates(units, nodes, target, r1, answer))
        old = pace.LINK_JOB_MAX_CHARS
        pace.LINK_JOB_MAX_CHARS = pace.link_size(r1) + 1
        try:
            self.assertIsNone(pace.expand_candidates(units, nodes, target, r1, answer))
            self.assertTrue(pace.pick_additions(units, nodes, target, r1, answer))
        finally:
            pace.LINK_JOB_MAX_CHARS = old
        # 外す決定は 2-gram の追加候補にも入らない
        far = next(n for n in nodes if n["text"] == "理由の文がここにある。")
        self.assertNotIn(far["id"], [n["id"] for _, n in pace.pick_additions(units, nodes, target, r1, answer,
                                                                            excluded=frozenset({far["id"]}))])

    def test_leverage(self):
        ids = ["A", "B", "C", "U"]
        answers = self.answers({"A": (["g1"], ["g1"]), "B": (["A"], ()), "C": (["g1", "g2"], ["g1", "g2"]),
                                "U": (["g1"], ["g1"])})
        status = {"A": "needs_gap", "B": "needs_gap", "C": "ungrounded", "U": "unverified"}
        lev = pace.leverage(ids, answers, status)
        self.assertEqual(lev, {("A", "g1"): 2})


class FlowBase(unittest.TestCase):
    """流れのテストの道具（next と verify を subprocess で叩き、実行役の代わりに答えを書く）。"""

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

    def answer(self, bundles, link=link_for, decycle=cut_b_to_a, review=lambda job: {"defeaters": []}):
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
                elif job["stage"] == "review":
                    ans = review(job)
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


class DecycleFlowTest(FlowBase):
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
        # verify は walk の痕跡の確かめ（切った辺の記憶）で止まる
        self.assertIn("切った辺の記憶がありません", r.stderr)
        self.assertIn("init を打ち直して", r.stderr)

    def test_corrupt_memory_is_read_as_empty(self):
        self.init()
        self.drive()
        rnd = pace.Round(str(self.plan), readonly=True)
        node = rnd.node_by_id(self.ids()["決定Aを選ぶ。"])
        self.assertEqual(len(rnd.memory_get(node)), 1)
        (self.work / "cache" / (rnd.memory_key(node) + ".json")).write_text('{"cuts": [{"from_text": 1}]}')
        self.assertEqual(pace.Round(str(self.plan), readonly=True).memory_get(node), [])

    def test_memory_of_another_pass_or_round_does_not_pass_the_check(self):
        """walk の確かめは、いまの周の印と pass=k の記憶だけを数える。理由は書き直す。"""
        self.init()
        self.drive()
        rnd = pace.Round(str(self.plan))
        a = rnd.node_by_id(self.ids()["決定Aを選ぶ。"])
        b = rnd.node_by_id(self.ids()["決定Bを選ぶ。"])
        path = self.work / "cache" / (rnd.memory_key(a) + ".json")
        good = path.read_text(encoding="utf-8")
        # pass を別の回にすると walk の痕跡の確かめで、round を前の周の印にすると（回1から効くので）回1の入力の
        # 作り直しで、どちらも 1 で終わる
        for key, value, message in (("pass", 2, "切った辺の記憶がありません"), ("round", "00000000", "形が違います")):
            obj = json.loads(good)
            obj["cuts"][0][key] = value
            path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
            r = self.verify(ok=False)
            self.assertEqual(r.returncode, 1, key)
            self.assertIn(message, r.stderr)
            self.assertIn("init を打ち直して", r.stderr)
        path.write_text(good, encoding="utf-8")
        rid = rnd.round_id()
        rnd.memory_add(a, b, "新しい理由", rid, 1)
        cuts = pace.Round(str(self.plan), readonly=True).memory_get(a)
        self.assertEqual([(c["reason"], c["round"], c["pass"]) for c in cuts], [("新しい理由", rid, 1)])
        self.verify()

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

        support = {"決定Aを選ぶ。": ["前提の文。"], "決定Bを選ぶ。": ["決定Cを選ぶ。"], "決定Cを選ぶ。": ["前提の文。"]}

        def fail_c(job):
            if job["input"]["target"]["plan_text"] == "決定Cを選ぶ。":
                return None
            ids = {g["plan_text"]: n["id"] for g in job["input"]["candidates"] for n in g["nodes"]}
            froms = [ids[x] for x in support[job["input"]["target"]["plan_text"]]]
            return {"claims": [], "gaps": [], "need": [],
                    "steps": [{"from": froms, "to": job["input"]["target"]["id"], "warrant": "理由"}]}
        last, _ = self.drive(link=fail_c)
        self.assertEqual(last, "DONE decisions=3 premises=3 failed_jobs=1")
        out = self.verify().stdout.splitlines()
        ids = self.ids()
        self.assertIn("DECISION %s unverified" % ids["決定Cを選ぶ。"], out)
        # C を理由にした B も、C の失敗に左右されるので unverified。前提から根に届く A は rooted のまま
        self.assertIn("DECISION %s unverified" % ids["決定Bを選ぶ。"], out)
        self.assertIn("DECISION %s rooted local" % ids["決定Aを選ぶ。"], out)

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
        before = self.snapshot()
        self.verify()
        self.assertEqual(self.snapshot(), before)
        # 周の印の合わない段は、読むだけのときは消さずに「作られていない」とみなす
        mark = self.work / "jobs" / "decycle_2" / "created.json"
        info = json.loads(mark.read_text())
        info["round"] = "00000000"
        mark.write_text(json.dumps(info))
        before = self.snapshot()
        r = self.verify(ok=False)
        self.assertIn("next を DONE まで回して", r.stderr)
        self.assertEqual(self.snapshot(), before)

    def snapshot(self):
        out = {}
        for root, dirs, files in os.walk(str(self.dir / ".pace")):
            for name in dirs + files:
                path = Path(root) / name
                st = os.lstat(str(path))
                out[str(path)] = (st.st_mode, path.read_bytes() if path.is_file() else None)
        return out

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
        self.assertEqual(dict(pace.STAGE_VERSIONS)["review"], "2026-10-17.1")
        self.assertIn("input.excluded_note", pace.INSTRUCTIONS["link"])
        self.assertEqual(pace.STAGE_AGENT["decycle"], "pace-judge")
        # 反論の段は JOB_STAGES の外に置くので、並びと末尾は #6 のまま
        self.assertEqual(pace.JOB_STAGES[-1], "link_r2_6")
        self.assertEqual(pace.REVIEW_STAGE, "review")
        self.assertEqual(sum(1 for s in pace.JOB_STAGES if s.startswith("decycle_")), 5)

    def test_round_guard_of_the_memory_check(self):
        """walk の確かめの「いまの周の印」の条件（c["round"] == rid）。

        流れで記憶の round を書き換えると、exclusions が回1から外して link_r1 の load_job が先に止めるので、守りに届かない。
        回のメモを先に埋めてから、記憶の読み置きの round を書き換えて叩く。
        """
        self.init()
        self.drive()
        rnd = pace.Round(str(self.plan), readonly=True)
        rid = rnd.round_id()
        ids = rnd.created("decycle_1", rid)
        rnd.check_decycle_stage(1, ids, rid)
        a = rnd.node_by_id(self.ids()["決定Aを選ぶ。"])
        key = rnd.memory_key(a)
        rnd._memory[key] = [dict(c, round="00000000") for c in rnd._memory[key]]
        with self.assertRaisesRegex(pace.PaceError, "切った辺の記憶がありません"):
            rnd.check_decycle_stage(1, ids, rid)

    def test_memory_add_rewrites_round_and_pass(self):
        """from が同じ記憶は、round と pass と理由をいまのものに書き直す（足さない）。"""
        self.init()
        self.drive()
        rnd = pace.Round(str(self.plan))
        a = rnd.node_by_id(self.ids()["決定Aを選ぶ。"])
        b = rnd.node_by_id(self.ids()["決定Bを選ぶ。"])
        rnd.memory_add(a, b, "別の理由", "abcdef12", 3)
        cuts = pace.Round(str(self.plan), readonly=True).memory_get(a)
        self.assertEqual([(c["reason"], c["round"], c["pass"]) for c in cuts], [("別の理由", "abcdef12", 3)])


def chain_link(job):
    """A は B があれば B、無ければ C を理由にする。B と C は A を理由にする（回1で A⇄B、回2で A⇄C の輪）。"""
    target = job["input"]["target"]
    ids = {g["plan_text"]: n["id"] for g in job["input"]["candidates"] for n in g["nodes"]}
    want = {"決定Aを選ぶ。": ["決定Bを選ぶ。", "決定Cを選ぶ。"], "決定Bを選ぶ。": ["決定Aを選ぶ。"],
            "決定Cを選ぶ。": ["決定Aを選ぶ。"]}[target["plan_text"]]
    for text in want:
        if text in ids:
            return {"claims": [], "gaps": [], "need": [],
                    "steps": [{"from": [ids[text]], "to": target["id"], "warrant": "理由"}]}
    return {"claims": [], "gaps": [{"id": "g1", "ptype": "fact", "text": "t"}], "need": [],
            "steps": [{"from": ["g1"], "to": target["id"], "warrant": "理由"}]}


def cut_into_a(job):
    """A に入る辺を切る。"""
    ids = {n["id"]: n["plan_text"] for n in job["input"]["nodes"]}
    e = next(e for e in job["input"]["edges"] if ids[e["to"]] == "決定Aを選ぶ。")
    return {"cuts": [{"from": e["from"], "to": e["to"], "reason": "理由ではない"}]}


def ring_premise_link(job):
    """ring_link に、前提から根に届く step を足したもの（輪は残るが、どの決定も rooted で gap が無い）。"""
    answer = ring_link(job)
    target = job["input"]["target"]
    ids = {g["plan_text"]: n["id"] for g in job["input"]["candidates"] for n in g["nodes"]}
    steps = [s for s in answer["steps"] if not any(f.startswith("g") for f in s["from"])]
    steps.append({"from": [ids["前提の文。"]], "to": target["id"], "warrant": "前提から"})
    return {"claims": [], "gaps": [], "need": [], "steps": steps}


class RewrittenDecycleJobTest(FlowBase):
    """輪を断つジョブの status を done から failed に書き換えたときの振る舞い（設計文書10節307行の文の確かめ）。"""

    def fail_job(self, stage):
        d = self.work / "jobs" / stage
        ids = json.loads((d / "created.json").read_text())["jobs"]
        path = d / (ids[0] + ".json")
        job = json.loads(path.read_text(encoding="utf-8"))
        job["status"] = "failed"
        path.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")

    def test_no_later_cycle_only_adds_failed_jobs(self):
        """後の回の輪とも回 K の輪とも重ならなければ見つけず、failed_jobs だけが増える。"""
        self.init()
        self.drive()
        self.fail_job("decycle_1")
        self.assertEqual(parse(self.next().stdout)[1], "DONE decisions=3 premises=3 failed_jobs=1")
        self.assertTrue(self.verify().stdout.startswith("VERIFY decisions=3 rooted=1 needs_gap=2"))

    def test_later_cycle_with_the_decision_stops(self):
        """後の回の輪を断つジョブが既に作られていて、その決定を含むなら、作り直した入力と食い違って 1 で終わる。"""
        self.init()
        last, _ = self.drive(link=chain_link, decycle=cut_into_a)
        self.assertEqual(pace.Round(str(self.plan), readonly=True).final_pass(), 3)
        self.fail_job("decycle_1")
        for r in (self.next(ok=False), self.verify(ok=False), run("report", str(self.plan))):
            self.assertEqual(r.returncode, 1)
            self.assertIn("init を打ち直して", r.stderr)

    def test_rewrite_in_the_middle_shrinks_k_and_makes_u(self):
        """後の回の段を作る前に書き換えると、その輪は断たれず反復が早く終わり（K が縮む）、終わった回の輪の決定が U に入る。
        楽観は U を rooted に固定するので、U の外で U を理由にした決定も unverified になる。"""
        self.init()
        for _ in range(30):
            bundles, last = parse(self.next().stdout)
            stages = {json.loads(Path(p).read_text(encoding="utf-8"))["stage"] for _, _, ps in bundles for p in ps}
            if "link_r1_2" in stages:
                self.fail_job("decycle_1")
                break
            self.answer(bundles, link=chain_link, decycle=cut_into_a)
        self.answer(bundles, link=chain_link, decycle=cut_into_a)
        last, _ = self.drive(link=chain_link, decycle=cut_into_a)
        self.assertEqual(last, "DONE decisions=3 premises=3 failed_jobs=1")
        self.assertEqual(pace.Round(str(self.plan), readonly=True).final_pass(), 2)
        self.assertIn("unverified=3", self.verify().stdout.splitlines()[0])

    def test_cycle_left_at_k_with_rooted_decisions(self):
        """回 K に残った輪と重なれば U に入るが、前提から根に届く決定は rooted のまま。反論の段が作られたあとなら、
        反論のジョブのある決定が対象から外れて 1 で終わる（verify は反論の段を見ないので 0）。"""
        self.plan.write_text("# 題\n\n## 設計\n\n前提の文。\n" + "".join("決定%dを選ぶ。\n" % i for i in range(1, 8)),
                             encoding="utf-8")
        self.init()
        self.drive(link=ring_premise_link, decycle=cut_first, limit=40)
        self.assertEqual(pace.Round(str(self.plan), readonly=True).final_pass(), 6)
        self.fail_job("decycle_5")
        out = self.verify().stdout.splitlines()[0]
        self.assertIn("rooted=7", out)
        self.assertIn("unverified=0", out)
        self.assertIn("cycles=0", out)
        # 反論のジョブのある決定が対象から外れたので、ジョブを読む前に止まる
        for r in (self.next(ok=False), run("report", str(self.plan))):
            self.assertEqual(r.returncode, 1)
            self.assertIn("いまの反論の対象の決定のものではありません", r.stderr)
            self.assertIn("init を打ち直して", r.stderr)


class OtherPlanFlowTest(FlowBase):
    """別のプランで回す流れ（回6まで回す輪・分類の失敗・同じ文の決定）。"""

    def use(self, text):
        self.plan.write_text(text, encoding="utf-8")
        self.init()

    def test_ring_runs_to_pass_six_and_leaves_a_cycle(self):
        self.use("# 題\n\n## 設計\n\n" + "".join("決定%dを選ぶ。\n" % i for i in range(1, 8)))
        last, _ = self.drive(link=ring_link, decycle=cut_first, limit=40)
        self.assertEqual(last, "DONE decisions=7 premises=2 failed_jobs=0")
        for k in range(1, 6):
            self.assertEqual(len(json.loads((self.work / "jobs" / ("decycle_%d" % k) / "created.json").read_text())["jobs"]), 1)
        self.assertTrue((self.work / "jobs" / "link_r1_6" / "created.json").exists())
        self.assertFalse((self.work / "jobs" / "decycle_6").exists())
        out = self.verify().stdout.splitlines()
        self.assertIn("cycles=1", out[0])
        cycles = [x.split()[1:] for x in out if x.startswith("CYCLE ")]
        self.assertEqual(len(cycles), 1)
        self.assertEqual(len(cycles[0]), 2)
        rnd = pace.Round(str(self.plan), readonly=True)
        self.assertEqual(rnd.final_pass(), 6)
        # DONE のあとに打ち直しても同じ
        self.assertEqual(parse(self.next().stdout)[1], last)

    def test_failed_unit_makes_neighbors_unverified(self):
        self.use("# 題\n\n## 設計\n\n前提の文。\n決定Aを選ぶ。\n\n## 別\n\n別の前提の文。\n")
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
                        continue  # 答えを書かず、差し戻しを使い切らせる
                    self.answer([("b", "a", [p])])
        self.assertTrue(last.startswith("DONE decisions=1 "), last)
        out = self.verify().stdout.splitlines()
        a = self.ids()["決定Aを選ぶ。"]
        self.assertIn("DECISION %s unverified" % a, out)
        self.assertIn("GAP G_%s_g1 value uncertain" % a, out)
        self.assertIn("uncertain=1", out[0])
        units = [x for x in out if x.startswith("UNIT ")]
        self.assertEqual(len(units), 2)  # 「別」の見出しと文
        self.assertIn("unverified_units=2", out[0])

    def test_same_text_decisions_keep_both_cuts(self):
        """同じ文の決定は記憶のファイルを共有する。片方に書き足しても、もう片方の分を消さない。"""
        self.use("# 題\n\n## 設計1\n\n決定Aを選ぶ。\n決定Xを選ぶ。\n\n## 設計2\n\n決定Aを選ぶ。\n決定Yを選ぶ。\n")
        self.drive()
        rnd = pace.Round(str(self.plan))
        a1, a2 = [n for n in rnd.decisions() if n["text"] == "決定Aを選ぶ。"]
        x = next(n for n in rnd.decisions() if n["text"] == "決定Xを選ぶ。")
        y = next(n for n in rnd.decisions() if n["text"] == "決定Yを選ぶ。")
        self.assertEqual(rnd.memory_key(a1), rnd.memory_key(a2))
        rnd.memory_get(a1)
        rnd.memory_get(a2)
        rid = rnd.round_id()
        rnd.memory_add(a1, x, "r", rid, 1)
        rnd.memory_add(a2, y, "r", rid, 1)
        froms = sorted(c["from_text"] for c in pace.Round(str(self.plan), readonly=True).memory_get(a1))
        self.assertEqual(froms, ["決定Xを選ぶ。", "決定Yを選ぶ。"])


    def test_r2_trimmed_to_nothing_makes_the_decision_unverified(self):
        """足す候補はあったが40,000字で全部削った決定は、r1 の答えのまま U に入り unverified になる。"""
        far = "理由の文" + "あ" * 45000 + "。"
        self.use("# 題\n\n## 節\n\n決定Aを選ぶ。\n\n## 遠く\n\n" + "".join("埋め%d。\n\n" % i for i in range(9)) + far + "\n")

        def link(job):
            target = job["input"]["target"]
            return {"claims": [], "gaps": [{"id": "g1", "ptype": "fact", "text": "t"}], "need": ["理由の文"],
                    "steps": [{"from": ["g1"], "to": target["id"], "warrant": "理由"}]}
        last, waves = self.drive(link=link)
        self.assertTrue(last.startswith("DONE decisions=1 "), last)
        stages = {json.loads(Path(p).read_text(encoding="utf-8"))["stage"] for w in waves for _, _, ps in w for p in ps}
        self.assertNotIn("link_r2_1", stages)  # r2 は作られない
        out = self.verify().stdout.splitlines()
        a = self.ids()["決定Aを選ぶ。"]
        self.assertIn("DECISION %s unverified" % a, out)
        self.assertIn("GAP G_%s_g1 fact uncertain" % a, out)

    def test_same_text_decisions_cut_in_one_wave_reach_done(self):
        """同じ文の決定 A1・A2 がそれぞれ X・Y と輪になり、同じ波で両方を切っても、次の next は止まらない。"""
        self.use("# 題\n\n## 設計1\n\n決定Aを選ぶ。\n決定Xを選ぶ。\n\n## 設計2\n\n決定Aを選ぶ。\n決定Yを選ぶ。\n")
        partner = {"決定Aを選ぶ。": ("決定Xを選ぶ。", "決定Yを選ぶ。"), "決定Xを選ぶ。": ("決定Aを選ぶ。",),
                   "決定Yを選ぶ。": ("決定Aを選ぶ。",)}

        def link(job):
            target = job["input"]["target"]
            froms = [n["id"] for g in job["input"]["candidates"] for n in g["nodes"]
                     if g["section"] == target["section"] and g["plan_text"] in partner[target["plan_text"]]]
            if froms:
                return {"claims": [], "gaps": [], "need": [],
                        "steps": [{"from": froms, "to": target["id"], "warrant": "理由"}]}
            return {"claims": [], "gaps": [{"id": "g1", "ptype": "fact", "text": "t"}], "need": [],
                    "steps": [{"from": ["g1"], "to": target["id"], "warrant": "理由"}]}

        def decycle(job):
            ids = {n["id"]: n["plan_text"] for n in job["input"]["nodes"]}
            e = next(e for e in job["input"]["edges"] if ids[e["to"]] == "決定Aを選ぶ。")
            return {"cuts": [{"from": e["from"], "to": e["to"], "reason": "理由ではない"}]}
        last, waves = self.drive(link=link, decycle=decycle)
        self.assertTrue(last.startswith("DONE decisions=4 "), last)
        dec = [p for w in waves for _, _, ps in w for p in ps if Path(p).name.startswith("decycle_1_")]
        self.assertEqual(len(dec), 2)  # 2つの輪を同じ波で断った
        self.verify()
        self.assertEqual(parse(self.next().stdout)[1], last)

    def test_exclusions_take_every_decision_of_the_same_text(self):
        self.use("# 題\n\n## 設計1\n\n決定Aを選ぶ。\n決定Xを選ぶ。\n\n## 設計2\n\n決定Aを選ぶ。\n")
        self.drive()
        rnd = pace.Round(str(self.plan))
        a1, a2 = [n for n in rnd.decisions() if n["text"] == "決定Aを選ぶ。"]
        x = next(n for n in rnd.decisions() if n["text"] == "決定Xを選ぶ。")
        rnd.memory_add(x, a1, "r", "00000000", 1)  # 前の周に切った記憶
        self.assertEqual(pace.Round(str(self.plan), readonly=True).exclusions(1, x), frozenset({a1["id"], a2["id"]}))


if __name__ == "__main__":
    unittest.main()
