"""pace.py の分割（split_units）の unittest。規則は docs/plans/pace_design.md の4節。"""

import hashlib
import sys
import unittest
from pathlib import Path

# plugins/pace/scripts は package ではないので、sys.path に足して import する
# pace を import しても、プラグインのディレクトリに __pycache__ を書かない（プラグインに余計なものを入れない）
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "pace" / "scripts"))

import pace  # noqa: E402


def split(text):
    return pace.split_units(text)


def kinds_texts(text):
    return [(u["kind"], u["text"]) for u in split(text)]


class KindsTest(unittest.TestCase):
    def test_five_kinds(self):
        text = "\n".join([
            "# 題",
            "",
            "本文の一文目。二文目。",
            "",
            "- 箇条書き。中の「。」では切らない。",
            "",
            "| 列1 | 列2 |",
            "| --- | --- |",
            "| a | b |",
            "",
            "```go",
            "func f() {",
            "}",
            "```",
        ])
        self.assertEqual(kinds_texts(text), [
            ("heading", "題"),
            ("sentence", "本文の一文目。"),
            ("sentence", "二文目。"),
            ("bullet", "箇条書き。中の「。」では切らない。"),
            ("table_row", "列1 | 列2"),
            ("table_row", "a | b"),
            ("code_line", "func f() {"),
            ("code_line", "}"),
        ])

    def test_rule_and_blank_are_dropped(self):
        self.assertEqual(kinds_texts("一。\n\n---\n\n***\n二。"), [("sentence", "一。"), ("sentence", "二。")])


class JoinTest(unittest.TestCase):
    def test_wrapped_paragraph_is_joined(self):
        units = split("前の行の途中で\n折り返した文。次の文。")
        self.assertEqual([u["text"] for u in units], ["前の行の途中で折り返した文。", "次の文。"])

    def test_ascii_joint_gets_space_and_trailing_space_is_dropped(self):
        self.assertEqual([u["text"] for u in split("use the  \nconfig file")], ["use the config file"])
        self.assertEqual([u["text"] for u in split("日本語の   \n折り返し。")], ["日本語の折り返し。"])
        # 片側でも ASCII なら空白を入れる（折り返す位置で本文が変わらないように）
        self.assertEqual([u["text"] for u in split("設定が\n`cfg` にある")], ["設定が `cfg` にある"])
        # 端の ** は外すので、その内側の字で決める
        self.assertEqual([u["text"] for u in split("**重要**\nです。")], ["重要です。"])
        # 見ないのは ** だけ。単独の * は ASCII の字として数える
        self.assertEqual([u["text"] for u in split("*強調*\nです。")], ["*強調* です。"])

    def test_blank_line_starts_new_paragraph(self):
        self.assertEqual([u["text"] for u in split("一つ目\n\n二つ目")], ["一つ目", "二つ目"])

    def test_bullet_continuation(self):
        text = "- 項目の\n  続き\n    深い字下げも続き\n- 次の項目\n  1. 入れ子"
        self.assertEqual(kinds_texts(text), [
            ("bullet", "項目の続き深い字下げも続き"),
            ("bullet", "次の項目"),
            ("bullet", "入れ子"),
        ])

    def test_structure_lines_are_not_swallowed(self):
        text = "- 項目\n| a | b |\n# 見出し\n```\nx\n```\n> 引用"
        self.assertEqual(kinds_texts(text), [
            ("bullet", "項目"),
            ("table_row", "a | b"),
            ("heading", "見出し"),
            ("code_line", "x"),
            ("sentence", "引用"),
        ])

    def test_marker_only_bullet_points_to_marker_line(self):
        self.assertEqual([(u["kind"], u["text"], u["line"]) for u in split("1.\n  本文")], [("bullet", "本文", 1)])

    def test_four_space_indent_is_continuation(self):
        # 開いている段落にはつながり、空行のあとでは新しい段落になる（4つ字下げのコードブロックは扱わない）
        self.assertEqual(kinds_texts("本文\n    続き"), [("sentence", "本文続き")])
        self.assertEqual(kinds_texts("本文\n\n    {\"a\": 1}"), [("sentence", "本文"), ("sentence", "{\"a\": 1}")])

    def test_line_of_second_sentence(self):
        units = split("一行目の文。\n二行目で\n始まる文。")
        self.assertEqual([(u["text"], u["line"]) for u in units],
                         [("一行目の文。", 1), ("二行目で始まる文。", 2)])


