#!/usr/bin/env python3
"""PACE の機械の仕事を受け持つスクリプト。

いまは --version（段ごとの版の一覧）と init（プランを単位に分けて作業ディレクトリに置く）を持つ。
next・verify・report・show・shown は後の issue（maimuzo/pace#4 以降）で足す。
Python 3.9 以上・標準ライブラリだけで動かす。分割と作業ディレクトリの決まりは
docs/plans/pace_design.md の4節（入力）と9節（作業ディレクトリ）にある。
"""

import argparse
import bisect
import errno
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
from pathlib import Path

# 段ごとの版。キャッシュの鍵に入るので、段の指示文や出力の形を変えたら、その段の版を上げる。
# 版を持つのは AI に頼む段（キャッシュの鍵を持つ段）だけで、名前は docs/idea.md 付録A に揃える。
# 形は docs/idea.md 6.4 の prompt_version（日付.連番）に揃える。
STAGE_VERSIONS = (
    ("classify", "2026-10-16.1"),
    ("adjudicate", "2026-10-16.1"),
    ("link", "2026-10-16.1"),
    ("decycle", "2026-10-16.1"),
    ("review", "2026-10-16.1"),
)

# init が作り直さずに残すもの（作業ディレクトリの直下の名前）。
KEEP = frozenset({"cache", "rounds.jsonl", "shown.md", "show_snapshot.md", "source.json"})

UNREADABLE_STATUS = "PACE-STATUS: errors=1 failed_jobs=0 fixes=0 questions=0"


class PaceError(Exception):
    """errors に数えない失敗（読む権限が無い・書けない・作業ディレクトリが使えない）。標準エラーに理由を出して 1 で終わる。

    errors に数えるのは、プランが無いことと UTF-8 でないことだけ（docs/plans/pace_design.md の5節）。
    """


# ---------------------------------------------------------------------------
# 分割
# ---------------------------------------------------------------------------

LINE_END = re.compile(r"\r\n|\r|\n")
QUOTE_MARK = re.compile(r"[ \t]*>[ \t]?")
FENCE_OPEN = re.compile(r"[ \t]*(`{3,}|~{3,})(.*)")
HEADING = re.compile(r"[ \t]{0,3}(#{1,6})(?:[ \t]+(.*))?")
RULE = re.compile(r"[ \t]{0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*")
TABLE = re.compile(r"[ \t]{0,3}\|")
TABLE_SEPARATOR = re.compile(r":?-+:?")
BULLET = re.compile(r"[ \t]*(?:[-*+]|[0-9]+[.)])(?:[ \t]+(.*))?")
SPACES = re.compile(r"\s+")

OPEN_BRACKETS = {"「": "」", "『": "』", "（": "）"}
CLOSE_BRACKETS = {v: k for k, v in OPEN_BRACKETS.items()}


def strip_quote(line, limit=None):
    """行頭の引用の記号を段ごとに外し、(段数, 残り) を返す。limit があれば、その段数までしか外さない。"""
    depth = 0
    rest = line
    while limit is None or depth < limit:
        m = QUOTE_MARK.match(rest)
        if not m:
            break
        rest = rest[m.end():]
        depth += 1
    return depth, rest


def strip_closing_hashes(title):
    """見出しの末尾の # の並びを、前に空白があるか本文が # だけのときに外す（正規表現の後戻りを避ける）。"""
    body = title.rstrip(" \t")
    head = body.rstrip("#")
    if head == body:
        return title
    if head == "" or head[-1] in " \t":
        return head
    return title


def code_spans(text):
    """backtick で囲んだ範囲を [(始まり, 終わり)] で返す。閉じの無い並びがあれば ok は False。"""
    runs = [(m.start(), m.end()) for m in re.finditer(r"`+", text)]
    spans = []
    ok = True
    i = 0
    while i < len(runs):
        start, end = runs[i]
        width = end - start
        for j in range(i + 1, len(runs)):
            if runs[j][1] - runs[j][0] == width:
                spans.append((start, runs[j][1]))
                i = j + 1
                break
        else:
            ok = False
            i += 1
    return spans, ok


def in_spans(pos, spans):
    """spans は始まりの順に並んでいて重ならない。二分探索で引く（巨大な単位でも2乗にしない）。"""
    k = bisect.bisect_right(spans, (pos, float("inf"))) - 1
    return k >= 0 and spans[k][0] <= pos < spans[k][1]


def normalize(text):
    """backtick の外の ** を外し、空白の並びを1つにし、前後を落とす（code_line には使わない）。"""
    spans, ok = code_spans(text)
    if ok:
        out = []
        i = 0
        while i < len(text):
            if text.startswith("**", i) and not in_spans(i, spans):
                i += 2
                continue
            out.append(text[i])
            i += 1
        text = "".join(out)
    else:
        # 閉じの無い backtick がある単位では、どこが中かを決められないので全部外す
        text = text.replace("**", "")
    return SPACES.sub(" ", text).strip()


