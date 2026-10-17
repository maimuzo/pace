"""pace.py next の根拠付け（候補の絞り込み・r1・2-gram の追加候補・r2）の unittest。決まりは docs/plans/pace_design.md の15節。

候補・2-gram・答えの検証は import して関数を直に確かめる。流れは test_next.py と同じく、テストが実行役（subagent）の
代わりに *.out.json を書き、next を subprocess で叩いて確かめる。
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

冒頭の前提。

## 目標

読み直しの漏れを無くす。

## 設計

読み直しは `Tick` の頭で1回だけ行う。
設定ファイルは `cfg` に読む。
監視は使わない。

## 別の節

ここも `cfg` を書き換えない。
無関係な文その1。
無関係な文その2。
無関係な文その3。
無関係な文その4。
無関係な文その5。
無関係な文その6。
無関係な文その7。
遠くの読み直しの理由を書く。
"""


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


def decision(text="決める"):
    return {"kind": "decision", "text": text, "alternatives": ["別の案"]}


def premise(text="前提", ptype="fact"):
    return {"kind": "premise", "ptype": ptype, "text": text}


def gap_answer(job, need=()):
    return {"claims": [], "gaps": [{"id": "g1", "ptype": "fact", "text": "足りない前提"}],
            "steps": [{"from": ["g1"], "to": job["input"]["target"]["id"], "warrant": "理由"}], "need": list(need)}


def units_of(text):
    return pace.split_units(text)


def nodes_for(units, labels_for):
    """単位ごとのラベルからノードを作る（pace.Round.nodes と同じ ID の付け方）。"""
    out = []
    for u in units:
        seen = {"decision": 0, "premise": 0}
        for lab in labels_for(u):
            seen[lab["kind"]] += 1
            node = {"id": "%s_%s_%d" % ("D" if lab["kind"] == "decision" else "P", u["id"][1:], seen[lab["kind"]]),
                    "kind": lab["kind"]}
            if lab["kind"] == "premise":
                node["ptype"] = lab["ptype"]
            node["text"] = lab["text"]
            if lab["kind"] == "decision":
                node["alternatives"] = lab["alternatives"]
            node.update({"unit": u["id"], "line": u["line"], "sec": u["sec"], "path": u["path"]})
            out.append(node)
    return out


def candidate_texts(job_input):
    return [g["plan_text"] for g in job_input["candidates"]]


