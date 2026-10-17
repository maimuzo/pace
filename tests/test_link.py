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

    def test_opening_and_same_h2_rules_alone(self):
        """前後5単位から外れた単位だけで、冒頭の節と同じ H2 の塊の規則を確かめる。"""
        filler = "".join("## 埋め%d\n\n埋め%d。\n\n" % (i, i) for i in range(6))
        body = "".join("同じ節の文%d。\n" % i for i in range(10))
        units = units_of("# 題\n\n冒頭の前提。\n\n## 目標\n\n目標の文。\n\n" + filler + "## 本題\n\n" + body)
        nodes = nodes_for(units, lambda u: [premise(u["text"])])
        target = next(n for n in nodes if n["text"] == "同じ節の文9。")
        target = dict(target, kind="decision", alternatives=["別"])
        nodes = [target if n["id"] == target["id"] else n for n in nodes]
        texts = candidate_texts(pace.select_candidates(units, nodes, target))
        pos = {u["text"]: i for i, u in enumerate(units)}
        t = pos["同じ節の文9。"]
        for far in ("冒頭の前提。", "目標の文。", "同じ節の文0。", "本題"):
            self.assertGreater(t - pos[far], pace.WINDOW, far)  # 前後5単位では入らない位置
            self.assertIn(far, texts)
        self.assertNotIn("埋め1。", texts)  # どの規則にも当たらない

    def test_one_decision_over_limit_has_no_candidates(self):
        units = units_of("# 題\n\n## 節\n\n" + "長" * (pace.LINK_JOB_MAX_CHARS + 10) + "。\n\n短い前提。\n")
        nodes = nodes_for(units, lambda u: [premise(u["text"])] if u["kind"] == "sentence" else [])
        target = dict(nodes[0], kind="decision", alternatives=["別"])
        nodes[0] = target
        inp = pace.select_candidates(units, nodes, target)
        self.assertEqual(inp["candidates"], [])
        self.assertGreater(pace.link_size(inp), pace.LINK_JOB_MAX_CHARS)

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
        """削る判定は指示文と入力だけで測る。out_path と差し戻しの理由を足したジョブ全体が上限を超えても、候補は削らない。"""
        body = "".join("文%d%s。\n" % (i, "長" * 3000) for i in range(30))
        units = units_of("# 題\n\n## 節\n\n" + body)
        nodes = nodes_for(units, lambda u: [premise("前提")] if u["kind"] == "sentence" else [])
        target = dict(nodes[0], kind="decision", alternatives=["別"])
        nodes[0] = target
        inp = pace.select_candidates(units, nodes, target)
        size = pace.link_size(inp)
        # 字数は指示文と入力だけを pace.dumps（indent=1）で測った長さ
        self.assertEqual(size, len(pace.dumps({"instructions": pace.INSTRUCTIONS["link"], "input": inp})))
        self.assertLessEqual(size, pace.LINK_JOB_MAX_CHARS)
        # 削ったあとの余白より大きい out_path と理由を足すと、ジョブ全体は上限を超える
        room = pace.LINK_JOB_MAX_CHARS - size
        job = {"instructions": pace.INSTRUCTIONS["link"], "input": inp, "out_path": "/" + "x" * (room + 10),
               "previous_errors": ["理由" * 150] * 3, "attempts": 3}
        self.assertGreater(len(pace.dumps(job)), pace.LINK_JOB_MAX_CHARS)
        # ジョブ全体で測って削っていたら、もっと少ない候補になっていた（実際は削らずに残っている）
        whole = dict(inp, candidates=list(inp["candidates"]))
        while whole["candidates"] and len(pace.dumps(dict(job, input=whole))) > pace.LINK_JOB_MAX_CHARS:
            whole["candidates"].pop()
        self.assertLess(len(whole["candidates"]), len(inp["candidates"]))

    def test_link_bundle_limits(self):
        self.assertEqual(pace.bundle_limits("link_r1_1"), (pace.LINK_BUNDLE_MAX_CHARS, pace.BUNDLE_MAX_JOBS))
        self.assertEqual(pace.bundle_limits("link_r2_1"), (pace.LINK_BUNDLE_MAX_CHARS, pace.BUNDLE_MAX_JOBS))
        self.assertEqual(pace.bundle_limits("classify"), (pace.BUNDLE_MAX_CHARS, pace.BUNDLE_MAX_JOBS))
        jobs = [{"job_id": "j%d" % i, "pad": "x" * 35000} for i in range(7)]
        out = pace.Round.bundles(jobs, *pace.bundle_limits("link_r1_1"))
        self.assertEqual([len(b) for b in out], [3, 3, 1])  # 120,000字に35,000字台が3件
        self.assertTrue(all(sum(len(pace.dumps(j)) for j in b) <= pace.LINK_BUNDLE_MAX_CHARS for b in out))

    def test_failed_unit_nodes_are_not_candidates(self):
        gone = self.by_text["監視は使わない。"]["id"]
        nodes = [n for n in self.nodes if n["unit"] != gone]
        self.assertNotIn("監視は使わない。", candidate_texts(pace.select_candidates(self.units, nodes, self.target)))
        # Round の経路（分類が失敗した単位を nodes() が外す）は LinkFlowTest.test_failed_classify_units_are_not_candidates

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


