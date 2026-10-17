"""キャッシュの有無で結果が一致することの unittest。決まりは docs/plans/pace_design.md の9節・10節・15節・19節。

流れは test_decycle.py の道具（FlowBase）で、テストが実行役の代わりに *.out.json を書き、init・next・report（限界の
テストでは verify も）を subprocess で叩く。決定の ID はテストのプロセスの中で読むだけの Round から引く。1周目のあと、
プランと .pace/ をまるごと別の一時ディレクトリへ写し、写した側だけ cache/ を消して、同じ rounds.jsonl から始まる同じ
2周目を2つ回して比べる（source.json の plan_file はファイル名だけで、jobs/ の絶対パスの out_path は init が jobs/ ごと
消すので、写した側でも check_owner を通る）。

- 比べるもの（標準出力の最後の2行・report.md・fixes.md・questions.md・nodes.jsonl・rounds.jsonl の最後の行）は、
  写した場所によらない。rounds.jsonl の行のうち run（周の印の乱数）と jobs（キャッシュの有無で違って当然）は比べず、
  jobs は段ごとの値を別に確かめる。標準出力の FILE fixes の行とジョブファイルは絶対パスを持つので比べない。
- 一致のテストで落ちると確かめた pace.py の壊し方は4つ。キャッシュありとなしの比べで落ちるのは、根拠付けの鍵から
  candidates を抜く・裁定の鍵から text と A と B を抜く（nodes.jsonl が食い違う）の2つ。切った辺の記憶の鍵を空にする
  壊し方はキャッシュなしの jobs の値で、分類の鍵から variant を抜く壊し方は1周目の PACE-STATUS の値で、はじめて落ちる
  （jobs と1周目の値を書き直すときは、この2つの壊し方に気づけなくなることに気をつける）。反論の鍵を決定だけに狭める
  壊し方と、単位の本文と一緒に変わる入力（単位の ID は path と text から作るので、本文を直すと ID を持つ鍵は全部
  変わる）の入れ忘れは、このテストでは見つけられない。
- 18節の限界の当て方: 「順1は版を見ない」は、キャッシュありとなしが同じ rounds.jsonl から始まるので両方に同じく当たる。
  「周の途中の plugin の更新」は plugin を更新しないので当たらない。「同じ周の2回目の report」は各周で report を
  1回だけ打つので当たらない。
- 限界のテスト2つは、切った辺の記憶がキャッシュありの周でだけ回1から効くために、キャッシュの有無で結果が変わる
  いまの振る舞いを押さえる（10節）。issue #8 の作業の報告で起票を頼んだ、記憶の効き方の bug で直す対象で、
  直したら落ちて気づける。起票したら、その issue の番号をここに書く。
"""

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "pace" / "scripts"))
import pace  # noqa: E402
from test_decycle import FlowBase, PLAN, RING_PLAN, cut_b_to_a, cut_first, parse, ring_link, run  # noqa: E402
from test_report import accept_b  # noqa: E402

# 一致のテストのプラン。足す1行（ADDED）は決定A・B・C から前後5単位の外で、冒頭の節（## 設計）にも、決定A・B・C と
# 同じ H2 にも入らないので、比べる周でも決定A・B・C の根拠付けと反論はキャッシュに当たる
SPLIT_PLAN = ("# 題\n\n## 設計\n\n前提の文。\n決定Aを選ぶ。\n決定Bを選ぶ。\n決定Cを選ぶ。\n\n## 別\n\n"
              + "".join("埋めの文%dを書く。\n" % i for i in range(1, 9)) + "前提の別の文。\n決定Dを選ぶ。\n")
ADDED = "前提を足した文。"
SPLIT_EDITED = SPLIT_PLAN.replace("前提の別の文。\n", "前提の別の文。\n%s\n" % ADDED)

# 一致のテストの根拠付け: 決定（原文）ごとに理由にする候補の原文。決定A と決定B は互いを理由にし合う（輪）
SPLIT_SUPPORT = {"決定Aを選ぶ。": ["決定Bを選ぶ。"], "決定Bを選ぶ。": ["決定Aを選ぶ。"], "決定Cを選ぶ。": ["前提の文。"],
                 "決定Dを選ぶ。": [ADDED]}

# 輪をほどいた周の足す1行。決定B がこれを理由にすると、決定A⇄決定B の輪がほどける
UNTIE = "追記1の前提。"