class CandidateTest(unittest.TestCase):
    """候補の絞り込み（import して確かめる）。"""

    def setUp(self):
        self.units = units_of(PLAN)
        self.by_text = {u["text"]: u for u in self.units}

        def labels(u):
            if u["text"] == "読み直しは `Tick` の頭で1回だけ行う。":
                return [decision("読み直しは Tick の頭"), premise("同じ単位の前提")]
            return [premise(u["text"])]

        self.nodes = nodes_for(self.units, labels)
        self.target = next(n for n in self.nodes if n["kind"] == "decision")

    def test_four_rules(self):
        inp = pace.select_candidates(self.units, self.nodes, self.target)
        texts = candidate_texts(inp)
        self.assertIn("監視は使わない。", texts)  # 同じ H2 の塊
        self.assertIn("ここも `cfg` を書き換えない。", texts)  # 前後5単位
        self.assertIn("冒頭の前提。", texts)  # 冒頭の節（最初の H2 より前）
        self.assertIn("読み直しの漏れを無くす。", texts)  # 冒頭の節（最初の H2）
        self.assertNotIn("無関係な文その7。", texts)  # どれにも当たらない
        self.assertNotIn("遠くの読み直しの理由を書く。", texts)

    def test_identifier_shared(self):
        target = next(n for n in self.nodes if n["unit"] == self.by_text["遠くの読み直しの理由を書く。"]["id"])
        target = dict(target, kind="decision", alternatives=["別"])
        units = [dict(u, text=u["text"].replace("遠くの読み直しの理由を書く。", "遠くでも `cfg` を使う。"))
                 if u["id"] == target["unit"] else u for u in self.units]
        inp = pace.select_candidates(units, self.nodes, target)
        texts = candidate_texts(inp)
        self.assertIn("設定ファイルは `cfg` に読む。", texts)  # 識別子の共有（離れた別の H2）
        self.assertNotIn("読み直しは `Tick` の頭で1回だけ行う。", texts)

    def test_target_excluded_same_unit_kept_and_ordered(self):
        inp = pace.select_candidates(self.units, self.nodes, self.target)
        ids = [n["id"] for g in inp["candidates"] for n in g["nodes"]]
        self.assertNotIn(self.target["id"], ids)
        first = inp["candidates"][0]
        self.assertEqual(first["plan_text"], "読み直しは `Tick` の頭で1回だけ行う。")
        self.assertEqual(first["nodes"], [{"id": self.target["id"].replace("D_", "P_"), "kind": "fact",
                                           "text": "同じ単位の前提"}])
        # 近い順（同じ距離なら前の単位）
        self.assertEqual(candidate_texts(inp)[1:3], ["設計", "設定ファイルは `cfg` に読む。"])
        self.assertEqual(inp["target"], {"id": self.target["id"], "text": "読み直しは Tick の頭",
                                         "alternatives": ["別の案"], "section": "設計",
                                         "plan_text": "読み直しは `Tick` の頭で1回だけ行う。"})

    def test_same_title_h2_are_separate_blocks_and_h3_does_not_split(self):
        units = units_of("# 題\n\n## A\n\n一。\n\n### A\n\n二。\n\n## B\n\n三。\n\n## A\n\n四。\n")
        blocks = dict(zip([u["text"] for u in units], pace.h2_blocks(units)))
        self.assertEqual(blocks["一。"], blocks["二。"])  # 同じ題名の H3 では区切らない
        self.assertNotEqual(blocks["一。"], blocks["四。"])  # 同じ題名の H2 は別の塊

    def test_eighty_nodes_without_cutting_a_unit(self):
        body = "".join("文%d。\n" % i for i in range(60))
        units = units_of("# 題\n\n## 節\n\n" + body)
        nodes = nodes_for(units, lambda u: [premise(u["text"] + "a"), premise(u["text"] + "b")]
                          if u["kind"] == "sentence" else [])
        target = dict(nodes[0], kind="decision", alternatives=["別"])
        nodes[0] = target
        inp = pace.select_candidates(units, nodes, target)
        counts = [len(g["nodes"]) for g in inp["candidates"]]
        self.assertLessEqual(sum(counts), pace.MAX_CANDIDATES)
        self.assertEqual(sum(counts), 79)  # 同じ単位の1件 + 2件ずつ39単位。80を超える単位の手前で止まる
        self.assertTrue(all(c == 2 for c in counts[1:]))

    def test_cut_by_chars_from_far_units(self):
        body = "".join("文%d%s。\n" % (i, "長" * 3000) for i in range(30))
        units = units_of("# 題\n\n## 節\n\n" + body)
        nodes = nodes_for(units, lambda u: [premise("前提")] if u["kind"] == "sentence" else [])
        target = dict(nodes[0], kind="decision", alternatives=["別"])
        nodes[0] = target
        inp = pace.select_candidates(units, nodes, target)
        self.assertLessEqual(pace.link_size(inp), pace.LINK_JOB_MAX_CHARS)
        kept = candidate_texts(inp)
        self.assertGreater(len(kept), 0)
        # 遠い単位から削るので、残るのは近い単位の続き
        self.assertEqual(kept, [u["text"] for u in units if u["kind"] == "sentence"][1:len(kept) + 1])

    def test_size_ignores_paths_and_errors(self):
        inp = pace.select_candidates(self.units, self.nodes, self.target)
        self.assertEqual(pace.link_size(inp), len(pace.dumps({"instructions": pace.INSTRUCTIONS["link"], "input": inp})))

    def test_failed_unit_nodes_are_not_candidates(self):
        gone = self.by_text["監視は使わない。"]["id"]
        nodes = [n for n in self.nodes if n["unit"] != gone]
        self.assertNotIn("監視は使わない。", candidate_texts(pace.select_candidates(self.units, nodes, self.target)))

    def test_empty_backticks_are_not_identifiers(self):
        self.assertEqual(pace.identifiers("a ` ` b `x` c"), {"x"})