def join_pieces(pieces):
    """(行番号, 本文) の列を1つの本文につなぎ、(本文, [(始まりの位置, 行番号)]) を返す。"""
    raw = ""
    starts = []
    for line_no, piece in pieces:
        piece = piece.strip()
        if not piece:
            continue
        # 日本語どうしの折り返しだけを詰め、片側でも ASCII なら空白1つを入れる。
        # 端の ** はあとで外すので、その内側の字で決める（「**重要**」で折り返しても本文を変えない）
        if raw:
            stars = len(raw) - len(raw.rstrip("*"))
            last = raw[:len(raw) - stars + stars % 2][-1:] or raw[-1]
            stars = len(piece) - len(piece.lstrip("*"))
            first = piece[stars - stars % 2:][:1] or piece[0]
            if ord(last) < 128 or ord(first) < 128:
                raw += " "
        starts.append((len(raw), line_no))
        raw += piece
    return raw, starts


def line_at(pos, positions, starts):
    """本文の位置 pos が、つなぐ前のどの行にあったかを返す。positions は starts の始まりの位置の列。"""
    k = bisect.bisect_right(positions, pos) - 1
    return starts[max(k, 0)][1]


def split_sentences(raw):
    """段落の本文を「。」の直後で切り、[(始まりの位置, 文)] を返す。backtick と括弧の中では切らない。"""
    spans, ok = code_spans(raw)
    guarded = [False] * len(raw)
    if ok:
        for start, end in spans:
            for k in range(start, end):
                guarded[k] = True
        stack = []
        for k, ch in enumerate(raw):
            if guarded[k]:
                continue
            if ch in OPEN_BRACKETS:
                stack.append(ch)
                guarded[k] = True
            elif ch in CLOSE_BRACKETS:
                if not stack or stack[-1] != CLOSE_BRACKETS[ch]:
                    ok = False
                    break
                stack.pop()
            elif stack:
                guarded[k] = True
        if stack:
            ok = False
    if not ok:
        # 閉じ忘れで段落が丸ごと1単位になるのを防ぐため、守りを全部外す
        guarded = [False] * len(raw)
    pieces = []
    start = 0
    for k, ch in enumerate(raw):
        if ch == "。" and not guarded[k]:
            pieces.append((start, raw[start:k + 1]))
            start = k + 1
    if start < len(raw):
        pieces.append((start, raw[start:]))
    return pieces


def table_cells(row):
    """表の行をセルに分け、` | ` でつないだ本文と、区切り行かどうかを返す。"""
    row = row.strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|") and not row.endswith("\\|"):
        row = row[:-1]
    cells = []
    cell = []
    i = 0
    while i < len(row):
        if row.startswith("\\|", i):
            cell.append("|")
            i += 2
            continue
        if row[i] == "|":
            cells.append("".join(cell).strip())
            cell = []
        else:
            cell.append(row[i])
        i += 1
    cells.append("".join(cell).strip())
    separator = all(TABLE_SEPARATOR.fullmatch(c) for c in cells)
    return " | ".join(cells), separator


