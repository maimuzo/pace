"""pace.py next の unittest。決まりは docs/plans/pace_design.md の14節（ジョブと束）と9節（作業ディレクトリ）。

テストが実行役（subagent）の代わりに *.out.json を書き、next を subprocess で叩く。
束の分け方・鍵の計算・定数は、test_split.py と同じく import して確かめる（指示文と agent の定義を差し替えるため）。
"""

import json
import os
import stat
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

## 節A

読み直しは `Tick` の頭で1回だけ行う。
本体は reflect を使っていない。

## 節B

- 設定は差し替えない
- 安全側に倒す
"""


def run(*args):
    return subprocess.run([sys.executable, str(PACE), *args], capture_output=True, text=True)


def parse(out):
    """next の出力を [(束の ID, agent, [ジョブのパス])] と最後の行にする。"""
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


def answer_all(bundles, labels_for=None):
    """束の全ジョブに、検証を通る答えを書く。labels_for(stage, variant, uid) でラベルを選ぶ。"""
    labels_for = labels_for or (lambda stage, variant, uid: [decision()])
    for _, _, paths in bundles:
        for p in paths:
            job = json.loads(Path(p).read_text(encoding="utf-8"))
            if job["stage"] == "classify":
                ans = {"units": [{"uid": u["uid"], "labels": labels_for("classify", job["variant"], u["uid"])}
                                 for u in job["input"]["units"]]}
            else:
                ans = {"labels": labels_for("adjudicate", None, job["input"]["unit"]["uid"])}
            Path(job["out_path"]).write_text(json.dumps(ans, ensure_ascii=False), encoding="utf-8")


class NextTest(unittest.TestCase):
    def setUp(self):
        self._umask = os.umask(0o022)
        self.addCleanup(os.umask, self._umask)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
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

    def units(self):
        return [json.loads(x) for x in (self.work / "units.jsonl").read_text(encoding="utf-8").splitlines()]

    def drive(self, labels_for=None, limit=20):
        """DONE まで、毎回の波に答えを書いて回す。最後の行を返す。"""
        for _ in range(limit):
            bundles, last = parse(self.next().stdout)
            if last.startswith("DONE"):
                return last
            answer_all(bundles, labels_for)
        self.fail("DONE に届かない")

    # --- 束 ---------------------------------------------------------------

    def test_first_wave_has_a_and_b_in_separate_bundles(self):
        self.init()
        bundles, last = parse(self.next().stdout)
        self.assertEqual(last, "WAVE bundles=2 jobs=6")
        variants = []
        for _, agent, paths in bundles:
            self.assertEqual(agent, "pace:pace-worker")
            vs = {json.loads(Path(p).read_text(encoding="utf-8"))["variant"] for p in paths}
            self.assertEqual(len(vs), 1)
            variants.append(vs.pop())
        self.assertEqual(variants, ["A", "B"])

    def test_b_reverses_unit_order(self):
        self.init()
        bundles, _ = parse(self.next().stdout)
        a = json.loads(Path(bundles[0][2][0]).read_text(encoding="utf-8"))
        b_jobs = [json.loads(Path(p).read_text(encoding="utf-8")) for p in bundles[1][2]]
        b_last = [u["uid"] for u in b_jobs[-1]["input"]["units"]]
        self.assertEqual(b_last, list(reversed([u["uid"] for u in a["input"]["units"]])))

    def test_bundle_limits(self):
        jobs = [{"variant": "A", "job_id": "j%d" % i, "pad": "x" * 25000} for i in range(5)]
        jobs += [{"variant": "B", "job_id": "k%d" % i} for i in range(45)]
        out = pace.Round.bundles(jobs)
        for b in out:
            self.assertLessEqual(len(b), pace.BUNDLE_MAX_JOBS)
            self.assertEqual(len({j["variant"] for j in b}), 1)
            if len(b) > 1:
                self.assertLessEqual(sum(len(pace.dumps(j)) for j in b), pace.BUNDLE_MAX_CHARS)
        self.assertEqual([b[0]["variant"] for b in out[:4]], ["A", "B", "A", "B"])

    def test_wave_has_at_most_four_bundles(self):
        body = "\n".join("## 節%d\n\n%s\n" % (i, "あ" * 9000) for i in range(6))
        self.plan.write_text("# 題\n\n" + body, encoding="utf-8")
        self.init()
        bundles, last = parse(self.next().stdout)
        self.assertEqual(len(bundles), pace.PARALLEL)
        self.assertTrue(last.startswith("WAVE bundles=4 "))

    def test_classify_job_is_cut_by_chars(self):
        body = "".join("文%d。%s\n\n" % (i, "い" * 3000) for i in range(10))
        self.plan.write_text("# 題\n\n## 節\n\n" + body, encoding="utf-8")
        self.init()
        self.next()
        for p in (self.work / "jobs" / "classify").glob("classify_*_???_*.json"):
            if ".out" in p.name or ".rejected" in p.name:
                continue
            job = json.loads(p.read_text(encoding="utf-8"))
            if len(job["input"]["units"]) > 1:
                self.assertLessEqual(len(pace.dumps(job)), pace.JOB_MAX_CHARS)

    def test_job_shape_paths_and_permissions(self):
        self.init()
        bundles, _ = parse(self.next().stdout)
        p = Path(bundles[0][2][0])
        self.assertTrue(p.is_absolute())
        job = json.loads(p.read_text(encoding="utf-8"))
        for key in ("job_id", "stage", "cache_key", "prompt_version", "instructions", "input", "out_path",
                    "attempts", "previous_errors", "status"):
            self.assertIn(key, job)
        self.assertTrue(Path(job["out_path"]).is_absolute())
        self.assertEqual(job["status"], "issued")
        self.assertEqual(job["cache_key"], "")
        self.assertTrue(all("cache_key" not in u for u in job["input"]["units"]))  # 鍵はジョブに持たない
        self.assertIn("code_line", job["instructions"])
        self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.work / "jobs").stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.work / "jobs" / "classify").stat().st_mode), 0o700)
        self.assertIn("節A", p.read_text(encoding="utf-8"))  # 日本語を \uXXXX にしない

    def test_job_path_with_space(self):
        d = self.dir / "a b"
        d.mkdir()
        self.plan = d / "plan.md"
        self.plan.write_text(PLAN, encoding="utf-8")
        self.work = d / ".pace" / "plan"
        self.init()
        bundles, _ = parse(self.next().stdout)
        for _, _, paths in bundles:
            for p in paths:
                self.assertTrue(Path(p).is_file(), p)

    # --- 検証と差し戻し ----------------------------------------------------

    def first_job(self):
        self.init()
        bundles, _ = parse(self.next().stdout)
        return bundles, json.loads(Path(bundles[0][2][0]).read_text(encoding="utf-8"))

    def write_answer(self, job, text):
        Path(job["out_path"]).write_text(text, encoding="utf-8")

    def reload(self, job):
        return json.loads((self.work / "jobs" / job["stage"] / (job["job_id"] + ".json")).read_text(encoding="utf-8"))

    def test_bad_answers_are_sent_back(self):
        cases = [
            "これは JSON ではない",
            "[1, 2]",
            '{"units": []}',
            "[" * 100000 + "]" * 100000,
        ]
        for text in cases:
            with self.subTest(text=text[:20]):
                self.setUp()
                bundles, job = self.first_job()
                answer_all(bundles[1:])
                self.write_answer(job, text)
                self.next()
                job2 = self.reload(job)
                self.assertEqual(job2["status"], "issued")  # 差し戻して、次の波にまた出た
                self.assertEqual(job2["attempts"], 1)
                self.assertEqual(len(job2["previous_errors"]), 1)
                self.assertTrue((Path(job["out_path"]).parent / (job["job_id"] + ".rejected-1.json")).exists())
                self.assertFalse(Path(job["out_path"]).exists())

    def test_label_shape_errors(self):
        uids = None
        bad_labels = [
            [{"kind": "decision", "text": "x", "alternatives": []}],
            [{"kind": "premise", "ptype": "guess", "text": "x"}],
            [{"kind": "other", "text": "x"}],
            [{"kind": "decision", "text": " ", "alternatives": ["y"]}],
        ]
        for labels in bad_labels:
            with self.subTest(labels=labels):
                self.setUp()
                bundles, job = self.first_job()
                uids = [u["uid"] for u in job["input"]["units"]]
                ans = {"units": [{"uid": u, "labels": labels} for u in uids]}
                self.write_answer(job, json.dumps(ans, ensure_ascii=False))
                self.next()
                self.assertEqual(self.reload(job)["attempts"], 1)

    def test_uid_missing_and_duplicate(self):
        for mode in ("missing", "dup", "extra"):
            with self.subTest(mode=mode):
                self.setUp()
                bundles, job = self.first_job()
                uids = [u["uid"] for u in job["input"]["units"]]
                items = [{"uid": u, "labels": []} for u in uids]
                if mode == "missing":
                    items = items[1:]
                elif mode == "dup":
                    items.append(items[0])
                else:
                    items.append({"uid": "uffffffff", "labels": []})
                self.write_answer(job, json.dumps({"units": items}))
                self.next()
                self.assertEqual(self.reload(job)["attempts"], 1)

    def test_empty_labels_and_unknown_keys_pass(self):
        bundles, job = self.first_job()
        uids = [u["uid"] for u in job["input"]["units"]]
        ans = {"units": [{"uid": u, "labels": [], "note": "x"} for u in uids], "extra": 1}
        self.write_answer(job, json.dumps(ans))
        self.next()
        self.assertEqual(self.reload(job)["status"], "done")

    def test_symlink_answer_is_rejected(self):
        bundles, job = self.first_job()
        real = self.dir / "real.json"
        uids = [u["uid"] for u in job["input"]["units"]]
        real.write_text(json.dumps({"units": [{"uid": u, "labels": []} for u in uids]}), encoding="utf-8")
        os.symlink(str(real), job["out_path"])
        self.next()
        self.assertEqual(self.reload(job)["attempts"], 1)

    def test_missing_answer_counts_and_failure_after_three_retries(self):
        bundles, job = self.first_job()
        mine = str((self.work / "jobs" / "classify" / (job["job_id"] + ".json")).absolute())

        def others(bundles):
            answer_all([(b, a, [p for p in ps if p != mine]) for b, a, ps in bundles])

        others(bundles)
        for n in range(1, 4):
            bundles, _ = parse(self.next().stdout)
            others(bundles)
            self.assertEqual(self.reload(job)["attempts"], n)
            self.assertEqual(self.reload(job)["status"], "issued")
        r = self.next()
        j = self.reload(job)
        self.assertEqual(j["status"], "failed")
        self.assertEqual(j["attempts"], 4)
        recs = [json.loads(x) for x in (self.work / "failed_jobs.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["job_id"], job["job_id"])
        failed_uids = set(recs[0]["units"])
        # 失敗したジョブは同じ周の中で出し直さない
        self.assertNotIn(str((self.work / "jobs" / "classify" / (job["job_id"] + ".json")).absolute()), r.stdout)
        last = self.drive()
        self.assertTrue(last.endswith("failed_jobs=1"), last)
        nodes = [json.loads(x) for x in (self.work / "nodes.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertFalse(failed_uids & {n["unit"] for n in nodes})
        # DONE のあとに打っても数が増えない
        self.assertEqual(self.next().stdout.strip(), last)

    # --- 段・比較・ノード化 -------------------------------------------------

    def test_agreeing_units_go_straight_to_nodes(self):
        self.init()
        last = self.drive(lambda stage, variant, uid: [decision(), premise()])
        n = len(self.units())
        self.assertEqual(last, "DONE decisions=%d premises=%d failed_jobs=0" % (n, n))
        nodes = [json.loads(x) for x in (self.work / "nodes.jsonl").read_text(encoding="utf-8").splitlines()]
        u = self.units()[0]
        self.assertEqual(nodes[0]["id"], "D_%s_1" % u["id"][1:])
        self.assertEqual(nodes[1]["id"], "P_%s_1" % u["id"][1:])
        self.assertEqual(nodes[1]["ptype"], "fact")
        self.assertEqual({k for k in nodes[0]}, {"id", "kind", "text", "alternatives", "unit", "line", "sec", "path"})
        self.assertFalse((self.work / "jobs" / "adjudicate" / "created.json").read_text().count("adjudicate_"))

    def test_disagreement_goes_to_judge(self):
        self.init()
        target = self.units()[2]["id"]

        def labels(stage, variant, uid):
            if stage == "adjudicate":
                return [premise("裁定の答え", "value")]
            if uid == target and variant == "B":
                return [decision(), decision()]
            return [decision()]

        seen = []
        for _ in range(10):
            bundles, last = parse(self.next().stdout)
            if last.startswith("DONE"):
                break
            seen += [agent for _, agent, _ in bundles]
            answer_all(bundles, labels)
        self.assertIn("pace:pace-judge", seen)
        nodes = [json.loads(x) for x in (self.work / "nodes.jsonl").read_text(encoding="utf-8").splitlines()]
        mine = [n for n in nodes if n["unit"] == target]
        self.assertEqual([n["text"] for n in mine], ["裁定の答え"])

    def test_cache_makes_second_round_free(self):
        self.init()
        first = self.drive()
        self.init()
        r = self.next()
        self.assertEqual(r.stdout.strip(), first)

    def test_cache_hit_classify_still_runs_judge(self):
        """分類が全部キャッシュに当たっても、裁定が要る単位は裁定に回る（前の周で裁定が失敗した、など）。"""
        self.init()
        target = self.units()[1]["id"]

        def labels(stage, variant, uid):
            if uid == target and variant == "B":
                return []
            return [decision()]

        bundles, _ = parse(self.next().stdout)
        answer_all(bundles, labels)
        bundles, last = parse(self.next().stdout)  # 裁定の波
        self.assertEqual([a for _, a, _ in bundles], ["pace:pace-judge"])
        # 裁定を答えずに4回打って失敗させる
        for _ in range(4):
            self.next()
        self.assertIn("failed_jobs=1", self.next().stdout)
        self.init()
        bundles, last = parse(self.next().stdout)
        self.assertEqual([a for _, a, _ in bundles], ["pace:pace-judge"])

    def test_neighbors_only_change_does_not_reclassify(self):
        self.init()
        self.drive()
        text = PLAN.replace("本体は reflect を使っていない。", "本体は reflect を使っていない。\n前後に足した文。")
        self.plan.write_text(text, encoding="utf-8")
        self.init()
        bundles, last = parse(self.next().stdout)
        jobs = [json.loads(Path(p).read_text(encoding="utf-8")) for _, _, ps in bundles for p in ps]
        texts = {u["text"] for j in jobs for u in j["input"]["units"]}
        self.assertEqual(texts, {"前後に足した文。"})

    def test_cache_key_includes_instructions_and_agent(self):
        self.init()
        r = pace.Round(str(self.plan))
        unit = r.units[0]
        base = r.classify_key("A", unit)
        self.assertNotEqual(base, r.classify_key("B", unit))
        saved = pace.INSTRUCTIONS["classify"]
        try:
            pace.INSTRUCTIONS["classify"] = saved + " "
            self.assertNotEqual(base, r.classify_key("A", unit))
        finally:
            pace.INSTRUCTIONS["classify"] = saved
        r.agent_sha["pace-worker"] = "0" * 64
        self.assertNotEqual(base, r.classify_key("A", unit))

    # --- 作り終えた印と網羅 -------------------------------------------------

    def test_stage_without_created_mark_is_rebuilt(self):
        self.init()
        self.next()
        (self.work / "jobs" / "classify" / "created.json").unlink()
        bundles, last = parse(self.next().stdout)
        self.assertEqual(last, "WAVE bundles=2 jobs=6")
        answer_all(bundles)
        self.assertTrue(self.drive().startswith("DONE"))

    def test_stray_answers_are_ignored(self):
        self.init()
        bundles, _ = parse(self.next().stdout)
        d = self.work / "jobs" / "classify"
        (d / "classify_A_001_00000000.out.json").write_text("{}", encoding="utf-8")
        (d / "classify_A_009_00000000.json").write_text("{}", encoding="utf-8")
        answer_all(bundles)
        self.assertTrue(self.drive().startswith("DONE"))

    def test_missing_coverage_stops(self):
        self.init()
        self.drive()
        # キャッシュを1件消してから、作り終えた印のある段のまま打ち直す
        victim = sorted((self.work / "cache").glob("*.json"))[0]
        victim.unlink()
        r = self.next(ok=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("init を打ち直して", r.stderr)
        self.assertFalse((self.work / "nodes.jsonl").exists())
        # init を打ち直せば、欠けた分だけジョブを作り直して DONE まで行く
        self.init()
        bundles, last = parse(self.next().stdout)
        self.assertEqual(sum(len(ps) for _, _, ps in bundles), 1)
        answer_all(bundles)
        self.assertTrue(self.drive().startswith("DONE"))

    # --- 止まる場面 ---------------------------------------------------------

    def test_stops_without_writing(self):
        cases = ["changed", "no_plan_copy", "unreadable", "other_plan", "not_init", "cache_symlink"]
        for case in cases:
            with self.subTest(case=case):
                self.setUp()
                if case != "not_init":
                    self.init()
                if case == "changed":
                    self.plan.write_text(PLAN + "足した。\n", encoding="utf-8")
                elif case == "no_plan_copy":
                    (self.work / "plan.md").unlink()
                elif case == "unreadable":
                    self.plan.write_bytes(b"\xff\xfe")
                elif case == "other_plan":
                    (self.work / "source.json").write_text('{"plan_file": "plan.txt"}\n', encoding="utf-8")
                elif case == "cache_symlink":
                    outside = self.dir / "outside"
                    outside.mkdir()
                    os.symlink(str(outside), str(self.work / "cache"))
                r = self.next(ok=False)
                self.assertEqual(r.returncode, 1, case)
                self.assertTrue(r.stderr.startswith("pace.py next: "), r.stderr)
                self.assertNotIn("Traceback", r.stderr)
                self.assertFalse((self.work / "jobs").exists() and any((self.work / "jobs").iterdir()), case)
                self.assertFalse((self.work / "pending.json").exists(), case)
                if case != "cache_symlink":
                    self.assertFalse((self.work / "cache").exists(), case)
                if case == "cache_symlink":
                    self.assertEqual(list((self.dir / "outside").iterdir()), [])

    def test_wrong_shape_work_files_stop_without_traceback(self):
        cases = {
            "created": ("jobs/classify/created.json", "[]"),
            "round": ("jobs/round.json", "{}"),
            "job": (None, "[]"),
            "failed": ("failed_jobs.jsonl", "[]\n"),
            "failed_job_id": ("failed_jobs.jsonl", '{"job_id": [1], "units": []}\n'),
            "round_shape": ("jobs/round.json", '{"round": "zz"}'),
            "attempts": (None, "ATTEMPTS"),
        }
        for case, (rel, text) in cases.items():
            with self.subTest(case=case):
                self.setUp()
                self.init()
                bundles, _ = parse(self.next().stdout)
                if case in ("failed", "failed_job_id"):
                    answer_all(bundles)  # 失敗の記録を読むのは、分類が終わって裁定を作るとき
                path = Path(bundles[0][2][0]) if rel is None else self.work / rel
                if text == "ATTEMPTS":
                    raw = json.loads(path.read_text(encoding="utf-8"))
                    raw["attempts"] = -10 ** 30
                    text = json.dumps(raw, ensure_ascii=False)
                path.write_text(text, encoding="utf-8")
                r = self.next(ok=False)
                self.assertEqual(r.returncode, 1, case)
                self.assertNotIn("Traceback", r.stderr, case)
                self.assertIn("init を打ち直して", r.stderr, case)

    def test_broken_cache_is_rebuilt(self):
        for text in ("{", "[]", '{"labels": [{"kind": "zzz"}]}'):
            with self.subTest(text=text):
                self.setUp()
                self.init()
                self.drive()
                victim = sorted((self.work / "cache").glob("*.json"))[0]
                victim.write_text(text, encoding="utf-8")
                self.init()
                r = self.next()
                self.assertNotIn("Traceback", r.stderr)
                bundles, last = parse(r.stdout)
                self.assertTrue(last.startswith("WAVE"), last)
                answer_all(bundles)
                self.assertTrue(self.drive().startswith("DONE"))
                self.assertIn("labels", json.loads(victim.read_text(encoding="utf-8")))

    def test_judge_cache_is_used_after_init(self):
        self.init()
        target = self.units()[2]["id"]

        def labels(stage, variant, uid):
            if stage == "adjudicate":
                return [premise("裁定の答え", "value")]
            return [decision(), decision()] if (uid == target and variant == "B") else [decision()]

        first = self.drive(labels)
        nodes1 = (self.work / "nodes.jsonl").read_text(encoding="utf-8")
        self.init()
        self.assertEqual(self.next().stdout.strip(), first)
        self.assertEqual((self.work / "nodes.jsonl").read_text(encoding="utf-8"), nodes1)

    def test_cache_drops_unknown_keys_and_files_are_private(self):
        self.init()

        def labels(stage, variant, uid):
            return [dict(decision(), extra="捨てる")]

        self.drive(labels)
        for p in (self.work / "cache").glob("*.json"):
            obj = json.loads(p.read_text(encoding="utf-8"))
            self.assertEqual(set(obj), {"labels"})
            self.assertTrue(all(set(x) == {"kind", "text", "alternatives"} for x in obj["labels"]))
            self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)
        for name in ("pending.json", "nodes.jsonl"):
            self.assertEqual(stat.S_IMODE((self.work / name).stat().st_mode), 0o600, name)
        self.assertEqual(stat.S_IMODE((self.work / "cache").stat().st_mode), 0o700)

    def test_long_errors_are_cut(self):
        bundles, job = self.first_job()
        self.write_answer(job, json.dumps({"units": [{"uid": "u" + "x" * 2000, "labels": []}]}))
        self.next()
        errs = self.reload(job)["previous_errors"]
        self.assertEqual(len(errs), 1)
        self.assertEqual(len(errs[0]), pace.ERROR_MAX_CHARS + 1)
        self.assertTrue(errs[0].endswith("…"))

    def test_surrogates_in_answers_are_sent_back(self):
        """対のない surrogate（JSON の "\\ud83d"）を含む答えは、差し戻して、4回目に失敗したジョブにする。"""
        for mode in ("text", "uid"):
            with self.subTest(mode=mode):
                self.setUp()
                bundles, job = self.first_job()
                uids = [u["uid"] for u in job["input"]["units"]]
                if mode == "text":
                    items = [{"uid": u, "labels": [decision("SURROGATE")]} for u in uids]
                else:
                    items = [{"uid": "SURROGATE", "labels": []}]
                # json.dumps は surrogate をそのまま書けないので、印を JSON のエスケープに置き換える
                text = json.dumps({"units": items}).replace("SURROGATE", "\\ud83d")
                for n in range(1, 5):
                    Path(job["out_path"]).write_text(text, encoding="utf-8")
                    r = self.next()
                    self.assertNotIn("Traceback", r.stderr)
                self.assertEqual(self.reload(job)["status"], "failed")

    def test_tampered_job_cannot_touch_outside(self):
        bundles, job = self.first_job()
        outside = self.dir / "src.py"
        outside.write_text("print(1)\n", encoding="utf-8")
        path = self.work / "jobs" / "classify" / (job["job_id"] + ".json")
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["out_path"] = str(outside)
        raw["job_id"] = "../../../../escaped"
        path.write_text(json.dumps(raw), encoding="utf-8")
        r = self.next()
        self.assertNotIn("Traceback", r.stderr)
        self.assertEqual(outside.read_text(encoding="utf-8"), "print(1)\n")
        self.assertFalse(list(self.dir.glob("escaped*")))
        self.assertEqual(self.reload(job)["attempts"], 1)

    def test_tampered_cache_key_and_variant_are_not_used(self):
        """ジョブファイルの cache_key と variant は使わず、プランから分け直した単位と列の ID から作り直す。"""
        self.init()
        bundles, _ = parse(self.next().stdout)
        outside = self.dir / "package.json"
        outside.write_text('{"name": "app"}\n', encoding="utf-8")
        for _, _, paths in bundles:
            for p in paths:
                raw = json.loads(Path(p).read_text(encoding="utf-8"))
                for u in raw["input"]["units"]:
                    u["cache_key"] = "../../../package"
                raw["cache_key"] = "/tmp/x"
                raw["variant"] = "A"
                Path(p).write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        answer_all([(b, a, ps) for b, a, ps in bundles])
        last = self.drive()
        self.assertTrue(last.startswith("DONE"), last)
        self.assertEqual(outside.read_text(encoding="utf-8"), '{"name": "app"}\n')
        self.assertEqual(list(self.dir.glob("*.json")), [outside])
        # 作り直した鍵でキャッシュに入っているので、init し直すとジョブは出ない
        self.init()
        self.assertEqual(self.next().stdout.strip(), last)

    def test_bad_names_in_work_files_stop(self):
        """列の ID は形を確かめる。形の違う名前のジョブは、中身が正しくても読まない。"""
        self.init()
        bundles, _ = parse(self.next().stdout)
        d = self.work / "jobs" / "classify"
        mark = d / "created.json"
        info = json.loads(mark.read_text(encoding="utf-8"))
        first = info["jobs"][0]
        (d / "zzz.json").write_text((d / (first + ".json")).read_text(encoding="utf-8"), encoding="utf-8")
        info["jobs"][0] = "zzz"
        mark.write_text(json.dumps(info), encoding="utf-8")
        r = self.next(ok=False)
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("Traceback", r.stderr)
        self.assertIn("init を打ち直して", r.stderr)

    def test_job_uids_must_be_plan_units(self):
        for mode in ("unknown", "dup", "judge"):
            with self.subTest(mode=mode):
                self.setUp()
                self.init()
                if mode == "judge":
                    target = self.units()[2]["id"]

                    def labels(stage, variant, uid):
                        return [decision(), decision()] if (uid == target and variant == "B") else [decision()]

                    bundles, _ = parse(self.next().stdout)
                    answer_all(bundles, labels)
                    bundles, _ = parse(self.next().stdout)
                    path = Path(bundles[0][2][0])
                    raw = json.loads(path.read_text(encoding="utf-8"))
                    raw["input"]["unit"]["uid"] = self.units()[0]["id"]
                else:
                    bundles, _ = parse(self.next().stdout)
                    path = Path(bundles[0][2][0])
                    raw = json.loads(path.read_text(encoding="utf-8"))
                    if mode == "unknown":
                        raw["input"]["units"][0]["uid"] = "uffffffff"
                    else:
                        raw["input"]["units"].append(dict(raw["input"]["units"][0]))
                path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
                r = self.next(ok=False)
                self.assertEqual(r.returncode, 1, mode)
                self.assertNotIn("Traceback", r.stderr, mode)

    def test_units_jsonl_is_not_trusted(self):
        """units.jsonl を書き換えても、next はプランから分け直した単位でノードを作る。"""
        self.init()
        first = self.drive()
        nodes1 = (self.work / "nodes.jsonl").read_text(encoding="utf-8")
        (self.work / "units.jsonl").write_text('{"id": "../x"}\n', encoding="utf-8")
        self.assertEqual(self.next().stdout.strip(), first)
        self.assertEqual((self.work / "nodes.jsonl").read_text(encoding="utf-8"), nodes1)

    def test_more_than_999_jobs(self):
        """片側の分類のジョブが1000件を超えても、名前の形の確かめで止まらない。"""
        body = "".join("## 節%d\n\n文%d。\n\n" % (i, i) for i in range(1001))
        self.plan.write_text("# 題\n\n" + body, encoding="utf-8")
        self.init()
        r = self.next()
        self.assertTrue(r.stdout.strip().splitlines()[-1].startswith("WAVE"), r.stderr)
        r = self.next()
        self.assertNotIn("Traceback", r.stderr)
        self.assertTrue((self.work / "jobs" / "classify" / ("classify_A_1001_%s.json"
                        % json.loads((self.work / "jobs" / "round.json").read_text())["round"])).exists())

    def test_old_nodes_removed_when_plan_unreadable(self):
        self.init()
        self.drive()
        self.plan.write_bytes(b"\xff\xfe")
        r = self.next(ok=False)
        self.assertEqual(r.returncode, 1)
        self.assertFalse((self.work / "nodes.jsonl").exists())

    def test_tampered_job_input_stops(self):
        bundles, job = self.first_job()
        path = self.work / "jobs" / "classify" / (job["job_id"] + ".json")
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["input"] = {}
        path.write_text(json.dumps(raw), encoding="utf-8")
        r = self.next(ok=False)
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("Traceback", r.stderr)

    def test_old_nodes_removed_when_plan_changed(self):
        self.init()
        self.drive()
        self.plan.write_text(PLAN + "足した。\n", encoding="utf-8")
        r = self.next(ok=False)
        self.assertEqual(r.returncode, 1)
        self.assertFalse((self.work / "nodes.jsonl").exists())

    def test_agent_definition_file_changes_key(self):
        self.init()
        saved = pace.AGENTS_DIR
        with tempfile.TemporaryDirectory() as d:
            for name in ("pace-worker", "pace-judge"):
                (Path(d) / (name + ".md")).write_bytes((saved / (name + ".md")).read_bytes())
            try:
                pace.AGENTS_DIR = Path(d)
                unit = pace.Round(str(self.plan)).units[0]
                base = pace.Round(str(self.plan)).classify_key("A", unit)
                with open(str(Path(d) / "pace-worker.md"), "a", encoding="utf-8") as f:
                    f.write("\n足した。\n")
                self.assertNotEqual(base, pace.Round(str(self.plan)).classify_key("A", unit))
                judge = pace.Round(str(self.plan))
                self.assertEqual(judge.agent_digest("adjudicate"),
                                 pace.hashlib.sha256((saved / "pace-judge.md").read_bytes()).hexdigest())
            finally:
                pace.AGENTS_DIR = saved

    def test_broken_work_file_stops_without_traceback(self):
        self.init()
        self.next()
        (self.work / "jobs" / "classify" / "created.json").write_text("{", encoding="utf-8")
        # 作り終えた印が壊れていたら、読めない理由を出して 1 で終わる
        r = self.next(ok=False)
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("Traceback", r.stderr)


if __name__ == "__main__":
    unittest.main()