def support_link(support, gap_ptype):
    """support の候補が入力にあれば理由にし、無ければ gap にする根拠付けの答え。gap_ptype(決定の原文) が gap の ptype。"""
    def link(job):
        target = job["input"]["target"]
        ids = {g["plan_text"]: n["id"] for g in job["input"]["candidates"] for n in g["nodes"]}
        froms = [ids[t] for t in support(target["plan_text"], ids) if t in ids]
        if froms:
            return {"claims": [], "gaps": [], "need": [],
                    "steps": [{"from": froms, "to": target["id"], "warrant": "理由"}]}
        return {"claims": [], "gaps": [{"id": "g1", "ptype": gap_ptype(target["plan_text"]), "text": "穴"}], "need": [],
                "steps": [{"from": ["g1"], "to": target["id"], "warrant": "理由"}]}
    return link


def untie_link(job):
    """決定B は UNTIE が候補にあればそれを、無ければ決定A を理由にする（ほかは test_decycle.py の SUPPORT と同じ）。"""
    want = {"決定Aを選ぶ。": ["決定Bを選ぶ。"], "決定Cを選ぶ。": ["前提の文。"]}
    return support_link(lambda text, ids: want.get(text) or ([UNTIE] if UNTIE in ids else ["決定Aを選ぶ。"]),
                        lambda text: "fact")(job)