def split_units(text):
    """プランの本文を単位の列（dict の list）に分ける。規則は docs/plans/pace_design.md の4節。"""
    lines = LINE_END.split(text)
    if lines and lines[-1] == "":
        lines.pop()

    units = []
    headings = []  # 引用の外の見出しの列。[(段, 本文)]
    sec = ""
    fence = None  # {"char", "width", "depth"}
    block = None  # {"kind", "depth", "pieces"}
    table_rows = 0  # いまの表で、ここまでに見た行の数（表の行が途切れたら 0 に戻す）
    in_table = False

    def path():
        return [t for _, t in headings]

    def emit(kind, line_no, text_):
        if text_:
            units.append({"kind": kind, "line": line_no, "path": path(), "sec": sec, "text": text_})

    def close_block():
        nonlocal block
        if block is None:
            return
        raw, starts = join_pieces(block["pieces"])
        if raw:
            if block["kind"] == "bullet":
                # line は記号の行（記号だけの行のあとに本文が続いても、記号の行を指す）
                emit("bullet", block["pieces"][0][0], normalize(raw))
            else:
                positions = [start for start, _ in starts]
                for pos, sentence in split_sentences(raw):
                    lead = len(sentence) - len(sentence.lstrip())
                    emit("sentence", line_at(pos + lead, positions, starts), normalize(sentence))
        block = None

    for line_no, line in enumerate(lines, start=1):
        if not in_table:
            table_rows = 0
        in_table = False
        if fence is not None:
            _, rest = strip_quote(line, fence["depth"])
            mark = rest.strip()
            if mark and re.fullmatch(re.escape(fence["char"]) + "{%d,}" % fence["width"], mark):
                fence = None
                continue
            code = rest.rstrip()
            if code.strip():
                emit("code_line", line_no, code)
            continue

        depth, rest = strip_quote(line)
        if not rest.strip():
            close_block()
            continue

        m = FENCE_OPEN.fullmatch(rest)
        if m and not (m.group(1)[0] == "`" and "`" in m.group(2)):
            close_block()
            fence = {"char": m.group(1)[0], "width": len(m.group(1)), "depth": depth}
            continue

        m = HEADING.fullmatch(rest)
        if m:
            close_block()
            level = len(m.group(1))
            title = m.group(2) or ""
            title = strip_closing_hashes(title)
            title = normalize(title)
            if not title:
                # 本文の無い見出しは、単位にも文書の構造にもしない
                continue
            if depth > 0:
                # 引用の中の見出しは、文書の構造（path・sec）を進めない
                emit("heading", line_no, title)
                continue
            while headings and headings[-1][0] >= level:
                headings.pop()
            if level == 1:
                sec = ""
            elif level == 2:
                sec = title
            emit("heading", line_no, title)
            headings.append((level, title))
            continue

        if RULE.fullmatch(rest):
            close_block()
            continue

        if TABLE.match(rest):
            close_block()
            row, separator = table_cells(rest)
            # 区切り行として捨てるのは、表の2行目だけ（「- 」で該当なしを書いたデータの行は残す）
            if not (separator and table_rows == 1):
                emit("table_row", line_no, normalize(row))
            table_rows += 1
            in_table = True
            continue

        m = BULLET.fullmatch(rest)
        if m and FENCE_OPEN.fullmatch(m.group(1) or ""):
            f = FENCE_OPEN.fullmatch(m.group(1))
            if not (f.group(1)[0] == "`" and "`" in f.group(2)):
                # 箇条書きの記号と同じ行で開いたフェンス（`- ```go`）。開きを見落とすと、
                # 字下げした閉じの行が開きになって、内と外が文書の終わりまで逆になる
                close_block()
                fence = {"char": f.group(1)[0], "width": len(f.group(1)), "depth": depth}
                continue
        if m:
            close_block()
            block = {"kind": "bullet", "depth": depth, "pieces": [(line_no, m.group(1) or "")]}
            continue

        if block is not None and block["depth"] == depth:
            block["pieces"].append((line_no, rest))
        else:
            close_block()
            block = {"kind": "sentence", "depth": depth, "pieces": [(line_no, rest)]}

    close_block()

    seen = {}
    result = []
    for pos, unit in enumerate(units):
        key = "\n".join(unit["path"]) + "\n" + unit["text"]
        base = "u" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]
        seen[base] = seen.get(base, 0) + 1
        uid = base if seen[base] == 1 else "%s_%d" % (base, seen[base])
        result.append({"id": uid, "pos": pos, "kind": unit["kind"], "line": unit["line"],
                       "path": unit["path"], "sec": unit["sec"], "text": unit["text"]})
    return result


# ---------------------------------------------------------------------------
# 作業ディレクトリ
# ---------------------------------------------------------------------------

def read_plan(plan):
    """プランを読み、(バイト, 本文) を返す。読めない（無い・UTF-8 でない）ときは None。"""
    try:
        if not stat.S_ISREG(plan.stat().st_mode):
            # ディレクトリ・FIFO・機器のファイルは「ファイルとして無い」と数える（FIFO を読むと戻らない）
            return None
        data = plan.read_bytes()
    except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
        return None
    except OSError as e:
        if e.errno == errno.ELOOP:
            # 輪になった symlink は、行き先の無い symlink と同じく「無い」と数える
            return None
        raise PaceError("プランを読めませんでした（%s）: %s" % (e.strerror or e, plan))
    try:
        return data, data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None


def workdir_for(plan):
    """プランの作業ディレクトリ（.pace/<拡張子を除いた名前>/）と .pace/ を返す。

    名前が `.`・`..`・`.gitignore` になるプラン（`..md`・`...md`・`.gitignore.md`）は、
    作業ディレクトリが .pace/ そのものやプランのディレクトリになり、作り直しで外を消すので断る。
    """
    pace_dir = plan.parent / ".pace"
    name = plan.stem
    if name in ("", ".", "..", ".gitignore"):
        raise PaceError("プランの名前 %s からは作業ディレクトリの名前を作れないので、何も書きません。プランの名前を変えてください" % plan.name)
    return pace_dir, pace_dir / name


