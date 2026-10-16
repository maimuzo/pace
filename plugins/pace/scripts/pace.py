#!/usr/bin/env python3
"""PACE の機械の仕事を受け持つスクリプト。

いまは --version（段ごとの版の一覧）と init（プランを単位に分けて作業ディレクトリに置く）と
next（AI に頼むジョブを束にして出し、答えを検証して取り込む。分類・比較・裁定・ノード化まで）を持つ。
verify・report・show・shown は後の issue（maimuzo/pace#5 以降）で足す。
Python 3.9 以上・標準ライブラリだけで動かす。分割と作業ディレクトリの決まりは
docs/plans/pace_design.md の4節（入力）と9節（作業ディレクトリ）、next の決まりは5節（パイプライン）にある。
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

# .pace/ の中は、init を打った利用者だけが読み書きする（プランの中身を他の利用者に読ませない）。
# 新しく作るファイルは 0600、ディレクトリは 0700 に、umask に依らず揃える（持ち主のビットを消す umask で
# 作っても、壊れた権限のディレクトリを残さない）
PRIVATE_FILE = 0o600
PRIVATE_DIR = 0o700


class PaceError(Exception):
    """errors に数えない失敗（読む権限が無い・書けない・作業ディレクトリが使えない）。標準エラーに理由を出して 1 で終わる。

    errors に数えるのは、init がプランを読めない（無い・UTF-8 でない）ときだけ（docs/plans/pace_design.md の5節）。
    next はプランを読めないときも、errors に数えずにこれで 1 で終わる。
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
    pos = 0
    # 段ごとに行の残りを写すと、深く重ねた行で2乗になるので、位置だけを進めて最後に1回切る
    while limit is None or depth < limit:
        m = QUOTE_MARK.match(line, pos)
        if not m:
            break
        pos = m.end()
        depth += 1
    return depth, line[pos:]


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
    # 幅ごとに並びの番号を持ち、次の同じ幅の並びを先頭から引く（後ろを毎回なめない）
    by_width = {}
    for k, (start, end) in enumerate(runs):
        by_width.setdefault(end - start, []).append(k)
    heads = {w: 0 for w in by_width}
    spans = []
    ok = True
    i = 0
    while i < len(runs):
        start, end = runs[i]
        width = end - start
        same = by_width[width]
        h = heads[width]
        while h < len(same) and same[h] <= i:
            h += 1
        heads[width] = h
        if h < len(same):
            j = same[h]
            spans.append((start, runs[j][1]))
            i = j + 1
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
    parts = []
    length = 0
    last = ""  # つないだ本文の、端の ** を見ない最後の字
    starts = []
    for line_no, piece in pieces:
        piece = piece.strip()
        if not piece:
            continue
        # 日本語どうしの折り返しだけを詰め、片側でも ASCII なら空白1つを入れる。
        # 端の ** はあとで外すので、その内側の字で決める（「**重要**」で折り返しても本文を変えない）。
        # 本文を写さずに piece の端だけを見るので、行が多くても2乗にならない
        lead = len(piece) - len(piece.lstrip("*"))
        first = piece[lead - lead % 2] if lead - lead % 2 < len(piece) else piece[0]
        spaced = bool(parts) and (ord(last) < 128 or ord(first) < 128)
        if spaced:
            parts.append(" ")
            length += 1
        starts.append((length, line_no))
        parts.append(piece)
        length += len(piece)
        trail = len(piece) - len(piece.rstrip("*"))
        cut = len(piece) - trail + trail % 2
        if cut > 0:
            last = piece[cut - 1]
        elif spaced:
            # ** だけの行。端の ** を見ないと、最後の字はその前に入れた空白になる
            last = " "
        else:
            last = last or piece[-1]
    return "".join(parts), starts


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

    名前が `.`・`..`・`.gitignore`（大文字と小文字を区別しない）になるプラン（`..md`・`...md`・`.gitignore.md`）は、
    作業ディレクトリが .pace/ そのものやプランのディレクトリになり、作り直しで外を消すので断る。
    """
    pace_dir = plan.parent / ".pace"
    name = plan.stem
    # 大文字と小文字を区別しない FS では .GITIGNORE も .pace/.gitignore とぶつかるので、小文字で比べる
    if name in ("", ".", "..") or name.lower() == ".gitignore":
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


def write_atomic(path, data, new_mode):
    """同じディレクトリの一時ファイルへ書き切ってから差し替える。

    既にあるファイルの権限は写す。新しく作るときは new_mode にする（umask は当てない）。
    失敗は、一時ファイルでなく書こうとした path を filename に持つ OSError にして投げる。
    """
    try:
        mode = path.stat().st_mode & 0o7777 if not path.is_symlink() else None
    except FileNotFoundError:
        mode = None
    except OSError as e:
        raise OSError(e.errno, e.strerror, str(path))
    if mode is None:
        mode = new_mode
    try:
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix="." + path.name + ".", suffix=".tmp")
    except OSError as e:
        raise OSError(e.errno, e.strerror, str(path))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
            # パスでなく開いたファイルに付ける（パスを差し替えられても、ほかのファイルの権限を変えない）
            os.fchmod(f.fileno(), mode)
        os.replace(tmp, str(path))
    except BaseException as e:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        if isinstance(e, OSError):
            raise OSError(e.errno, e.strerror, str(path))
        raise


def make_private_dir(path):
    """ディレクトリを作り、新しく作ったときだけ umask に依らず 0700 にする。既にあるものは変えない。"""
    try:
        os.mkdir(str(path), PRIVATE_DIR)
    except FileExistsError:
        return
    os.chmod(str(path), PRIVATE_DIR)


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
    try:
        check_dir(pace_dir)
        check_dir(work)
        check_owner(plan, work)
    except OSError as e:
        raise PaceError("作業ディレクトリを確かめられませんでした（%s）: %s" % (e.strerror or e, e.filename or work))
    try:
        source = (json.dumps({"plan_file": plan.name}, ensure_ascii=False) + "\n").encode("utf-8")
    except UnicodeEncodeError:
        raise PaceError("プランの名前が UTF-8 で表せないので、何も書きません。プランの名前を変えてください")
    units = split_units(text)
    try:
        make_private_dir(pace_dir)
        write_atomic(pace_dir / ".gitignore", b"*\n", PRIVATE_FILE)
        make_private_dir(work)
        write_atomic(work / "source.json", source, PRIVATE_FILE)
        clear_workdir(work)
        write_atomic(work / "plan.md", data, PRIVATE_FILE)
        lines = "".join(json.dumps(u, ensure_ascii=False) + "\n" for u in units)
        write_atomic(work / "units.jsonl", lines.encode("utf-8"), PRIVATE_FILE)
        shown = work / "shown.md"
        # shown.md が symlink なら、何を指していても触らない（作業ディレクトリの外を書き換えない）
        if not os.path.lexists(str(shown)):
            write_atomic(shown, data, PRIVATE_FILE)
    except OSError as e:
        raise PaceError("作業ディレクトリに書けませんでした（%s）: %s" % (e.strerror or e, e.filename or work))
    print("units=%d" % len(units))
    return 0


# ---------------------------------------------------------------------------
# next（ジョブ・束・検証・キャッシュ）
# ---------------------------------------------------------------------------

# 差し戻しの上限。attempts（検証に落ちた回数）が3のときにまた落ちたら、失敗したジョブにする。
# docs/idea.md 付録B の MAX_ATTEMPTS（差し戻しの上限）と同じ値で、名前の取り違えを避けて RETRIES と呼ぶ
MAX_RETRIES = 3
CLASSIFY_BATCH = 30  # 分類の1ジョブの単位の数の上限（docs/idea.md 段2）
NEIGHBORS = 2  # 分類と裁定に付ける前後の単位の数（キャッシュの鍵には入れない）
# 束とジョブの大きさ。ジョブファイルの字数（指示文を含む）で数える。推論で決めた仮の値で、#9 で測り直す
JOB_MAX_CHARS = 20000
BUNDLE_MAX_CHARS = 60000
BUNDLE_MAX_JOBS = 20
PARALLEL = 4  # 1つの波に出す束の数の上限

AGENTS_DIR = Path(__file__).resolve().parent.parent / "agents"
STAGE_AGENT = {"classify": "pace-worker", "adjudicate": "pace-judge"}
JOB_STAGES = ("classify", "adjudicate")
PTYPES = ("fact", "requirement", "value")

# 段の指示文。docs/idea.md 付録A の全文に、code_line（コードの1行）を足したもの。
# 変えるとキャッシュの鍵（指示文の sha256）が変わるので、古い答えは使われない。出力の形を変えたら段の版も上げる
INSTRUCTIONS = {
    "classify": """あなたは設計プランの文を分類する。input.units の各単位（文・箇条書き・表の1行・見出し・コードの1行）に、次のラベルを付けよ。単位の種類は kind にあり、code_line はコードブロックの中の1行である。