class QuoteTest(unittest.TestCase):
    def test_quote_is_stripped_and_joined(self):
        text = "> 引用の\n> 続き。次。\n本文。"
        self.assertEqual(kinds_texts(text), [
            ("sentence", "引用の続き。"),
            ("sentence", "次。"),
            ("sentence", "本文。"),
        ])

    def test_table_in_quote(self):
        self.assertEqual(kinds_texts("> | a | b |\n> | - | - |"), [("table_row", "a | b")])

    def test_heading_in_quote_does_not_move_path_or_sec(self):
        units = split("## 本当の節\n\n> ## 引いた節\n\n本文。")
        self.assertEqual(units[1]["kind"], "heading")
        self.assertEqual(units[1]["text"], "引いた節")
        self.assertEqual(units[1]["sec"], "本当の節")
        self.assertEqual(units[2]["path"], ["本当の節"])
        self.assertEqual(units[2]["sec"], "本当の節")

    def test_quote_mark_in_fence_outside_quote_is_kept(self):
        self.assertEqual(kinds_texts("```\n> echo hi\n```"), [("code_line", "> echo hi")])

    def test_fence_in_quote(self):
        self.assertEqual(kinds_texts("> ```\n> x\n> > y\n> ```\n> 後。"),
                         [("code_line", "x"), ("code_line", "> y"), ("sentence", "後。")])


class TableTest(unittest.TestCase):
    def test_escaped_pipe_and_empty_cells(self):
        # 空のセルも残す。空白は1つに詰めるので、空のセルは "| |" になる
        self.assertEqual(kinds_texts("| `a\\|b` | | c |"), [("table_row", "`a|b` | | c")])

    def test_empty_edge_cells(self):
        self.assertEqual(kinds_texts("| | a | |"), [("table_row", "| a |")])

    def test_dash_data_row_is_kept(self):
        # 区切り行として捨てるのは表の2行目だけ
        self.assertEqual(kinds_texts("| 列 | 値 |\n| --- | --- |\n| - | - |\n\n| --- |"),
                         [("table_row", "列 | 値"), ("table_row", "- | -"), ("table_row", "---")])

    def test_row_without_trailing_pipe(self):
        self.assertEqual(kinds_texts("| a | b"), [("table_row", "a | b")])

    def test_cell_is_not_split_by_period(self):
        self.assertEqual(kinds_texts("| 一。二。 |"), [("table_row", "一。二。")])


class HeadingTest(unittest.TestCase):
    def test_closing_hashes(self):
        self.assertEqual(kinds_texts("## C#\n## 題 ##\n## ##"), [("heading", "C#"), ("heading", "題")])

    def test_tab_counts_as_one_indent(self):
        self.assertEqual(kinds_texts("\t# 題\n\t| a |\n\t---\n本文"),
                         [("heading", "題"), ("table_row", "a"), ("sentence", "本文")])

    def test_empty_heading_is_ignored(self):
        units = split("## 節\n##\n本文。")
        self.assertEqual([(u["kind"], u["path"]) for u in units], [("heading", []), ("sentence", ["節"])])

    def test_path_and_sec(self):
        units = split("# 一\n## 二\n### 三\n本文。\n# 次の一\n本文。")
        self.assertEqual([(u["path"], u["sec"]) for u in units], [
            ([], ""),
            (["一"], "二"),
            (["一", "二"], "二"),
            (["一", "二", "三"], "二"),
            ([], ""),
            (["次の一"], ""),
        ])