class CacheFlowTest(FlowBase):
    def copy(self):
        """プランと .pace/ を別の一時ディレクトリへ写し、写した側の cache/ を消す。写した側のプランのパスを返す。"""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        dst = Path(tmp.name) / "copy"
        shutil.copytree(str(self.dir), str(dst), symlinks=True)
        shutil.rmtree(str(dst / ".pace" / "plan" / "cache"))
        return dst / "plan.md"

    def cycle(self, plan, answer):
        """plan で init から DONE まで回し、report を打つ。比べるものと jobs/ の場所を返す。"""
        r = run("init", str(plan))
        self.assertEqual(r.returncode, 0, r.stderr)
        for _ in range(60):
            r = run("next", str(plan))
            self.assertEqual(r.returncode, 0, r.stderr)
            bundles, last = parse(r.stdout)
            if last.startswith("DONE"):
                break
            answer(bundles)
        else:
            self.fail("DONE に届かない")
        r = run("report", str(plan))
        self.assertEqual(r.returncode, 0, r.stderr)
        work = plan.parent / ".pace" / "plan"
        record = json.loads((work / "rounds.jsonl").read_text().splitlines()[-1])
        jobs = record.pop("jobs")
        del record["run"]
        files = {name: (work / name).read_text(encoding="utf-8")
                 for name in ("report.md", "fixes.md", "questions.md", "nodes.jsonl")}
        return {"stdout": r.stdout.splitlines()[-2:], "files": files, "record": record, "jobs": jobs,
                "jobs_dir": work / "jobs"}

    def decision_id(self, plan, text):
        rnd = pace.Round(str(plan), readonly=True)
        return next(n["id"] for n in rnd.decisions() if rnd.units[rnd.pos[n["unit"]]]["text"] == text)

    def verify_status(self, plan, decision):
        r = run("verify", str(plan))
        self.assertEqual(r.returncode, 0, r.stderr)
        return next(x.split()[2] for x in r.stdout.splitlines() if x.startswith("DECISION %s " % decision))

    # --- 一致のテスト --------------------------------------------------------

    def split_answer(self, plan):
        """一致のテストの答え。「前提」で始まる文だけ分類の A と B を食い違わせ（B はラベル無し）、裁定は fact の前提。
        決定A の gap は value（questions に出る）、ほかの gap は fact。輪は決定B→決定A の辺で切り、反論は accept_b。"""
        link = support_link(lambda text, ids: SPLIT_SUPPORT.get(text, []),
                            lambda text: "value" if text == "決定Aを選ぶ。" else "fact")

        def answer(bundles):
            texts = {u["id"]: u["text"] for u in pace.split_units(plan.read_text(encoding="utf-8"))}
            for _, _, paths in bundles:
                for p in paths:
                    job = json.loads(Path(p).read_text(encoding="utf-8"))
                    if job["stage"] == "classify":
                        def labels(text):
                            if text.startswith("決定"):
                                return [{"kind": "decision", "text": text, "alternatives": ["別"]}]
                            if text.startswith("前提") and job["variant"] == "B":
                                return []
                            return [{"kind": "premise", "ptype": "fact", "text": text}]
                        ans = {"units": [{"uid": u["uid"], "labels": labels(texts[u["uid"]])}
                                         for u in job["input"]["units"]]}
                    elif job["stage"] == "adjudicate":
                        ans = {"labels": [{"kind": "premise", "ptype": "fact", "text": job["input"]["unit"]["text"]}]}
                    elif job["stage"].startswith("link_"):
                        ans = link(job)
                    elif job["stage"].startswith("decycle_"):
                        ans = cut_b_to_a(job)
                    else:
                        ans = accept_b(job)
                    Path(job["out_path"]).write_text(json.dumps(ans, ensure_ascii=False), encoding="utf-8")
        return answer

    def test_cached_and_uncached_rounds_agree(self):
        self.plan.write_text(SPLIT_PLAN, encoding="utf-8")
        first = self.cycle(self.plan, self.split_answer(self.plan))
        self.assertEqual(first["stdout"], ["PACE-STATUS: errors=0 failed_jobs=0 fixes=2 questions=1", "NEXT: continue"])
        self.plan.write_text(SPLIT_EDITED, encoding="utf-8")
        other = self.copy()
        cached = self.cycle(self.plan, self.split_answer(self.plan))
        uncached = self.cycle(other, self.split_answer(other))
        self.assertEqual(cached["stdout"], ["PACE-STATUS: errors=0 failed_jobs=0 fixes=1 questions=1", "NEXT: continue"])
        for key in ("stdout", "files", "record"):
            self.assertEqual(cached[key], uncached[key], key)
        # キャッシュありは、足した単位の分類（A と B）と裁定、決定D の根拠付けと反論だけをやり直す（分類と裁定の鍵に
        # 前後の文は入らないので、近くの単位は分類し直さない）
        self.assertEqual(cached["jobs"], {"classify": 2, "adjudicate": 1, "link": 1, "decycle": 0, "review": 1})
        self.assertEqual(uncached["jobs"], {"classify": 6, "adjudicate": 3, "link": 5, "decycle": 1, "review": 3})
        relinked = json.loads((cached["jobs_dir"] / "link_r1_1" / "created.json").read_text())["jobs"]
        d = self.decision_id(self.plan, "決定Dを選ぶ。")
        self.assertEqual([j.split("_")[3:6] for j in relinked], [d.split("_")])

    # --- 限界のテスト（10節） ------------------------------------------------

    def test_ring_left_at_pass_six_differs(self):
        """回6で輪が残る周: キャッシュありの次の周は、記憶が回1から効いて残った輪をもう一度断つ。"""
        self.plan.write_text(RING_PLAN, encoding="utf-8")
        answer = lambda bundles: self.answer(bundles, link=ring_link, decycle=cut_first)
        first = self.cycle(self.plan, answer)
        self.assertEqual(first["stdout"], ["PACE-STATUS: errors=0 failed_jobs=0 fixes=6 questions=0", "NEXT: continue"])
        other = self.copy()
        cached = self.cycle(self.plan, answer)
        uncached = self.cycle(other, answer)
        self.assertEqual(cached["stdout"], ["PACE-STATUS: errors=0 failed_jobs=0 fixes=1 questions=0", "NEXT: show"])
        self.assertEqual(uncached["stdout"], ["PACE-STATUS: errors=0 failed_jobs=0 fixes=6 questions=0", "NEXT: show"])
        self.assertEqual(cached["jobs"], {"classify": 0, "adjudicate": 0, "link": 1, "decycle": 1, "review": 0})

    def test_untied_ring_differs(self):
        """輪をほどいた周: キャッシュありの周だけ、前の周に切った辺の記憶で決定A が決定B を候補から外したまま gap を持つ。"""
        self.plan.write_text(PLAN, encoding="utf-8")
        answer = lambda bundles: self.answer(bundles, link=untie_link)
        first = self.cycle(self.plan, answer)
        self.assertEqual(first["stdout"], ["PACE-STATUS: errors=0 failed_jobs=0 fixes=1 questions=0", "NEXT: continue"])
        self.plan.write_text(PLAN + UNTIE + "\n", encoding="utf-8")
        other = self.copy()
        cached = self.cycle(self.plan, answer)
        uncached = self.cycle(other, answer)
        self.assertEqual(cached["stdout"], ["PACE-STATUS: errors=0 failed_jobs=0 fixes=1 questions=0", "NEXT: continue"])
        self.assertEqual(uncached["stdout"], ["PACE-STATUS: errors=0 failed_jobs=0 fixes=0 questions=0", "NEXT: show"])
        a = self.decision_id(self.plan, "決定Aを選ぶ。")
        self.assertEqual((self.verify_status(self.plan, a), self.verify_status(other, a)), ("needs_gap", "rooted"))


if __name__ == "__main__":
    unittest.main()