class ExpandTrimTest(unittest.TestCase):
    """r2 の40,000字での削り（import して確かめる）。"""

    def build(self, far_len):
        near = "".join("近%d。\n" % i for i in range(3))
        far = "".join("理由の文%d%s。\n" % (i, "長" * far_len) for i in range(12))
        units = units_of("# 題\n\n## 一\n\n決める。\n" + near + "\n" + "".join(
            "## 節%d\n\n埋め%d。\n\n" % (i, i) for i in range(8)) + "## 遠く\n\n" + far)
        nodes = nodes_for(units, lambda u: [decision(u["text"])] if u["text"] == "決める。" else [premise(u["text"])])
        target = next(n for n in nodes if n["kind"] == "decision")
        r1 = pace.select_candidates(units, nodes, target)
        return units, nodes, target, r1

    def test_trims_lowest_similarity_first(self):
        units, nodes, target, r1 = self.build(3000)
        need = {"claims": [], "gaps": [], "steps": [], "need": ["理由の文1", "理由の文"]}
        full = pace.expand_candidates(units, nodes, target, r1, need)
        self.assertIsNotNone(full)
        self.assertLessEqual(pace.link_size(full), pace.LINK_JOB_MAX_CHARS)
        self.assertLess(len(full["added"]), pace.EXPAND_MAX)  # 字数で削った
        texts = [next(n["text"] for n in nodes if n["id"] == i) for i in full["added"]]
        self.assertTrue(texts[0].startswith("理由の文1"))  # いちばん近いものは残る

    def test_all_trimmed_gives_none(self):
        units, nodes, target, r1 = self.build(pace.LINK_JOB_MAX_CHARS)
        need = {"claims": [], "gaps": [], "steps": [], "need": ["理由の文"]}
        self.assertIsNone(pace.expand_candidates(units, nodes, target, r1, need))


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
        self.bad(self.ok(steps=[{"from": ["P_a_1", "g1"], "to": ["D_x_1"], "warrant": "w"}]), "to が")
        self.bad(self.ok(steps=[{"from": ["P_a_1", "g1"], "to": {"id": "D_x_1"}, "warrant": "w"}]), "to が")
        self.bad(self.ok(steps=[{"from": ["P_a_1", "g1"], "to": "c1", "warrant": "w"}]), "step がありません")
        self.bad(self.ok(steps=[{"from": ["c1", "g1"], "to": "c1", "warrant": "w"},
                                {"from": ["P_a_1"], "to": "D_x_1", "warrant": "w"}]), "自己参照")
        self.bad(self.ok(steps=[{"from": ["P_a_1"], "to": "D_x_1", "warrant": "w"}]), "gap")
        self.bad(self.ok(steps=[{"from": ["P_a_1", "g1"], "to": "D_x_1", "warrant": " "}]), "warrant")
        self.bad(self.ok(claims=[{"id": "x1", "text": "主張"}]), "c<数>")
        self.bad(self.ok(gaps=[{"id": "c9", "ptype": "value", "text": "穴"}]), "g<数>")
        self.bad(self.ok(gaps=[{"id": "g1", "ptype": "value", "text": "穴"}, {"id": "g1", "ptype": "fact", "text": "穴2"}]),
                 "2回")
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
            if job["stage"] == "link_r1_1" and "Tick" in job["input"]["target"]["plan_text"]:
                return gap_answer(job, ["遠くの読み直しの理由"])
            return gap_answer(job)

        last, stages = self.drive(link_for)
        self.assertEqual(last, "DONE decisions=2 premises=16 failed_jobs=0")
        self.assertIn(["link_r1_1"], stages)
        self.assertIn(["link_r2_1"], stages)
        r2 = sorted((self.work / "jobs" / "link_r2_1").glob("link_r2_1_*_????????.json"))
        self.assertEqual(len(r2), 1)
        job = json.loads(r2[0].read_text(encoding="utf-8"))
        self.assertTrue(job["input"]["added"])
        self.assertEqual(job["input"]["previous"]["need"], ["遠くの読み直しの理由"])
        self.assertEqual(job["stage"], "link_r2_1")
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
            if stage == {"link_r1_1"}:
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
            if tick and job["stage"] == "link_r1_1":
                return gap_answer(job, ["遠くの読み直しの理由"])
            return gap_answer(job)
        return self.drive(link_for)

    def test_r1_failure(self):
        self.init()
        last, _ = self.fail_link("link_r1_1")
        self.assertEqual(last, "DONE decisions=2 premises=16 failed_jobs=1")
        rec = [json.loads(x) for x in (self.work / "failed_jobs.jsonl").read_text().splitlines()]
        self.assertEqual(len(rec), 1)
        self.assertEqual(rec[0]["units"], [])
        self.assertEqual(rec[0]["stage"], "link_r1_1")
        tick = rec[0]["decision"]
        rnd = self.round()
        self.assertIsNone(rnd.link_answer(tick))
        others = [d for d in self.decision_ids() if d != tick]
        self.assertTrue(all(rnd.link_answer(d) is not None for d in others))
        # 根拠付けの失敗ではノードを消さないので、ほかの決定の候補に失敗した決定が残る
        self.assertIn(tick, [n["id"] for n in rnd.node_list()])
        for d in others:
            ids = [n["id"] for g in rnd.link_input("link_r1_1", d)["candidates"] for n in g["nodes"]]
            self.assertIn(tick, ids)

    def test_r2_only_failure_is_not_replaced_by_r1(self):
        self.init()
        last, _ = self.fail_link("link_r2_1")
        self.assertEqual(last, "DONE decisions=2 premises=16 failed_jobs=1")
        rec = json.loads((self.work / "failed_jobs.jsonl").read_text().splitlines()[0])
        self.assertEqual(rec["stage"], "link_r2_1")
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

    def test_missing_r2_answer_stops(self):
        self.init()

        def link_for(job):
            if job["stage"] == "link_r1_1" and "Tick" in job["input"]["target"]["plan_text"]:
                return gap_answer(job, ["遠くの読み直しの理由"])
            return gap_answer(job)

        self.drive(link_for)
        r2 = json.loads(next((self.work / "jobs" / "link_r2_1").glob("link_r2_1_*_????????.json")).read_text(encoding="utf-8"))
        rnd = self.round()
        key = rnd.link_key(rnd.link_input("link_r2_1", r2["input"]["target"]["id"]))
        (self.work / "cache" / (key + ".json")).unlink()
        r = self.next(ok=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("init を打ち直して", r.stderr)

    def test_job_names_and_paths_do_not_change_inputs(self):
        """ジョブの名前の形と、作業ディレクトリのパスの長さで入力が変わらないこと。"""
        inputs = []
        for sub in ("a", "b" * 120):
            d = self.dir / sub
            d.mkdir()
            plan = d / "plan.md"
            plan.write_text(PLAN, encoding="utf-8")
            self.plan = plan
            self.work = d / ".pace" / "plan"
            self.init()
            for _ in range(5):
                bundles, _ = parse(self.next().stdout)
                paths = [q for _, _, ps in bundles for q in ps]
                job = json.loads(Path(paths[0]).read_text(encoding="utf-8"))
                if job["stage"] == "link_r1_1":
                    break
                self.answer(bundles)
            for q in paths:
                self.assertRegex(Path(q).name, r"^link_r1_1_D_[0-9a-f]{8}_1_[0-9a-f]{8}\.json$")
            inputs.append(sorted(json.dumps(json.loads(Path(q).read_text(encoding="utf-8"))["input"],
                                            ensure_ascii=False, sort_keys=True) for q in paths))
        self.assertEqual(inputs[0], inputs[1])

    def test_failed_classify_units_are_not_candidates(self):
        """分類の A のジョブが失敗した節の単位は、ノードが無いので根拠付けの候補に入らない（Round の経路）。"""
        self.init()
        goal = "読み直しの漏れを無くす。"
        skipped = set()
        for _ in range(12):
            bundles, last = parse(self.next().stdout)
            if last.startswith("DONE"):
                break
            keep = []
            for b in bundles:
                paths = []
                for q in b[2]:
                    job = json.loads(Path(q).read_text(encoding="utf-8"))
                    if job["stage"] == "classify" and job["variant"] == "A" and any(
                            u["text"] == goal for u in job["input"]["units"]):
                        skipped.add(q)
                        continue
                    paths.append(q)
                keep.append((b[0], b[1], paths))
            self.answer(keep)
        self.assertTrue(last.startswith("DONE"), last)
        self.assertIn("failed_jobs=1", last)
        self.assertTrue(skipped)
        rnd = self.round()
        for d in self.decision_ids():
            self.assertNotIn(goal, [g["plan_text"] for g in rnd.link_input("link_r1_1", d)["candidates"]])

    def test_reinit_reuses_cache(self):
        self.init()
        first, _ = self.drive()
        self.init()
        self.assertEqual(self.next().stdout.strip(), first)

    def test_broken_link_cache_is_rebuilt(self):
        """根拠付けのキャッシュは読むたびにいまの入力で検証し直す（cache_get が check_link を通す）。壊れた答えは
        無いものとして、その決定だけジョブを作り直し、答えると検証を通る答えで書き戻す。"""
        self.init()
        self.drive()
        rnd = pace.Round(str(self.plan), readonly=True)
        ids = [d["id"] for d in rnd.decisions()]
        self.assertEqual(len(ids), 2)
        # 決定1は答えが JSON のオブジェクトでない。決定2は step の to が target でも自分の claim でもない
        broken = [(ids[0], {"answer": 5}),
                  (ids[1], {"answer": {"claims": [], "gaps": [], "need": [],
                                       "steps": [{"from": ["P_x"], "to": "c9", "warrant": "理由"}]}})]
        for victim, bad in broken:
            link_input = rnd.link_input("link_r1_1", victim)
            path = self.work / "cache" / (rnd.link_key(link_input) + ".json")
            self.assertTrue(path.exists())
            path.write_text(json.dumps(bad), encoding="utf-8")
            self.init()
            bundles, _ = parse(self.next().stdout)
            jobs = [json.loads(Path(p).read_text(encoding="utf-8")) for _, _, ps in bundles for p in ps]
            # 壊した決定の link_r1_1 だけが出る（もう1つの決定はキャッシュに当たる）
            self.assertEqual([(j["stage"], j["input"]["target"]["id"]) for j in jobs], [("link_r1_1", victim)])
            self.answer(bundles)
            last, _ = self.drive()
            self.assertTrue(last.startswith("DONE decisions=2"))
            self.assertIsNotNone(rnd.cache_get(rnd.link_key(link_input), link_input))

    def test_editing_a_unit_relinks_only_dependent_decisions(self):
        far = "遠くの読み直しの理由を書く。"
        base = self.labels
        # インスタンスの属性に置くので、self を取らない関数のまま差し替える
        self.labels = lambda stage, variant, uid, text: ([decision(text)] if text == far
                                                         else base(stage, variant, uid, text))
        self.init()
        self.drive()
        text = self.plan.read_text(encoding="utf-8").replace("無関係な文その7。", "無関係な文その七。")
        self.plan.write_text(text, encoding="utf-8")
        self.init()
        _, stages = self.drive()
        jobs = sorted((self.work / "jobs" / "link_r1_1").glob("link_r1_1_*_????????.json"))
        # 「その7」を前後5単位に含む遠くの決定だけが根拠付けし直しになる（Tick と監視の決定の候補には入っていない）
        self.assertEqual([json.loads(j.read_text(encoding="utf-8"))["input"]["target"]["plan_text"] for j in jobs], [far])
        text = text.replace("監視は使わない。", "監視は決して使わない。")
        self.plan.write_text(text, encoding="utf-8")
        self.init()
        self.drive()
        jobs = sorted((self.work / "jobs" / "link_r1_1").glob("link_r1_1_*_????????.json"))
        targets = sorted(json.loads(j.read_text(encoding="utf-8"))["input"]["target"]["plan_text"] for j in jobs)
        # 直した単位そのものの決定と、それを候補に含む Tick の決定の2件。遠くの決定（3件目）は出ない
        self.assertEqual(targets, ["監視は決して使わない。", "読み直しは `Tick` の頭で1回だけ行う。"])
        self.assertEqual(len(self.decision_ids()), 3)

    def test_tampered_link_input_stops(self):
        self.init()
        for _ in range(5):
            bundles, _ = parse(self.next().stdout)
            paths = [p for _, _, ps in bundles for p in ps]
            job = json.loads(Path(paths[0]).read_text(encoding="utf-8"))
            if job["stage"] == "link_r1_1":
                break
            self.answer(bundles)
        original = Path(paths[0]).read_text(encoding="utf-8")
        fake = {"section": "", "plan_text": "偽", "nodes": [{"id": "P_ffffffff_1", "kind": "fact", "text": "偽"}]}
        for name, change in (("candidates", lambda j: j["input"]["candidates"].append(fake)),
                             ("target", lambda j: j["input"]["target"].update(text="書き換えた決定"))):
            with self.subTest(name=name):
                j = json.loads(original)
                change(j)
                Path(paths[0]).write_text(json.dumps(j, ensure_ascii=False), encoding="utf-8")
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
            if job["stage"] == "link_r1_1":
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