class FenceTest(unittest.TestCase):
    def test_unclosed_fence_runs_to_end(self):
        self.assertEqual(kinds_texts("```\na\n# b"), [("code_line", "a"), ("code_line", "# b")])

    def test_tilde_and_long_fence(self):
        self.assertEqual(kinds_texts("~~~~\n```\n~~~\n~~~~\n後。"),
                         [("code_line", "```"), ("code_line", "~~~"), ("sentence", "後。")])

    def test_indented_open_and_close(self):
        text = "- 項目\n\n      ```text\n      - 中\n      ```\n後。"
        self.assertEqual(kinds_texts(text), [("bullet", "項目"), ("code_line", "      - 中"), ("sentence", "後。")])

    def test_code_line_is_not_normalized(self):
        self.assertEqual(kinds_texts("```\n    f(**kwargs,   x)   \n```"), [("code_line", "    f(**kwargs,   x)")])

    def test_fence_on_bullet_line(self):
        text = "- ```go\n  x := 1\n  ```\n\n## 次の節\n\n本文。\n\n1. ~~~\n   y\n   ~~~\n後。"
        self.assertEqual(kinds_texts(text), [
            ("code_line", "  x := 1"),
            ("heading", "次の節"),
            ("sentence", "本文。"),
            ("code_line", "   y"),
            ("sentence", "後。"),
        ])

    def test_blank_lines_in_fence_are_dropped(self):
        self.assertEqual(kinds_texts("```\na\n\n   \nb\n```"), [("code_line", "a"), ("code_line", "b")])

    def test_inline_triple_backtick_is_not_fence(self):
        self.assertEqual(kinds_texts("```x```。後。"), [("sentence", "```x```。"), ("sentence", "後。")])


class SpeedTest(unittest.TestCase):
    def test_large_paragraph_is_not_quadratic(self):
        import time
        start = time.monotonic()
        split("。\n".join(["文"] * 40000))
        split("`a`**" * 40000)
        split("a" + "*" * 1600000 + "\nb")
        split("# a" + " " * 80000 + "b")
        split("".join("`" * k + " " for k in range(1200, 1, -1)) + "`a` " * 100000)
        split("a**\n" * 400000)
        split(">" * 800000 + " x")
        # 2乗の版では、4万行の段落で10秒、`a`** の4万回で38秒、* の160万個で8秒、
        # 8万の空白の見出しで15秒、閉じない backtick の幅1,200種で約17秒、
        # ** で終わる40万行で8秒、> の80万段で4秒かかった（Python 3.9.6）。
        # どの1つを2乗に戻しても閾値を超え、線形の版は7つ合わせて2秒に届かない
        self.assertLess(time.monotonic() - start, 4.0)


class SentenceTest(unittest.TestCase):
    def test_no_split_inside_brackets_and_code(self):
        self.assertEqual([u["text"] for u in split("彼は「止める。」と言った。`a。b` を使う。")],
                         ["彼は「止める。」と言った。", "`a。b` を使う。"])

    def test_double_backtick(self):
        self.assertEqual([u["text"] for u in split("``a`。b`` を使う。次。")], ["``a`。b`` を使う。", "次。"])

    def test_unbalanced_bracket_drops_guard(self):
        self.assertEqual([u["text"] for u in split("「閉じない。次。")], ["「閉じない。", "次。"])

    def test_unclosed_backtick_drops_guard(self):
        self.assertEqual([u["text"] for u in split("`開く「中。」外。")], ["`開く「中。", "」外。"])

    def test_html_comment_is_text(self):
        self.assertEqual(kinds_texts("<!-- 目的: 決める。 -->"), [("sentence", "<!-- 目的: 決める。"), ("sentence", "-->")])


class NormalizeTest(unittest.TestCase):
    def test_bold_is_removed_outside_code(self):
        self.assertEqual([u["text"] for u in split("**強い。**`Edit(**/.pace/**)` を足す。")],
                         ["強い。", "`Edit(**/.pace/**)` を足す。"])

    def test_line_endings(self):
        # 行の終わりは \r\n・\r・\n だけ。U+2028 は行の終わりにしない
        units = split("一。\r\n\r\n二。\r\r三 続き。")
        self.assertEqual([(u["text"], u["line"]) for u in units], [("一。", 1), ("二。", 3), ("三 続き。", 5)])


class IdTest(unittest.TestCase):
    def test_id_and_duplicates(self):
        units = split("```\n}\n}\n}\n```")
        base = "u" + hashlib.sha256("\n}".encode("utf-8")).hexdigest()[:8]
        self.assertEqual([u["id"] for u in units], [base, base + "_2", base + "_3"])
        self.assertEqual([u["pos"] for u in units], [0, 1, 2])

    def test_keys(self):
        self.assertEqual(list(split("一。")[0].keys()), ["id", "pos", "kind", "line", "path", "sec", "text"])


if __name__ == "__main__":
    unittest.main()