def check_dir(path):
    if path.is_symlink():
        raise PaceError("%s が symlink なので、何も書きません" % path)
    if path.exists() and not path.is_dir():
        raise PaceError("%s がディレクトリではないので、何も書きません" % path)


def check_owner(plan, work):
    """作業ディレクトリが別のプランのものなら PaceError。source.json が無ければ、いまのプランのものとする。"""
    source = work / "source.json"
    if not source.exists() and not source.is_symlink():
        return
    try:
        if source.is_symlink() or not stat.S_ISREG(source.stat().st_mode):
            # FIFO を読むと戻らない。symlink は作業ディレクトリの外を読みうる
            raise ValueError("ふつうのファイルではありません")
        recorded = json.loads(source.read_text(encoding="utf-8")).get("plan_file")
    except (OSError, ValueError, AttributeError, RecursionError) as e:
        raise PaceError("%s を読めないので、何も書きません（%s）" % (source, e))
    if not isinstance(recorded, str) or recorded in ("", ".", "..") or os.path.basename(recorded) != recorded:
        raise PaceError("%s の plan_file がファイル名ではないので、何も書きません" % source)
    if recorded == plan.name:
        return
    other = plan.parent / recorded
    try:
        if other.exists() and os.path.samefile(other, plan):
            return
    except OSError:
        pass
    raise PaceError(
        "%s は %s の作業ディレクトリです。拡張子だけが違うか、拡張子だけを付け替えたプランは、同じ作業ディレクトリを使えません。"
        "%s を回すなら、%s の名前を変えるか、%s を消してから打ち直してください（消すと、%s の見せた記録も消えます）"
        % (work, recorded, plan.name, plan.name, work, recorded))


def current_umask():
    mask = os.umask(0)
    os.umask(mask)
    return mask


def write_atomic(path, data):
    """同じディレクトリの一時ファイルへ書き切ってから差し替える。既にあるファイルの権限は写す。"""
    try:
        mode = path.stat().st_mode & 0o7777 if not path.is_symlink() else None
    except FileNotFoundError:
        mode = None
    if mode is None:
        mode = 0o666 & ~current_umask()
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix="." + path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def clear_workdir(work):
    """作業ディレクトリの直下のうち、残すもの以外を消す。symlink は先を辿らずにリンクだけを消す。"""
    with os.scandir(str(work)) as it:
        entries = list(it)
    for entry in entries:
        if entry.name in KEEP:
            continue
        if entry.is_dir(follow_symlinks=False):
            shutil.rmtree(entry.path)
        else:
            os.unlink(entry.path)


def init(plan_arg):
    plan = Path(plan_arg)
    read = read_plan(plan)
    if read is None:
        print(UNREADABLE_STATUS)
        print("NEXT: show")
        return 0
    data, text = read
    pace_dir, work = workdir_for(plan)
    check_dir(pace_dir)
    check_dir(work)
    check_owner(plan, work)
    try:
        source = (json.dumps({"plan_file": plan.name}, ensure_ascii=False) + "\n").encode("utf-8")
    except UnicodeEncodeError:
        raise PaceError("プランの名前が UTF-8 で表せないので、何も書きません。プランの名前を変えてください")
    units = split_units(text)
    try:
        pace_dir.mkdir(exist_ok=True)
        write_atomic(pace_dir / ".gitignore", b"*\n")
        work.mkdir(exist_ok=True)
        write_atomic(work / "source.json", source)
        clear_workdir(work)
        write_atomic(work / "plan.md", data)
        lines = "".join(json.dumps(u, ensure_ascii=False) + "\n" for u in units)
        write_atomic(work / "units.jsonl", lines.encode("utf-8"))
        if not (work / "shown.md").exists():
            write_atomic(work / "shown.md", data)
    except OSError as e:
        raise PaceError("作業ディレクトリに書けませんでした（%s）: %s" % (e.strerror or e, e.filename or work))
    print("units=%d" % len(units))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="pace.py", description="PACE の機械の仕事を受け持つ")
    parser.add_argument("--version", action="store_true", help="段ごとの版の一覧を出す")
    sub = parser.add_subparsers(dest="command")
    p_init = sub.add_parser("init", help="プランを単位に分けて作業ディレクトリに置く")
    p_init.add_argument("plan", help="プランのパス")
    args = parser.parse_args(argv)
    if args.version:
        for name, version in STAGE_VERSIONS:
            print(f"{name} {version}")
        return 0
    if args.command == "init":
        try:
            return init(args.plan)
        except PaceError as e:
            print("pace.py init: %s" % e, file=sys.stderr)
            return 1
    parser.print_help(sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