class ExpandTest(unittest.TestCase):
    """2-gram の追加候補（import して確かめる）。"""

    def setUp(self):
        self.units = units_of(PLAN)
        self.nodes = nodes_for(self.units, lambda u: [decision(u["text"])] if u["text"].startswith("読み直しは")
                               else [premise(u["text"])])
        self.target = next(n for n in self.nodes if n["kind"] == "decision")
        self.r1 = pace.select_candidates(self.units, self.nodes, self.target)

    def answer(self, need):
        return {"claims": [], "gaps": [], "steps": [], "need": need}

    def test_adds_close_node_with_added_and_previous(self):
        inp = pace.expand_candidates(self.units, self.nodes, self.target, self.r1, self.answer(["遠くの読み直しの理由"]))
        self.assertIsNotNone(inp)
        far = next(n["id"] for n in self.nodes if n["text"] == "遠くの読み直しの理由を書く。")
        self.assertEqual(inp["added"][0], far)
        self.assertEqual(inp["previous"], self.answer(["遠くの読み直しの理由"]))
        self.assertEqual(inp["candidates"][:len(self.r1["candidates"])], self.r1["candidates"])
        have = {n["id"] for g in self.r1["candidates"] for n in g["nodes"]} | {self.target["id"]}
        self.assertFalse(set(inp["added"]) & have)  # r1 の候補と決定そのものは足さない

    def test_no_match_gives_none(self):
        self.assertIsNone(pace.expand_candidates(self.units, self.nodes, self.target, self.r1, self.answer(["zzqq"])))

    def test_limits_per_need_and_total(self):
        body = "".join("理由の文%d。\n" % i for i in range(40))
        units = units_of("# 題\n\n## 一\n\n決める。\n\n" + "".join("## 節%d\n\n埋め%d。\n\n" % (i, i) for i in range(10))
                         + "## 遠く\n\n" + body)
        nodes = nodes_for(units, lambda u: [decision(u["text"])] if u["text"] == "決める。" else [premise(u["text"])])
        target = next(n for n in nodes if n["kind"] == "decision")
        r1 = pace.select_candidates(units, nodes, target)
        one = pace.expand_candidates(units, nodes, target, r1, self.answer(["理由の文"]))
        self.assertEqual(len(one["added"]), pace.EXPAND_PER_NEED)
        many = pace.expand_candidates(units, nodes, target, r1, self.answer(["理由の文"] * 3))
        self.assertEqual(len(many["added"]), pace.EXPAND_MAX)
        self.assertEqual(len(set(many["added"])), pace.EXPAND_MAX)
        # 同点（同じ文）は決定に近い単位から
        texts = [next(n["text"] for n in nodes if n["id"] == i) for i in one["added"]]
        self.assertEqual(texts[0], "理由の文0。")


class CheckLinkTest(unittest.TestCase):
    """根拠付けの答えの検証（import して確かめる）。"""

    INPUT = {"target": {"id": "D_x_1"}, "candidates": [{"nodes": [{"id": "P_a_1"}, {"id": "D_b_1"}]}]}

    def ok(self, **over):
        ans = {"claims": [{"id": "c1", "text": "主張"}], "gaps": [{"id": "g1", "ptype": "value", "text": "穴"}],
               "steps": [{"from": ["P_a_1"], "to": "c1", "warrant": "w"},
                         {"from": ["c1", "g1"], "to": "D_x_1", "warrant": "w"}],
               "need": []}
        ans.update(over)
        return ans

    def bad(self, ans, word):
        with self.assertRaises(ValueError) as cm:
            pace.check_link(ans, self.INPUT)
        self.assertIn(word, str(cm.exception))

    def test_valid_passes_and_drops_unknown_keys(self):
        ans = self.ok(extra=1)
        ans["claims"][0]["extra"] = 2
        out = pace.check_link(ans, self.INPUT)
        self.assertEqual(set(out), {"claims", "gaps", "steps", "need"})
        self.assertEqual(out["claims"], [{"id": "c1", "text": "主張"}])

    def test_rules(self):
        self.bad(self.ok(steps=[{"from": [], "to": "D_x_1", "warrant": "w"}]), "from")
        self.bad(self.ok(steps=[{"from": ["P_zz_1"], "to": "D_x_1", "warrant": "w"},
                                {"from": ["g1"], "to": "D_x_1", "warrant": "w"}]), "どれでもない")
        self.bad(self.ok(steps=[{"from": ["P_a_1", "g1"], "to": "D_b_1", "warrant": "w"}]), "to が")
        self.bad(self.ok(steps=[{"from": ["P_a_1", "g1"], "to": "c1", "warrant": "w"}]), "step がありません")
        self.bad(self.ok(steps=[{"from": ["c1", "g1"], "to": "c1", "warrant": "w"},
                                {"from": ["P_a_1"], "to": "D_x_1", "warrant": "w"}]), "自己参照")
        self.bad(self.ok(steps=[{"from": ["P_a_1"], "to": "D_x_1", "warrant": "w"}]), "gap")
        self.bad(self.ok(steps=[{"from": ["P_a_1", "g1"], "to": "D_x_1", "warrant": " "}]), "warrant")
        self.bad(self.ok(claims=[{"id": "x1", "text": "主張"}]), "c<数>")
        self.bad(self.ok(gaps=[{"id": "g1", "ptype": "zzz", "text": "穴"}]), "ptype")
        self.bad(self.ok(claims=[{"id": "c1", "text": "a"}, {"id": "c1", "text": "b"}]), "2回")
        self.bad(self.ok(need=["\ud83d"]), "need")
        self.bad(self.ok(need="文字列"), "need")
        self.bad(self.ok(claims=[{"id": "c1", "text": "\ud83d"}]), "UTF-8")

    def test_empty_need_passes(self):
        self.assertEqual(pace.check_link(self.ok(need=[]), self.INPUT)["need"], [])