- decision: プランが何かを選んでいて、要求を満たす別の選び方があり、選び方で振る舞いや作り方が変わるもの。「何を作る/作らない」「どう作る」「どの順」「何を例外にする」「何を出す/出さない」「数値・既定値」を全部含む。1単位に複数あれば分ける。text は一文で言い直した決定、alternatives は現実的な別案を1つ以上。
- premise: 決定ではない前提。ptype は fact（現状の事実）/ requirement（満たすべき要求・目標）/ value（何を重視するか）。text は一文で言い直した前提。
- 両方を含む単位には両方を付ける。どちらでもない単位は labels を空にする。迷ったら拾う側に倒す。
各単位は独立に判断してよいが、section_path と neighbors で文脈を確かめること。
出力は JSON: {"units":[{"uid":"...","labels":[{"kind":"decision","text":"...","alternatives":["..."]},{"kind":"premise","ptype":"fact","text":"..."}]}]}
input.units のすべての uid を、過不足なく1回ずつ出すこと。""",
    "adjudicate": """設計プランの1つの単位（input.unit。kind が code_line ならコードブロックの中の1行）について、2人の分類者の結果（input.A と input.B）が食い違った。基準に照らして正しいラベルを決めよ。
基準: decision = プランが選んでいて、別の選び方があり、選び方で振る舞いや作り方が変わるもの（1単位に複数あれば分ける）。premise = 決定ではない前提で、ptype は fact / requirement / value。どちらでもなければ空。迷ったら拾う側に倒す。
section_path と neighbors で文脈を確かめること。
出力は JSON: {"labels":[{"kind":"decision","text":"...","alternatives":["..."]} | {"kind":"premise","ptype":"fact","text":"..."}]}""",
}


def dumps(obj):
    """作業ディレクトリに書く JSON。日本語をそのまま書き、subagent が Read で読みやすいよう字下げする。"""
    return json.dumps(obj, ensure_ascii=False, indent=1) + "\n"


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_regular(path):
    """ふつうのファイルだけを読む。symlink・FIFO・ディレクトリは ValueError（FIFO を読むと戻らない）。"""
    st = os.lstat(str(path))
    if not stat.S_ISREG(st.st_mode):
        raise ValueError("ふつうのファイルではありません")
    with open(str(path), "rb") as f:
        return f.read()


def load_work_json(path):
    """作業ディレクトリの中の、pace.py が書いた JSON を読む。壊れていたら PaceError（init を打ち直す案内）。"""
    try:
        return json.loads(read_regular(path).decode("utf-8"))
    except (OSError, ValueError, RecursionError) as e:
        raise PaceError("%s を読めません（%s）。init を打ち直してください" % (path, e))


def write_json(path, obj):
    try:
        data = dumps(obj).encode("utf-8")
    except UnicodeEncodeError:
        raise PaceError("作業ディレクトリのパスが UTF-8 で表せないので、ジョブを書けません: %s" % path.parent)
    write_atomic(path, data, PRIVATE_FILE)


def check_label(label, where):
    """ラベル1つを確かめ、知らないキーを捨てたものを返す。誤りは ValueError。"""
    if not isinstance(label, dict):
        raise ValueError("%s のラベルがオブジェクトではありません" % where)
    kind = label.get("kind")
    text = label.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("%s のラベルの text が空でない文字列ではありません" % where)
    if kind == "decision":
        alts = label.get("alternatives")
        if not isinstance(alts, list) or not alts or not all(isinstance(a, str) and a.strip() for a in alts):
            raise ValueError("%s の decision の alternatives が、空でない文字列1つ以上の配列ではありません" % where)
        return {"kind": "decision", "text": text, "alternatives": list(alts)}
    if kind == "premise":
        if label.get("ptype") not in PTYPES:
            raise ValueError("%s の premise の ptype が fact / requirement / value のどれでもありません" % where)
        return {"kind": "premise", "ptype": label["ptype"], "text": text}
    raise ValueError("%s のラベルの kind が decision か premise ではありません" % where)


def check_labels(labels, where):
    if not isinstance(labels, list):
        raise ValueError("%s の labels が配列ではありません" % where)
    return [check_label(lab, where) for lab in labels]


def check_classify(answer, job):
    """分類の答えを確かめ、{uid: labels} を返す。"""
    items = answer.get("units")
    if not isinstance(items, list):
        raise ValueError('"units" が配列ではありません')
    want = [u["uid"] for u in job["input"]["units"]]
    got = {}
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("uid"), str):
            raise ValueError('"units" の要素に uid がありません')
        uid = item["uid"]
        if uid not in want:
            raise ValueError("入力に無い uid があります: %s" % uid)
        if uid in got:
            raise ValueError("uid が2回あります: %s" % uid)
        got[uid] = check_labels(item.get("labels"), uid)
    missing = [u for u in want if u not in got]
    if missing:
        raise ValueError("答えの無い uid があります: %s" % ", ".join(missing))
    return got


def check_adjudicate(answer, job):
    return check_labels(answer.get("labels"), job["input"]["unit"]["uid"])


def counts(labels):
    return (sum(1 for x in labels if x["kind"] == "decision"), sum(1 for x in labels if x["kind"] == "premise"))


class Round:
    """1回の next が扱う、1つのプランの作業ディレクトリ。"""

    def __init__(self, plan_arg):
        self.plan = Path(plan_arg)
        read = read_plan(self.plan)
        if read is None:
            raise PaceError("プランを読めません（無いか、UTF-8 ではありません）: %s。init を打ってください" % self.plan)
        data = read[0]
        pace_dir, self.work = workdir_for(self.plan)
        self.jobs_dir = self.work / "jobs"
        self.cache_dir = self.work / "cache"
        try:
            check_dir(pace_dir)
            check_dir(self.work)
            check_owner(self.plan, self.work)
            check_dir(self.jobs_dir)
            check_dir(self.cache_dir)
        except OSError as e:
            raise PaceError("作業ディレクトリを確かめられませんでした（%s）: %s" % (e.strerror or e, e.filename or self.work))
        for name in ("units.jsonl", "plan.md"):
            if not os.path.lexists(str(self.work / name)):
                raise PaceError("%s がありません。init を打ってください" % (self.work / name))
        try:
            copy = read_regular(self.work / "plan.md")
        except (OSError, ValueError) as e:
            raise PaceError("%s を読めません（%s）。init を打ち直してください" % (self.work / "plan.md", e))
        if copy != data:
            raise PaceError("プランが init のあとで変わりました。init を打ち直してください: %s" % self.plan)
        try:
            lines = read_regular(self.work / "units.jsonl").decode("utf-8").splitlines()
            self.units = [json.loads(line) for line in lines if line]
        except (OSError, ValueError, RecursionError) as e:
            raise PaceError("%s を読めません（%s）。init を打ち直してください" % (self.work / "units.jsonl", e))
        # 前後の文は文書の中の位置で引く（節をまたいでよい。docs/idea.md 段2 の neighbors）
        self.pos = {u["id"]: i for i, u in enumerate(self.units)}
        source = self.work / "source.json"
        self.plan_file = load_work_json(source)["plan_file"] if os.path.lexists(str(source)) else self.plan.name
        self.agent_sha = {}

    # --- 鍵とキャッシュ ---------------------------------------------------

    def agent_digest(self, stage):
        """段の agent の定義ファイルの sha256。定義（frontmatter の model を含む）を変えると鍵が変わる。"""
        name = STAGE_AGENT[stage]
        if name not in self.agent_sha:
            try:
                self.agent_sha[name] = hashlib.sha256(read_regular(AGENTS_DIR / (name + ".md"))).hexdigest()
            except (OSError, ValueError) as e:
                raise PaceError("agent の定義を読めません（%s）: %s" % (e, AGENTS_DIR / (name + ".md")))
        return self.agent_sha[name]

    def cache_key(self, stage, key_input):
        version = dict(STAGE_VERSIONS)[stage]
        material = [stage, version, sha256_text(INSTRUCTIONS[stage]), self.agent_digest(stage), key_input]
        return sha256_text(json.dumps(material, ensure_ascii=False, sort_keys=True))

    def classify_key(self, variant, unit):
        # 前後の文は鍵に入れない（docs/idea.md 6.6。入れると1行の修正が周りへ揺れる）
        return self.cache_key("classify", {"variant": variant, "plan_file": self.plan_file, "sec": unit["sec"],
                                           "uid": unit["id"], "kind": unit["kind"], "section_path": unit["path"],
                                           "text": unit["text"]})

    def adjudicate_key(self, unit, a, b):
        return self.cache_key("adjudicate", {"kind": unit["kind"], "section_path": unit["path"],
                                             "text": unit["text"], "A": a, "B": b})

    def cache_get(self, key):
        path = self.cache_dir / (key + ".json")
        if not os.path.lexists(str(path)):
            return None
        return load_work_json(path)["labels"]

    def cache_put(self, key, labels):
        make_private_dir(self.cache_dir)
        write_json(self.cache_dir / (key + ".json"), {"labels": labels})

    # --- ジョブ -----------------------------------------------------------

    def round_id(self):
        """周の印。init のあと最初の next が決め、ジョブの名前に入れる（前の起動の遅れた答えを見ないため）。"""
        path = self.jobs_dir / "round.json"
        if os.path.lexists(str(path)):
            return load_work_json(path)["round"]
        # 印が無いのに段のディレクトリがあれば、どの周のものか分からないので消す
        if self.jobs_dir.exists():
            clear_workdir(self.jobs_dir)
        make_private_dir(self.jobs_dir)
        rid = os.urandom(4).hex()
        write_json(path, {"round": rid})
        return rid

    def stage_dir(self, stage):
        return self.jobs_dir / stage

    def created(self, stage, rid):
        """段のジョブを作り終えていれば、ID の列を返す。作りかけ・前の周のものは消して None。"""
        d = self.stage_dir(stage)
        try:
            check_dir(d)
        except OSError as e:
            raise PaceError("作業ディレクトリを確かめられませんでした（%s）: %s" % (e.strerror or e, d))
        mark = d / "created.json"
        if os.path.lexists(str(mark)):
            info = load_work_json(mark)
            if info.get("round") == rid:
                return info["jobs"]
        if d.exists():
            shutil.rmtree(str(d))
        return None

    def job_path(self, stage, job_id):
        return self.stage_dir(stage) / (job_id + ".json")

    def load_job(self, stage, job_id):
        return load_work_json(self.job_path(stage, job_id))

    def new_job(self, stage, job_id, job_input, cache_key=""):
        d = self.stage_dir(stage)
        return {"job_id": job_id, "stage": stage, "cache_key": cache_key,
                "prompt_version": dict(STAGE_VERSIONS)[stage], "instructions": INSTRUCTIONS[stage],
                "input": job_input, "out_path": str((d / (job_id + ".out.json")).absolute()),
                "attempts": 0, "previous_errors": [], "status": "pending"}

    def write_stage(self, stage, rid, jobs):
        """段のジョブを全部書き、最後に created.json を書く（これが作り終えた印）。"""
        d = self.stage_dir(stage)
        make_private_dir(self.jobs_dir)
        make_private_dir(d)
        for job in jobs:
            write_json(d / (job["job_id"] + ".json"), job)
        ids = [job["job_id"] for job in jobs]
        write_json(d / "created.json", {"round": rid, "count": len(ids), "jobs": ids})
        return ids

    def unit_entry(self, unit):
        i = self.pos[unit["id"]]
        near = self.units[max(0, i - NEIGHBORS):i] + self.units[i + 1:i + 1 + NEIGHBORS]
        return {"uid": unit["id"], "kind": unit["kind"], "section_path": unit["path"], "text": unit["text"],
                "neighbors": [u["text"] for u in near]}

    def make_classify(self, rid):
        """cache に無い単位だけで、節ごとに30単位・20,000字までの分類のジョブを A と B で作る。"""
        sections = []
        for unit in self.units:
            if sections and sections[-1][0] == unit["sec"]:
                sections[-1][1].append(unit)
            else:
                sections.append((unit["sec"], [unit]))
        jobs = []
        for variant in ("A", "B"):
            seq = 0
            groups = sections if variant == "A" else [(s, list(reversed(us))) for s, us in reversed(sections)]
            for _, group in groups:
                chunk = []
                for unit in group:
                    key = self.classify_key(variant, unit)
                    if self.cache_get(key) is not None:
                        continue
                    entry = dict(self.unit_entry(unit), cache_key=key)
                    if chunk:
                        trial = self.new_job("classify", "x", {"units": chunk + [entry]})
                        if len(chunk) >= CLASSIFY_BATCH or len(dumps(trial)) > JOB_MAX_CHARS:
                            seq += 1
                            jobs.append(self.new_job("classify", "classify_%s_%03d_%s" % (variant, seq, rid),
                                                     {"units": chunk}))
                            chunk = []
                    chunk.append(entry)
                if chunk:
                    seq += 1
                    jobs.append(self.new_job("classify", "classify_%s_%03d_%s" % (variant, seq, rid),
                                             {"units": chunk}))
        for job in jobs:
            job["variant"] = job["job_id"].split("_")[1]
        return jobs

    def failed_units(self):
        out = set()
        for rec in self.failed_records():
            out.update(rec.get("units", []))
        return out

    def failed_records(self):
        path = self.work / "failed_jobs.jsonl"
        if not os.path.lexists(str(path)):
            return []
        try:
            return [json.loads(line) for line in read_regular(path).decode("utf-8").splitlines() if line]
        except (OSError, ValueError, RecursionError) as e:
            raise PaceError("%s を読めません（%s）。init を打ち直してください" % (path, e))

    def classified(self, unit):
        return self.cache_get(self.classify_key("A", unit)), self.cache_get(self.classify_key("B", unit))

    def make_adjudicate(self, rid):
        """A と B の決定の数・前提の数が食い違った単位だけ、裁定のジョブを作る（docs/idea.md 段2b）。"""
        failed = self.failed_units()
        jobs = []
        for unit in self.units:
            if unit["id"] in failed:
                continue
            a, b = self.classified(unit)
            if a is None or b is None or counts(a) == counts(b):
                continue
            key = self.adjudicate_key(unit, a, b)
            if self.cache_get(key) is not None:
                continue
            job_input = {"unit": self.unit_entry(unit), "A": a, "B": b}
            jobs.append(self.new_job("adjudicate", "adjudicate_%s_%s" % (unit["id"], rid), job_input, key))
        return jobs

    # --- 判定 -------------------------------------------------------------

    def judge(self, job):
        """issued のジョブの答えを判定し、ジョブを done・pending（差し戻し）・failed に書き換える。"""
        stage = job["stage"]
        out = Path(job["out_path"])
        error = None
        answer = None
        if not os.path.lexists(str(out)):
            error = "out_path に答えが書かれていませんでした"
        else:
            try:
                answer = json.loads(read_regular(out).decode("utf-8"))
                if not isinstance(answer, dict):
                    raise ValueError("答えが JSON のオブジェクトではありません")
                result = check_classify(answer, job) if stage == "classify" else check_adjudicate(answer, job)
            except (OSError, ValueError, RecursionError) as e:
                error = "答えが検証に落ちました: %s" % e
        path = self.job_path(stage, job["job_id"])
        if error is None:
            # キャッシュに書いてから done にする（間で落ちても、次の next が同じ答えを検証し直すだけ）
            if stage == "classify":
                keys = {u["uid"]: u["cache_key"] for u in job["input"]["units"]}
                for uid, labels in result.items():
                    self.cache_put(keys[uid], labels)
            else:
                self.cache_put(job["cache_key"], result)
            job["status"] = "done"
            write_json(path, job)
            return
        n = job["attempts"] + 1
        if os.path.lexists(str(out)):
            # 古い答えの名前を変えてから attempts を増やす（同じ名前で残すと、次の next が同じ答えを検証し直す）
            os.replace(str(out), str(out.parent / ("%s.rejected-%d.json" % (job["job_id"], n))))
        job["attempts"] = n
        job["previous_errors"].append(error)
        if n > MAX_RETRIES:
            # 記録を先に書いてから failed にする。記録は job_id で重ねない
            if job["job_id"] not in {r.get("job_id") for r in self.failed_records()}:
                units = ([u["uid"] for u in job["input"]["units"]] if stage == "classify"
                         else [job["input"]["unit"]["uid"]])
                rec = {"job_id": job["job_id"], "stage": stage, "units": units, "errors": job["previous_errors"]}
                path_f = self.work / "failed_jobs.jsonl"
                old = read_regular(path_f) if os.path.lexists(str(path_f)) else b""
                line = (json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8")
                write_atomic(path_f, old + line, PRIVATE_FILE)
            job["status"] = "failed"
        else:
            job["status"] = "pending"
        write_json(path, job)

    # --- 波 ---------------------------------------------------------------

    def bundles(self, stage, jobs):
        """pending のジョブを束にする。A と B は別の束にし、交互に並べる。"""
        groups = {}
        for job in jobs:
            groups.setdefault(job.get("variant", ""), []).append(job)
        per_group = []
        for variant in sorted(groups):
            out = []
            cur, size = [], 0
            for job in groups[variant]:
                n = len(dumps(job))
                if cur and (size + n > BUNDLE_MAX_CHARS or len(cur) >= BUNDLE_MAX_JOBS):
                    out.append(cur)
                    cur, size = [], 0
                cur.append(job)
                size += n
            if cur:
                out.append(cur)
            per_group.append(out)
        mixed = []
        for i in range(max(len(g) for g in per_group)):
            for g in per_group:
                if i < len(g):
                    mixed.append(g[i])
        return mixed

    def emit(self, stage, pending):
        chosen = self.bundles(stage, pending)[:PARALLEL]
        wave = []
        for k, bundle in enumerate(chosen, start=1):
            for job in bundle:
                job["status"] = "issued"
                write_json(self.job_path(stage, job["job_id"]), job)
            wave.append({"bundle_id": "b%03d" % k, "agent": "pace:" + STAGE_AGENT[stage],
                         "jobs": [str(self.job_path(stage, job["job_id"]).absolute()) for job in bundle]})
        write_json(self.work / "pending.json", {"bundles": wave})
        lines = []
        for b in wave:
            lines.append("BUNDLE %s %s" % (b["bundle_id"], b["agent"]))
            lines.extend("JOB %s" % p for p in b["jobs"])
        lines.append("WAVE bundles=%d jobs=%d" % (len(wave), sum(len(b["jobs"]) for b in wave)))
        return lines

    # --- ノード化 ----------------------------------------------------------

    def nodes(self):
        failed = self.failed_units()
        result = []
        for unit in self.units:
            if unit["id"] in failed:
                continue
            a, b = self.classified(unit)
            if a is None or b is None:
                raise PaceError("単位 %s の分類の答えがありません。init を打ち直してください" % unit["id"])
            labels = a
            if counts(a) != counts(b):
                labels = self.cache_get(self.adjudicate_key(unit, a, b))
                if labels is None:
                    raise PaceError("単位 %s の裁定の答えがありません。init を打ち直してください" % unit["id"])
            stem = unit["id"][1:]
            seen = {"decision": 0, "premise": 0}
            for lab in labels:
                seen[lab["kind"]] += 1
                node = {"id": "%s_%s_%d" % ("D" if lab["kind"] == "decision" else "P", stem, seen[lab["kind"]]),
                        "kind": lab["kind"]}
                if lab["kind"] == "premise":
                    node["ptype"] = lab["ptype"]
                node["text"] = lab["text"]
                if lab["kind"] == "decision":
                    node["alternatives"] = lab["alternatives"]
                node.update({"unit": unit["id"], "line": unit["line"], "sec": unit["sec"], "path": unit["path"]})
                result.append(node)
        return result

    def run(self):
        rid = self.round_id()
        # (1) issued のジョブだけを判定する
        for stage in JOB_STAGES:
            ids = self.created(stage, rid)
            for job_id in ids or []:
                job = self.load_job(stage, job_id)
                if job.get("status") == "issued":
                    self.judge(job)
        # (2) 段を前から順に見る。ジョブが残っている段で止める
        for stage in JOB_STAGES:
            ids = self.created(stage, rid)
            if ids is None:
                jobs = self.make_classify(rid) if stage == "classify" else self.make_adjudicate(rid)
                ids = self.write_stage(stage, rid, jobs)
            pending = []
            waiting = False
            for job_id in ids:
                job = self.load_job(stage, job_id)
                if job.get("status") == "pending":
                    pending.append(job)
                elif job.get("status") == "issued":
                    waiting = True
            if pending:
                return self.emit(stage, pending)
            if waiting:
                # 出したジョブの答えを待っている（判定のあとなので、ここには来ない見込み）
                raise PaceError("答えを待っているジョブがあります。束を subagent に渡してから打ってください")
        # (3) 全部の段を通った。網羅を確かめてノード化する
        nodes = self.nodes()
        write_atomic(self.work / "nodes.jsonl",
                     "".join(json.dumps(n, ensure_ascii=False) + "\n" for n in nodes).encode("utf-8"),
                     PRIVATE_FILE)
        write_json(self.work / "pending.json", {"bundles": []})
        decisions = sum(1 for n in nodes if n["kind"] == "decision")
        return ["DONE decisions=%d premises=%d failed_jobs=%d"
                % (decisions, len(nodes) - decisions, len(self.failed_records()))]


def next_(plan_arg):
    try:
        lines = Round(plan_arg).run()
    except OSError as e:
        raise PaceError("作業ディレクトリに書けませんでした（%s）: %s" % (e.strerror or e, e.filename or plan_arg))
    try:
        sys.stdout.write("".join(line + "\n" for line in lines))
    except UnicodeEncodeError:
        raise PaceError("ジョブのパスが UTF-8 で表せないので、出力できません")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="pace.py", description="PACE の機械の仕事を受け持つ")
    parser.add_argument("--version", action="store_true", help="段ごとの版の一覧を出す")
    sub = parser.add_subparsers(dest="command")
    p_init = sub.add_parser("init", help="プランを単位に分けて作業ディレクトリに置く")
    p_init.add_argument("plan", help="プランのパス")
    p_next = sub.add_parser("next", help="前の波の答えを取り込み、次の波の束を出す")
    p_next.add_argument("plan", help="プランのパス")
    args = parser.parse_args(argv)
    if args.version:
        for name, version in STAGE_VERSIONS:
            print(f"{name} {version}")
        return 0
    commands = {"init": init, "next": next_}
    if args.command in commands:
        try:
            return commands[args.command](args.plan)
        except PaceError as e:
            print("pace.py %s: %s" % (args.command, e), file=sys.stderr)
            return 1
    parser.print_help(sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