class LinkFlowTest(unittest.TestCase):
    """next の流れ（subprocess）。"""

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

    @staticmethod
    def labels(stage, variant, uid, text):
        if text.startswith("読み直しは") or text.startswith("監視は"):
            return [decision(text)]
        return [premise(text)]

    def answer(self, bundles, link_for=None):
        """束の全ジョブに答えを書く。link_for(job) が None を返したジョブには書かない。"""
        texts = {u["id"]: u["text"] for u in pace.split_units(self.plan.read_text(encoding="utf-8"))}
        link_for = link_for or (lambda job: gap_answer(job))
        for _, _, paths in bundles:
            for p in paths:
                job = json.loads(Path(p).read_text(encoding="utf-8"))
                if job["stage"] == "classify":
                    ans = {"units": [{"uid": u["uid"], "labels": self.labels("classify", job["variant"], u["uid"],
                                                                             texts[u["uid"]])}
                                     for u in job["input"]["units"]]}
                elif job["stage"] == "adjudicate":
                    ans = {"labels": []}
                else:
                    ans = link_for(job)
                    if ans is None:
                        continue
                Path(job["out_path"]).write_text(json.dumps(ans, ensure_ascii=False), encoding="utf-8")

    def drive(self, link_for=None, limit=30):
        stages = []
        for _ in range(limit):
            bundles, last = parse(self.next().stdout)
            if last.startswith("DONE"):
                return last, stages
            stages.append(sorted({json.loads(Path(p).read_text(encoding="utf-8"))["stage"]
                                  for _, _, ps in bundles for p in ps}))
            self.answer(bundles, link_for)
        self.fail("DONE に届かない")

    def round(self):
        return pace.Round(str(self.plan))

    def decision_ids(self):
        return [n["id"] for n in self.round().decisions()]

    def test_r1_then_r2_and_answers_in_cache(self):
        self.init()

        def link_for(job):
            if job["stage"] == "link_r1" and "Tick" in job["input"]["target"]["plan_text"]:
                return gap_answer(job, ["遠くの読み直しの理由"])
            return gap_answer(job)

        last, stages = self.drive(link_for)
        self.assertEqual(last, "DONE decisions=2 premises=16 failed_jobs=0")
        self.assertIn(["link_r1"], stages)
        self.assertIn(["link_r2"], stages)
        r2 = sorted((self.work / "jobs" / "link_r2").glob("link_r2_*_????????.json"))
        self.assertEqual(len(r2), 1)
        job = json.loads(r2[0].read_text(encoding="utf-8"))
        self.assertTrue(job["input"]["added"])
        self.assertEqual(job["input"]["previous"]["need"], ["遠くの読み直しの理由"])
        self.assertEqual(job["stage"], "link_r2")
        self.assertTrue(Path(job["out_path"]).is_absolute())
        rnd = self.round()
        for d in self.decision_ids():
            self.assertIsNotNone(rnd.link_answer(d))
        self.assertFalse((self.work / "links.jsonl").exists())

    def test_agent_and_bundle(self):
        self.init()
        for _ in range(5):
            bundles, last = parse(self.next().stdout)
            stage = {json.loads(Path(p).read_text(encoding="utf-8"))["stage"] for _, _, ps in bundles for p in ps}
            if stage == {"link_r1"}:
                self.assertEqual([a for _, a, _ in bundles], ["pace:pace-worker"])
                job = json.loads(Path(bundles[0][2][0]).read_text(encoding="utf-8"))
                self.assertEqual(job["prompt_version"], dict(pace.STAGE_VERSIONS)["link"])
                self.assertEqual(job["instructions"], pace.INSTRUCTIONS["link"])
                return
            self.answer(bundles)
        self.fail("根拠付けの波が出ない")

    def fail_link(self, which):
        """which の段の、Tick の決定のジョブだけ答えずに4回打って失敗させ、DONE まで回す。"""
        def link_for(job):
            tick = "Tick" in job["input"]["target"]["plan_text"]
            if tick and job["stage"] == which:
                return None
            if tick and job["stage"] == "link_r1":
                return gap_answer(job, ["遠くの読み直しの理由"])
            return gap_answer(job)
        return self.drive(link_for)

    def test_r1_failure(self):
        self.init()
        last, _ = self.fail_link("link_r1")
        self.assertEqual(last, "DONE decisions=2 premises=16 failed_jobs=1")
        rec = [json.loads(x) for x in (self.work / "failed_jobs.jsonl").read_text().splitlines()]
        self.assertEqual(len(rec), 1)
        self.assertEqual(rec[0]["units"], [])
        self.assertEqual(rec[0]["stage"], "link_r1")
        tick = rec[0]["decision"]
        rnd = self.round()
        self.assertIsNone(rnd.link_answer(tick))
        others = [d for d in self.decision_ids() if d != tick]
        self.assertTrue(all(rnd.link_answer(d) is not None for d in others))
        # 根拠付けの失敗ではノードを消さない
        self.assertIn(tick, [n["id"] for n in rnd.node_list()])

    def test_r2_only_failure_is_not_replaced_by_r1(self):
        self.init()
        last, _ = self.fail_link("link_r2")
        self.assertEqual(last, "DONE decisions=2 premises=16 failed_jobs=1")
        rec = json.loads((self.work / "failed_jobs.jsonl").read_text().splitlines()[0])
        self.assertEqual(rec["stage"], "link_r2")
        self.assertIsNone(self.round().link_answer(rec["decision"]))

    def test_missing_link_answer_stops(self):
        self.init()
        self.drive()
        victims = [p for p in (self.work / "cache").glob("*.json")
                   if "answer" in json.loads(p.read_text(encoding="utf-8"))]
        self.assertEqual(len(victims), 2)
        victims[0].unlink()
        r = self.next(ok=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("init を打ち直して", r.stderr)
        self.assertFalse((self.work / "nodes.jsonl").exists())

    def test_reinit_reuses_cache(self):
        self.init()
        first, _ = self.drive()
        self.init()
        self.assertEqual(self.next().stdout.strip(), first)

    def test_editing_a_unit_relinks_only_dependent_decisions(self):
        self.init()
        self.drive()
        # 「無関係な文その7。」は「監視は」の決定の候補（同じ H2 でなく前後5単位でもない）に入らない
        text = self.plan.read_text(encoding="utf-8").replace("無関係な文その7。", "無関係な文その七。")
        self.plan.write_text(text, encoding="utf-8")
        self.init()
        _, stages = self.drive()
        jobs = sorted((self.work / "jobs" / "link_r1").glob("link_r1_*_????????.json"))
        self.assertEqual(jobs, [])  # どの決定の候補にも入っていないので、根拠付けし直さない
        text = text.replace("監視は使わない。", "監視は決して使わない。")
        self.plan.write_text(text, encoding="utf-8")
        self.init()
        self.drive()
        jobs = sorted((self.work / "jobs" / "link_r1").glob("link_r1_*_????????.json"))
        # 直した単位そのものの決定と、それを候補に含む Tick の決定の2件
        self.assertEqual(len(jobs), 2)

    def test_tampered_link_input_stops(self):
        self.init()
        for _ in range(5):
            bundles, _ = parse(self.next().stdout)
            paths = [p for _, _, ps in bundles for p in ps]
            job = json.loads(Path(paths[0]).read_text(encoding="utf-8"))
            if job["stage"] == "link_r1":
                break
            self.answer(bundles)
        job["input"]["candidates"].append({"section": "", "plan_text": "偽", "nodes": [{"id": "P_ffffffff_1",
                                                                                    "kind": "fact", "text": "偽"}]})
        Path(paths[0]).write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
        r = self.next(ok=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("init を打ち直して", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_bad_answer_is_sent_back(self):
        self.init()
        for _ in range(5):
            bundles, _ = parse(self.next().stdout)
            paths = [p for _, _, ps in bundles for p in ps]
            job = json.loads(Path(paths[0]).read_text(encoding="utf-8"))
            if job["stage"] == "link_r1":
                break
            self.answer(bundles)
        bad = gap_answer(job)
        bad["steps"][0]["from"] = []
        Path(job["out_path"]).write_text(json.dumps(bad), encoding="utf-8")
        self.next()
        job = json.loads(Path(paths[0]).read_text(encoding="utf-8"))
        self.assertEqual(job["attempts"], 1)
        self.assertIn("from", job["previous_errors"][0])


if __name__ == "__main__":
    unittest.main()
