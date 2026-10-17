#!/usr/bin/env python3
"""PACE の機械の仕事を受け持つスクリプト。

--version（段ごとの版の一覧）と init（プランを単位に分けて作業ディレクトリに置く）と
next（AI に頼むジョブを束にして出し、答えを検証して取り込む。分類・比較・裁定・ノード化と、
候補の絞り込み・根拠付け r1・2-gram の追加候補・根拠付け r2・輪を断つ反復・反論まで）と
verify（終わった回 K まで進んだ作業ディレクトリを書き換えずに読み、結合したグラフの根への到達と gap の効きを出す。
反論の段は見ない）と report（直すものと訊くものに分けて書き、周を続けるかを出す）と
show・shown（人間に見せる show.md と写しを書き、見せた写しを差分の基準にする）を持つ。
Python 3.9 以上・標準ライブラリだけで動かす。分割と作業ディレクトリの決まりは
docs/plans/pace_design.md の4節（入力）と9節（作業ディレクトリ）、next の決まりは14節（ジョブと束）、
根拠付けの決まりは15節（根拠付け）、輪を断つ反復と結合の決まりは16節、反論の段は17節、report は18節、
show と shown は21節、周の回し方（NEXT の条件）は7節にある。
"""

import argparse
import bisect
import difflib
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
    ("link", "2026-10-17.1"),
    ("decycle", "2026-10-17.1"),
    ("review", "2026-10-17.1"),
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


def units_bytes(units):
    """units.jsonl の中身。init が書き、next が分け直した単位と突き合わせる。"""
    return "".join(json.dumps(u, ensure_ascii=False) + "\n" for u in units).encode("utf-8")


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
        write_atomic(work / "units.jsonl", units_bytes(units), PRIVATE_FILE)
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
# 根拠付けのジョブと束の大きさ。subagent の Read は1回に25,000トークンまでで、候補の形の JSON は
# 約0.57トークン/字だった（44,333字は1回で読め、53,526字は切れた。15節）。束は #9 で測り直す
LINK_JOB_MAX_CHARS = 40000
LINK_BUNDLE_MAX_CHARS = 120000
# 候補の絞り込みと追加候補の定数（docs/idea.md 付録B）
WINDOW = 5
MAX_CANDIDATES = 80
EXPAND_PER_NEED = 6
EXPAND_MAX = 12

# 輪を断つ段の回数の上限（docs/idea.md 付録B の MAX_DECYCLE_ITER）。decycle_5 で切ったあとに回6の根拠付けをして
# 終えるので、根拠付けは最大6回（16節）
MAX_DECYCLE = 5
PASSES = MAX_DECYCLE + 1

AGENTS_DIR = Path(__file__).resolve().parent.parent / "agents"
STAGE_AGENT = {"classify": "pace-worker", "adjudicate": "pace-judge", "link": "pace-worker", "decycle": "pace-judge",
               "review": "pace-judge"}
# 反論の段（17節）。回を持たない1つの段で、終わった回 K の答えを審査する。JOB_STAGES には入れない（並びと末尾
# link_r2_6 を前提にする walk・run・テストを動かさないため）。DONE はこの段のあとに出る
REVIEW_STAGE = "review"
DEFEATER_TYPES = ("logic", "missing", "fact", "value")
DEFEATER_STATUSES = ("refuted", "accepted", "needs_human")


def _job_stages():
    """段の並び。根拠付けと輪を断つ段は回ごとに別の段にし、名前に回の番号を入れる（link_r1_2 など）。"""
    out = ["classify", "adjudicate"]
    for k in range(1, PASSES + 1):
        out += ["link_r1_%d" % k, "link_r2_%d" % k]
        if k <= MAX_DECYCLE:
            out.append("decycle_%d" % k)
    return tuple(out)


JOB_STAGES = _job_stages()
# ノードを作る段（ここの失敗だけが、ノードの無い単位になる）
NODE_STAGES = ("classify", "adjudicate")
PTYPES = ("fact", "requirement", "value")
PASS_STAGE = re.compile(r"(link_r[12]|decycle)_([1-9])")


def stage_kind(stage):
    """ジョブの段（link_r1_2・decycle_1 など）を、版・指示文・agent・キャッシュの鍵の段の名前（link・decycle）に寄せる。"""
    m = PASS_STAGE.fullmatch(stage)
    if m is None:
        return stage
    return "decycle" if m.group(1) == "decycle" else "link"


def stage_pass(stage):
    """段の回の番号（分類と裁定は 0）。"""
    m = PASS_STAGE.fullmatch(stage)
    return int(m.group(2)) if m else 0


def bundle_limits(stage):
    if stage_kind(stage) == "link":
        return LINK_BUNDLE_MAX_CHARS, BUNDLE_MAX_JOBS
    return BUNDLE_MAX_CHARS, BUNDLE_MAX_JOBS

# 段の指示文。docs/idea.md 付録A の全文に、code_line（コードの1行）を足し、裁定には文脈を確かめる1文を足したもの（根拠付けと輪を断つ段は
# それぞれの上のコメント）。
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
    # docs/idea.md 付録A の [link] の全文に、gaps を必ず from に入れる規則（実験時に依頼文で足していたもの。
    # docs/idea.md:633 が指示文に入れるよう勧めている）と、入力の形の説明3文と、excluded_note の規則（実験時に依頼文で
    # 足していたもの。16節）を足したもの
    "link": """設計プランの1つの決定（input.target）について、それを支える理由を input.candidates の中から探し、推論の段（step）として書け。
規則:
1. input.candidates はプランの単位ごとのまとまりで、plan_text がプランの原文、nodes がその単位から取り出した決定・前提である。根拠の葉は nodes の id だけ。candidates に無いことを根拠にしてはならない。candidates の他の決定（kind=decision）も根拠に使ってよい。
2. 必要なのに candidates に無い前提は gaps に書く（ptype は fact / requirement / value）。でっち上げやこじつけは禁止。根拠が無いなら gap で示すのが正しい結果である。
3. step は小さく保ち、間に claims を挟んでよい。warrant に「なぜこの前提からこの結論が導けるか」を書く。
4. target.alternatives を退ける理由まで根拠付ける。退ける理由が無ければ gap にする。
5. candidates に無いが、プランの別の箇所に書かれていそうな前提や決定があれば、need に「探してほしい内容」を短く書く（無ければ空）。
claims と gaps の id は c1, c2, ... / g1, g2, ... とする。step の to は target.id か claims の id。
出力は JSON: {"claims":[{"id":"c1","text":"..."}],"gaps":[{"id":"g1","ptype":"fact","text":"..."}],"steps":[{"from":["..."],"to":"...","warrant":"..."}],"need":["..."]}
input.previous があれば、それは前回のあなたの結果である。追加された candidates を使って全体を書き直せ。
gaps に書いたものは必ずどれかの step の from に入れること。
nodes の kind は decision か、前提の種類（fact / requirement / value）である。plan_text はコードブロックの1行のこともある。
input.added は、追加された candidates のノードの id である。
input.excluded_note の決定は、輪を断つ段で target の本当の理由ではないとされた。理由に使ってはならない。""",
    # docs/idea.md 付録A の [decycle] の全文に、plan_text の説明の1文を足したもの
    "decycle": """設計プランの決定どうしが、互いを理由にし合って輪（循環論法）になっている。輪は根拠にならない。
input.nodes は輪の中の決定、input.edges は「from の決定を理由にして to の決定を根拠付けた」辺で、warrants はその理由の説明である。
各辺について、from が本当に to より先に決まっている理由（前提・制約）なのか、それとも to と並列の決定や、to から導かれる帰結にすぎないのかを判断し、本当の理由ではない辺を切れ。
切った後に輪が1つも残らないようにすること（スクリプトが検査する）。切る辺はできるだけ少なくし、それぞれ理由を書く。
出力は JSON: {"cuts":[{"from":"...","to":"...","reason":"..."}]}
nodes の plan_text はプランの原文である。""",
    # docs/idea.md 付録A の [review] の全文のうち、2文目（ほかの決定の扱い）・refuted の文・target の文・出力の形を
    # 書き換え（反証の根拠を前提に限り、by_quotes を外す。17節）、入力の形の説明4文を足したもの
    "review": """あなたは敵対的なレビュー担当である。input.target の決定は、input.steps の推論（steps の from に出る input.nodes）でプランの前提まで根拠がつながったとされている。本当に成り立つかを疑い、反論を出せ。甘い評価は不要。反論が無ければ空でよい。
target 以外の decision は別のジョブで審査されるので、反論の target にも、反証の根拠にもしない。審査するのは target に至る推論である。
観点: logic（前提から結論が本当に導けるか、別案を本当に退けているか）/ missing（プランに無い前提を暗黙に使っていないか）/ fact（前提の事実が結論を支えるのに十分か）/ value（人間が重視するもの次第で答えが変わる判断を黙って決めていないか）。
各反論は自分で処理まで行う: refuted（input.nodes の前提（kind が fact・requirement・value）で反証できる。by にその前提の id を1つ以上挙げる。反論の target の前提と、同じ答えのほかの反論が target にしている前提は挙げない）/ accepted（反論が正しい。プランの修正が要る）/ needs_human（人間の価値判断が要る）。
target には input.target.id か、input.steps の id か、input.steps の from に出る input.nodes の id（kind が decision のものを除く）を書く。
出力は JSON: {"defeaters":[{"target":"...","type":"logic","text":"...","status":"accepted","by":[],"note":"..."}]}
steps の id は s1, s2, … で、nodes の kind は decision・fact・requirement・value・claim である。
plan_text はプランの原文である。
決定そのものへの反論は、target に input.target.id を書く。
accepted と needs_human の by は空にする。""",
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


def load_work_json(path, valid=lambda obj: True):
    """作業ディレクトリの中の、pace.py が書いた JSON を読む。

    読めないか、valid が偽を返す形なら PaceError（init を打ち直す案内）。init で消えるファイルと、
    check_owner が先に確かめた source.json にだけ使う（init が残す cache/ は、壊れていたら答えが無いものとして
    作り直す。cache_get）。
    """
    try:
        obj = json.loads(read_regular(path).decode("utf-8"))
        ok = valid(obj)
    except (OSError, ValueError, RecursionError) as e:
        raise PaceError("%s を読めません（%s）。init を打ち直してください" % (path, e))
    if not ok:
        raise PaceError("%s の形が違います。init を打ち直してください" % path)
    return obj


def is_str_list(obj):
    return isinstance(obj, list) and all(isinstance(x, str) for x in obj)


def valid_job(obj):
    """ジョブファイルの形（段・試行の数・理由・状態と、入力の uid が文字列であること）。

    ジョブファイルは Edit(**/.pace/**) の許可で書き換えられうるので、パスやキャッシュの鍵になる値
    （job_id・out_path・cache_key・variant）はファイルから使わない。load_job と judge が、created.json の列の ID と
    プランから分け直した単位で作り直す（作業ディレクトリの外を指させない）。uid が単位にあるかは load_job が見る。
    """
    if not (isinstance(obj, dict) and (obj.get("stage") in JOB_STAGES or obj.get("stage") == REVIEW_STAGE)
            and type(obj.get("attempts")) is int and 0 <= obj["attempts"] <= MAX_RETRIES + 1
            and is_str_list(obj.get("previous_errors")) and all(encodable(e) for e in obj["previous_errors"])
            and isinstance(obj.get("input"), dict)
            and obj.get("status") in ("pending", "issued", "done", "failed")):
        return False
    if stage_kind(obj["stage"]) in ("link", REVIEW_STAGE):
        # 入力そのものは load_job が、プランとキャッシュから作り直したものと突き合わせる
        target = obj["input"].get("target")
        return isinstance(target, dict) and isinstance(target.get("id"), str)
    if stage_kind(obj["stage"]) == "decycle":
        # 輪を断つ段も、入力は load_job が作り直したものと突き合わせる
        return isinstance(obj["input"].get("nodes"), list) and isinstance(obj["input"].get("edges"), list)
    if obj["stage"] == "classify":
        units = obj["input"].get("units")
        return isinstance(units, list) and bool(units) and all(
            isinstance(u, dict) and isinstance(u.get("uid"), str) for u in units)
    unit = obj["input"].get("unit")
    return isinstance(unit, dict) and isinstance(unit.get("uid"), str)


def encodable(text):
    """UTF-8 で書ける文字列か（対のない surrogate を含む AI の答えは、キャッシュにもジョブにも書けない）。"""
    try:
        text.encode("utf-8")
        return True
    except UnicodeEncodeError:
        return False


# 作業ディレクトリのファイルから読んでパスに使う名前の形。ほかの形（`../` を含むものなど）は形の誤りとして断る
ROUND_ID = re.compile(r"[0-9a-f]{8}")
JOB_ID = re.compile(r"(?:classify_(?P<variant>[AB])_[0-9]{3,}|adjudicate_(?P<uid>u[0-9a-f]{8}(?:_[0-9]+)?)"
                    r"|link_r[12]_[1-9]_(?P<decision>D_[0-9a-f]{8}(?:_[0-9]+)?_[0-9]+)"
                    r"|decycle_[1-9]_(?P<seq>[0-9]{3,})"
                    # 反論の段の名前付きグループは、根拠付けの decision と名前を重ねない（re は同じ名前を2回使えない）
                    r"|review_(?P<review_decision>D_[0-9a-f]{8}(?:_[0-9]+)?_[0-9]+))_[0-9a-f]{8}")

# 差し戻しの理由1件の長さの上限。理由はジョブファイルに溜まるので、長いとジョブが Read の上限を超える
ERROR_MAX_CHARS = 300


def write_json(path, obj):
    try:
        data = dumps(obj).encode("utf-8")
    except UnicodeEncodeError:
        raise PaceError("UTF-8 で書けない字があるので書けません（作業ディレクトリのパスか、書き換えられたファイル）。"
                        "init を打ち直してください: %s" % path)
    write_atomic(path, data, PRIVATE_FILE)


def check_label(label, where):
    """ラベル1つを確かめ、知らないキーを捨てたものを返す。誤りは ValueError。"""
    if not isinstance(label, dict):
        raise ValueError("%s のラベルがオブジェクトではありません" % where)
    kind = label.get("kind")
    text = label.get("text")
    if not isinstance(text, str) or not text.strip() or not encodable(text):
        raise ValueError("%s のラベルの text が空でない文字列ではありません（UTF-8 で書けない字も含めない）" % where)
    if kind == "decision":
        alts = label.get("alternatives")
        if not isinstance(alts, list) or not alts or not all(isinstance(a, str) and a.strip() and encodable(a)
                                                             for a in alts):
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


CLAIM_ID = re.compile(r"c[0-9]+")
GAP_ID = re.compile(r"g[0-9]+")


def nonempty_text(value, where):
    if not isinstance(value, str) or not value.strip() or not encodable(value):
        raise ValueError("%s が空でない文字列ではありません（UTF-8 で書けない字も含めない）" % where)
    return value


def check_link(answer, link_input):
    """根拠付けの答えを、作り直した入力に照らして確かめ、知らないキーを捨てたものを返す。誤りは ValueError。

    検証は docs/idea.md 段4 の6つに、from が空でないこと・id の形・UTF-8 で書けることを足したもの（15節）。
    """
    if not isinstance(answer, dict):
        raise ValueError("答えが JSON のオブジェクトではありません")
    target = link_input["target"]["id"]
    candidates = {n["id"] for g in link_input["candidates"] for n in g["nodes"]}
    lists = {}
    for key in ("claims", "gaps", "steps", "need"):
        value = answer.get(key, [])
        if not isinstance(value, list):
            raise ValueError('"%s" が配列ではありません' % key)
        lists[key] = value
    claims, gaps = [], []
    seen = set()
    for key, pattern, out in (("claims", CLAIM_ID, claims), ("gaps", GAP_ID, gaps)):
        for item in lists[key]:
            if not isinstance(item, dict):
                raise ValueError('"%s" の要素がオブジェクトではありません' % key)
            cid = item.get("id")
            if not isinstance(cid, str) or not pattern.fullmatch(cid):
                raise ValueError('"%s" の id が %s の形ではありません: %r'
                                 % (key, "c<数>" if key == "claims" else "g<数>", cid))
            if cid in seen:
                raise ValueError("id が2回あります: %s" % cid)
            seen.add(cid)
            entry = {"id": cid}
            if key == "gaps":
                if item.get("ptype") not in PTYPES:
                    raise ValueError("gap %s の ptype が fact / requirement / value のどれでもありません" % cid)
                entry["ptype"] = item["ptype"]
            entry["text"] = nonempty_text(item.get("text"), "%s の text" % cid)
            out.append(entry)
    claim_ids = {c["id"] for c in claims}
    gap_ids = {g["id"] for g in gaps}
    allowed_from = candidates | claim_ids | gap_ids
    steps = []
    used = set()
    for k, step in enumerate(lists["steps"], start=1):
        where = "step %d" % k
        if not isinstance(step, dict):
            raise ValueError("%s がオブジェクトではありません" % where)
        froms = step.get("from")
        if not isinstance(froms, list) or not froms or not all(isinstance(f, str) for f in froms):
            raise ValueError("%s の from が、空でない文字列の配列ではありません" % where)
        bad = [f for f in froms if f not in allowed_from]
        if bad:
            raise ValueError("%s の from に、候補のノード・自分の claims・gaps のどれでもない id があります: %s"
                             % (where, ", ".join(repr(b) for b in bad)))
        to = step.get("to")
        # 型を先に確かめる（配列やオブジェクトを set に照らすと TypeError で next が落ち、差し戻せなくなる）
        if not isinstance(to, str) or (to != target and to not in claim_ids):
            raise ValueError("%s の to が target.id か claims の id ではありません: %r" % (where, to))
        if to in froms:
            raise ValueError("%s の from に to が入っています（自己参照）: %s" % (where, to))
        warrant = nonempty_text(step.get("warrant"), "%s の warrant" % where)
        used.update(froms)
        steps.append({"from": list(froms), "to": to, "warrant": warrant})
    if not any(s["to"] == target for s in steps):
        raise ValueError("to が target.id（%s）の step がありません" % target)
    unused = [g for g in sorted(gap_ids) if g not in used]
    if unused:
        raise ValueError("どの step の from にも使われていない gap があります: %s" % ", ".join(unused))
    need = []
    for k, item in enumerate(lists["need"], start=1):
        if not isinstance(item, str) or not encodable(item):
            raise ValueError("need の %d 件目が文字列ではありません（UTF-8 で書けない字も含めない）" % k)
        if item.strip():
            need.append(item)
    return {"claims": claims, "gaps": gaps, "steps": steps, "need": need}


def strong_components(nodes, edges):
    """強連結成分（Tarjan 法。再帰の深さに頼らないよう、手で積む）。

    nodes は順の決まった ID の列、edges は (from, to) の列。2ノード以上の成分だけを、成分の中はノードの順、
    成分どうしは成分の中でいちばん前のノードの順に並べて返す（輪を断つジョブの連番と入力を毎回同じにするため）。
    """
    order = {n: i for i, n in enumerate(nodes)}
    succ = {n: [] for n in nodes}
    for f, t in edges:
        if f in succ and t in succ:
            succ[f].append(t)
    index, low, on_stack = {}, {}, set()
    stack, comps = [], []
    counter = 0
    for root in nodes:
        if root in index:
            continue
        work = [(root, 0)]
        while work:
            v, i = work.pop()
            if i == 0:
                index[v] = low[v] = counter
                counter += 1
                stack.append(v)
                on_stack.add(v)
            if i < len(succ[v]):
                work.append((v, i + 1))
                w = succ[v][i]
                if w not in index:
                    work.append((w, 0))
                elif w in on_stack:
                    low[v] = min(low[v], index[w])
                continue
            if low[v] == index[v]:
                comp = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    comp.append(w)
                    if w == v:
                        break
                if len(comp) >= 2:
                    comps.append(sorted(comp, key=order.get))
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[v])
    comps.sort(key=lambda c: order[c[0]])
    return comps


def check_decycle(answer, decycle_input):
    """輪を断つ答えを、作り直した入力に照らして確かめ、知らないキーを捨てたものを返す。誤りは ValueError。

    切る辺が入力にあり重ならないこと・理由が空でないこと・切ったあと輪が残らないこと（docs/idea.md 段5a）。
    """
    if not isinstance(answer, dict):
        raise ValueError("答えが JSON のオブジェクトではありません")
    cuts = answer.get("cuts")
    if not isinstance(cuts, list) or not cuts:
        raise ValueError('"cuts" が、空でない配列ではありません（輪の中の辺を1本以上切ってください）')
    edges = [(e["from"], e["to"]) for e in decycle_input["edges"]]
    have = set(edges)
    out = []
    seen = set()
    for k, cut in enumerate(cuts, start=1):
        where = "cuts の %d 件目" % k
        if not isinstance(cut, dict) or not isinstance(cut.get("from"), str) or not isinstance(cut.get("to"), str):
            raise ValueError("%s の from と to が文字列ではありません" % where)
        pair = (cut["from"], cut["to"])
        if pair not in have:
            raise ValueError("%s の辺が input.edges にありません: %s -> %s" % (where, pair[0], pair[1]))
        if pair in seen:
            raise ValueError("%s の辺が2回あります: %s -> %s" % (where, pair[0], pair[1]))
        seen.add(pair)
        out.append({"from": pair[0], "to": pair[1], "reason": nonempty_text(cut.get("reason"), "%s の reason" % where)})
    rest = [e for e in edges if e not in seen]
    left = strong_components([n["id"] for n in decycle_input["nodes"]], rest)
    if left:
        raise ValueError("切ったあとも輪が残ります: %s" % " ".join(left[0]))
    return {"cuts": out}


def check_review(answer, review_input):
    """反論の答えを、作り直した入力に照らして確かめ、知らないキーを捨てたものを返す。誤りは ValueError（17節）。

    refuted の根拠（by）は入力の前提だけにする。claim・step・ほかの決定の強さは反論の target から来うるので、
    根拠にすると自分を理由にした循環になりうる。前提はプランの原文で強さを誰からも受けないので、検証を通った
    refuted は必ず根に届く。反論の target の前提と、同じ答えのどの反論の target の前提も根拠にしない
    （refuted どうしが互いの target を根拠にし合う持ち合いも断つ）。idea.md の by_quotes は受けない（知らないキーとして捨てる）。
    """
    if not isinstance(answer, dict):
        raise ValueError("答えが JSON のオブジェクトではありません")
    items = answer.get("defeaters")
    if not isinstance(items, list):
        raise ValueError('"defeaters" が配列ではありません')
    nodes = {n["id"]: n for n in review_input["nodes"]}
    premises = {i for i, n in nodes.items() if n["kind"] in PTYPES}
    step_ids = {s["id"] for s in review_input["steps"]}
    in_steps = {f for s in review_input["steps"] for f in s["from"]}
    # target にできるのは、審査する決定・step・claim と、推論で使った前提（候補として足しただけの前提は by にだけ使う）
    targets = ({review_input["target"]["id"]} | step_ids | {i for i, n in nodes.items() if n["kind"] == "claim"}
               | (premises & in_steps))
    parsed = []
    for k, item in enumerate(items, start=1):
        where = "defeaters の %d 件目" % k
        if not isinstance(item, dict):
            raise ValueError("%s がオブジェクトではありません" % where)
        # 型を先に確かめる（配列やオブジェクトを set に照らすと TypeError で next が落ち、差し戻せなくなる）
        for key in ("target", "type", "status"):
            if not isinstance(item.get(key), str):
                raise ValueError("%s の %s が文字列ではありません" % (where, key))
        by = item.get("by", [])
        if not isinstance(by, list) or not all(isinstance(b, str) for b in by):
            raise ValueError("%s の by が文字列の配列ではありません" % where)
        note = item.get("note", "")
        if not isinstance(note, str) or not encodable(note):
            raise ValueError("%s の note が文字列ではありません（UTF-8 で書けない字も含めない）" % where)
        if item["target"] not in targets:
            raise ValueError("%s の target が、input.target.id・steps の id・claims の id・steps の from に出る前提の id の"
                             "どれでもありません（ほかの決定への反論は出さない）: %r" % (where, item["target"]))
        if item["type"] not in DEFEATER_TYPES:
            raise ValueError("%s の type が logic / missing / fact / value のどれでもありません" % where)
        if item["status"] not in DEFEATER_STATUSES:
            raise ValueError("%s の status が refuted / accepted / needs_human のどれでもありません" % where)
        text = nonempty_text(item.get("text"), "%s の text" % where)
        parsed.append((where, item["target"], item["type"], item["status"], text, list(by), note))
    attacked = {t for _, t, _, _, _, _, _ in parsed}
    out = []
    for where, target, typ, status, text, by, note in parsed:
        if status == "refuted":
            if not by:
                raise ValueError("%s は refuted なのに by が空です（input.nodes の前提の id を1つ以上挙げる）" % where)
            bad = [b for b in by if b not in premises]
            if bad:
                raise ValueError("%s の by に、input.nodes の前提（kind が fact・requirement・value）でない id があります"
                                 "（claim・step・ほかの決定は根拠にしない）: %s" % (where, ", ".join(repr(b) for b in bad)))
            self_ref = [b for b in by if b in attacked]
            if self_ref:
                raise ValueError("%s の by に、この答えの反論が target にしている前提があります（自分を理由にした循環）: %s"
                                 % (where, ", ".join(self_ref)))
        else:
            # accepted と needs_human は根拠を持たない（書いてあっても捨てる。差し戻さない）
            by = []
        out.append({"target": target, "type": typ, "text": text, "status": status, "by": by, "note": note})
    return {"defeaters": out}


# ---------------------------------------------------------------------------
# 候補の絞り込みと 2-gram の追加候補（docs/idea.md 段3・段4a。決まりは15節）
# ---------------------------------------------------------------------------

def h2_blocks(units):
    """単位ごとに、属する H2 の塊の番号を返す。sec が変わるところと、H2 の見出しの単位で区切る。

    同じ題名の H2 が2つあっても、位置で別の塊にする。H3 の見出しは path の末尾が sec になるので区切らない。
    """
    blocks = []
    b = 0
    prev = None
    for u in units:
        h2 = u["kind"] == "heading" and u["sec"] and u["text"] == u["sec"] and (not u["path"] or u["path"][-1] != u["sec"])
        if blocks and (h2 or u["sec"] != prev):
            b += 1
        blocks.append(b)
        prev = u["sec"]
    return blocks


def identifiers(text):
    """単位の本文の backtick の中身（前後の backtick と空白を落としたもの）の集合。"""
    # 閉じの無い backtick があっても、閉じた範囲だけは識別子として拾う
    spans, _ = code_spans(text)
    out = set()
    for start, end in spans:
        inner = text[start:end].strip("`").strip()
        if inner:
            out.add(inner)
    return out


def node_entry(node):
    return {"id": node["id"], "kind": "decision" if node["kind"] == "decision" else node["ptype"], "text": node["text"]}


def link_size(job_input):
    """ジョブの字数。out_path・差し戻しの理由を入れず、指示文と入力だけで測る（パスや差し戻しで候補を変えない）。"""
    return len(dumps({"instructions": INSTRUCTIONS["link"], "input": job_input}))


def candidate_rule(units, t):
    """位置 t の単位の決定について、位置 i の単位が候補の規則（同じ H2 の塊・前後5単位・冒頭の節・識別子の共有）に
    当たるかを返す関数を作る。80件と40,000字の切り落としは見ない（失敗した単位の影響の範囲にも使う。16節）。
    """
    blocks = h2_blocks(units)
    h2_positions = [i for i, u in enumerate(units)
                    if u["kind"] == "heading" and u["sec"] and u["text"] == u["sec"]
                    and (not u["path"] or u["path"][-1] != u["sec"])]
    opening = blocks[h2_positions[0]] if h2_positions else None
    t_ids = identifiers(units[t]["text"])

    def hit(i):
        return bool(blocks[i] == blocks[t] or abs(i - t) <= WINDOW or opening is None or blocks[i] <= opening
                    or (t_ids and t_ids & identifiers(units[i]["text"])))
    return hit


def excluded_note(units, nodes, excluded):
    """外した決定を、入力の excluded_note の形（ノードの順の [{"text","plan_text"}]）にする。"""
    texts = {u["id"]: u["text"] for u in units}
    return [{"text": n["text"], "plan_text": texts[n["unit"]]} for n in nodes if n["id"] in excluded]


def select_candidates(units, nodes, decision, excluded=frozenset()):
    """決定1件の r1 の入力を作る。

    同じ H2 の塊・前後5単位・冒頭の節・識別子の共有のどれかに当たる単位のノードを、位置の近い単位から
    単位ごとに丸ごと入れ、80ノードを超える単位の手前で止める。ジョブが40,000字を超えたら遠い単位から削る。
    excluded（輪を断つ段で外した決定）は候補に入れず、excluded_note に入れてから字数を測る（16節）。
    """
    pos = {u["id"]: i for i, u in enumerate(units)}
    by_unit = {}
    for n in nodes:
        by_unit.setdefault(n["unit"], []).append(n)
    t = pos[decision["unit"]]
    hit = candidate_rule(units, t)
    picked = []
    for i, u in enumerate(units):
        unit_nodes = [n for n in by_unit.get(u["id"], []) if n["id"] != decision["id"] and n["id"] not in excluded]
        if not unit_nodes:
            continue
        if hit(i):
            picked.append((abs(i - t), i, unit_nodes))
    picked.sort(key=lambda x: (x[0], x[1]))
    groups = []
    total = 0
    for _, i, unit_nodes in picked:
        if total + len(unit_nodes) > MAX_CANDIDATES:
            break
        total += len(unit_nodes)
        groups.append({"section": units[i]["sec"], "plan_text": units[i]["text"],
                       "nodes": [node_entry(n) for n in unit_nodes]})
    tu = units[t]
    job_input = {"target": {"id": decision["id"], "text": decision["text"], "alternatives": decision["alternatives"],
                            "section": tu["sec"], "plan_text": tu["text"]},
                 "candidates": groups}
    if excluded:
        job_input["excluded_note"] = excluded_note(units, nodes, excluded)
    while groups and link_size(job_input) > LINK_JOB_MAX_CHARS:
        groups.pop()
    return job_input


def bigrams(text):
    """空白を落とし ASCII を小文字にした文字の2-gram の出現回数。"""
    s = "".join(c.lower() if c.isascii() else c for c in text if not c.isspace())
    out = {}
    for k in range(len(s) - 1):
        g = s[k:k + 2]
        out[g] = out.get(g, 0) + 1
    return out


def cosine(a, b):
    if not a or not b:
        return 0.0
    if len(a) > len(b):
        a, b = b, a
    dot = sum(v * b.get(k, 0) for k, v in a.items())
    if not dot:
        return 0.0
    na = sum(v * v for v in a.values()) ** 0.5
    nb = sum(v * v for v in b.values()) ** 0.5
    return dot / (na * nb)


def expand_candidates(units, nodes, decision, r1_input, r1_answer, vectors=None, excluded=frozenset()):
    """r1 の答えの need から 2-gram で近いノードを選び、r2 の入力を作る。足すものが無ければ None。

    r2 のジョブが40,000字を超えたら、足したものを類似度の低い順に削る（excluded_note も入れて測る）。
    """
    return trim_r2(units, r1_input, r1_answer,
                   pick_additions(units, nodes, decision, r1_input, r1_answer, vectors, excluded))


def pick_additions(units, nodes, decision, r1_input, r1_answer, vectors=None, excluded=frozenset()):
    """r1 の答えの need から 2-gram で近いノードを選ぶ（削る前）。[(類似度, ノード)] を足した順に返す。

    need 1件につき6件・合計12件まで。同点は決定に位置の近い単位・前の単位・単位の中のノードの順。
    excluded（輪を断つ段で外した決定）は選ばない。vectors はノードの id から 2-gram へのキャッシュ（同じ next の中で使い回す）。
    字数で r2 を作れなかった決定（足すものはあったが全部削った）を見分けるため、削る部分と分けてある（16節）。
    """
    need = r1_answer["need"]
    pos = {u["id"]: i for i, u in enumerate(units)}
    texts = {u["id"]: u["text"] for u in units}
    have = {n["id"] for g in r1_input["candidates"] for n in g["nodes"]} | {decision["id"]} | set(excluded)
    t = pos[decision["unit"]]
    pool = []
    for order, n in enumerate(nodes):
        if n["id"] in have:
            continue
        if vectors is not None and n["id"] in vectors:
            vec = vectors[n["id"]]
        else:
            vec = bigrams(n["text"] + texts[n["unit"]])
            if vectors is not None:
                vectors[n["id"]] = vec
        pool.append((abs(pos[n["unit"]] - t), pos[n["unit"]], order, n, vec))
    added = []
    chosen = set()
    for item in need:
        if len(added) >= EXPAND_MAX:
            break
        q = bigrams(item)
        scored = [(-cosine(q, vec), d, p, o, n) for d, p, o, n, vec in pool if n["id"] not in chosen]
        scored = [s for s in scored if s[0] < 0]
        scored.sort(key=lambda s: s[:4])
        for s in scored[:EXPAND_PER_NEED]:
            if len(added) >= EXPAND_MAX:
                break
            chosen.add(s[4]["id"])
            added.append((-s[0], s[4]))
    return added


def trim_r2(units, r1_input, r1_answer, added):
    """足したものから r2 の入力を作り、40,000字を超える間は類似度の低いものから削る。0件になれば None。"""
    added = list(added)
    while added:
        job_input = r2_input(units, r1_input, [n for _, n in added], r1_answer)
        if link_size(job_input) <= LINK_JOB_MAX_CHARS:
            return job_input
        # 類似度のいちばん低いものを削る（同じ類似度なら後に足したもの）
        low = min(range(len(added)), key=lambda k: (added[k][0], -k))
        del added[low]
    return None


def r2_input(units, r1_input, added, previous):
    """r1 の入力に、足したノード（同じ単位のまとまりがあればそこへ、無ければ後ろに）と added・previous を足す。

    r1 の入力の excluded_note も写す（r1 と r2 で、AI に渡す規則の材料を揃える）。
    """
    texts = {u["id"]: (u["sec"], u["text"]) for u in units}
    groups = [dict(g, nodes=list(g["nodes"])) for g in r1_input["candidates"]]
    new_groups = {}
    for n in added:
        if n["unit"] not in new_groups:
            sec, text = texts[n["unit"]]
            new_groups[n["unit"]] = {"section": sec, "plan_text": text, "nodes": []}
            groups.append(new_groups[n["unit"]])
        new_groups[n["unit"]]["nodes"].append(node_entry(n))
    out = {"target": r1_input["target"], "candidates": groups}
    if "excluded_note" in r1_input:
        out["excluded_note"] = r1_input["excluded_note"]
    out.update({"added": [n["id"] for n in added], "previous": previous})
    return out


# ---------------------------------------------------------------------------
# 結合と検証（docs/idea.md 段5。決まりは16節）
# ---------------------------------------------------------------------------

UNGROUNDED, NEEDS_GAP, ROOTED = 0, 1, 2
STATUS_NAMES = {UNGROUNDED: "ungrounded", NEEDS_GAP: "needs_gap", ROOTED: "rooted"}


def strengths(decision_ids, answers, fixed=None, premises=frozenset()):
    """決定ごとの強さ（ungrounded < needs_gap < rooted）を、最小の不動点で計算する。

    premise は rooted、gap は needs_gap、step の強さは from のいちばん弱いもの、ノードは step のいちばん強いもの、
    step の無いノードは ungrounded。全部を ungrounded から始めて変わらなくなるまで繰り返すので、輪の中だけで支え合う
    決定は ungrounded のまま残る（docs/idea.md:135・438 の「循環の中でしか支えられていない」）。
    fixed は強さを決めておく決定（楽観の計算の U、自前の推論の完結で rooted とみなすほかの決定）。
    """
    fixed = fixed or {}
    value = {d: fixed.get(d, UNGROUNDED) for d in decision_ids}
    claims = {}
    changed = True
    while changed:
        changed = False
        for d in decision_ids:
            answer = answers.get(d)
            if d in fixed or answer is None:
                continue
            gaps = {g["id"] for g in answer["gaps"]}
            local = claims.setdefault(d, {c["id"]: UNGROUNDED for c in answer["claims"]})
            best = {}
            for step in answer["steps"]:
                vals = []
                for f in step["from"]:
                    if f in gaps:
                        vals.append(NEEDS_GAP)
                    elif f in local:
                        vals.append(local[f])
                    elif f in value:
                        vals.append(value[f])
                    elif f in premises:
                        vals.append(ROOTED)
                    else:
                        # 候補のノードで決定でも前提でもないもの（作り直したノードに無い）は根拠にしない
                        vals.append(UNGROUNDED)
                s = min(vals)
                best[step["to"]] = max(best.get(step["to"], UNGROUNDED), s)
            for c, v in best.items():
                if c == d:
                    if v > value[d]:
                        value[d] = v
                        changed = True
                elif c in local and v > local[c]:
                    local[c] = v
                    changed = True
    return value


def leverage(decision_ids, answers, status):
    """gap ごとの効き {(決定, gap の id): 決定の数}（idea.md:137 に寄せた近似。多めに数える）。

    状態が needs_gap の決定から、自分の答えの辺を理由の側へ、needs_gap の決定だけを通って推移的に辿り、
    届いた決定の gap を数える（rooted・ungrounded・unverified の決定には入らない）。
    """
    ids = set(decision_ids)
    count = {}
    for start in decision_ids:
        if status[start] != "needs_gap":
            continue
        seen = {start}
        todo = [start]
        while todo:
            d = todo.pop()
            answer = answers.get(d)
            if answer is None:
                continue
            for g in answer["gaps"]:
                count[(d, g["id"])] = count.get((d, g["id"]), 0) + 1
            for step in answer["steps"]:
                for f in step["from"]:
                    if f in ids and f not in seen and status.get(f) == "needs_gap":
                        seen.add(f)
                        todo.append(f)
    return count


def leverage_sets(decision_ids, answers, status):
    """leverage と同じ辿り方で、gap ごとに止めている決定の集合 {(決定, gap の id): {決定, …}} を返す（report の項目）。

    leverage の数は、この集合の大きさと同じ。leverage はテストが戻り値を丸ごと比べているので、形を変えずに別に置く。
    """
    ids = set(decision_ids)
    out = {}
    for start in decision_ids:
        if status[start] != "needs_gap":
            continue
        seen = {start}
        todo = [start]
        while todo:
            d = todo.pop()
            answer = answers.get(d)
            if answer is None:
                continue
            for g in answer["gaps"]:
                out.setdefault((d, g["id"]), set()).add(start)
            for step in answer["steps"]:
                for f in step["from"]:
                    if f in ids and f not in seen and status.get(f) == "needs_gap":
                        seen.add(f)
                        todo.append(f)
    return out


class Round:
    """1回の next（か verify）が扱う、1つのプランの作業ディレクトリ。

    readonly（verify と、#7 の report の計算）のときは何も書かない: nodes.jsonl を消さず、jobs/round.json を作らず、
    周の印の合わない段を消さずに「作られていない」とみなす。
    """

    def __init__(self, plan_arg, readonly=False):
        self.readonly = readonly
        self.plan = Path(plan_arg)
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
        # nodes.jsonl は DONE のときだけ書く。前の DONE のものを残すと、プランが変わった・読めない・網羅が欠けた
        # などで 1 で終わったあとも古いノードを後の段が読むので、作業ディレクトリがこのプランのものと確かめたら、
        # プランを読むより先に消す
        nodes_path = self.work / "nodes.jsonl"
        try:
            if not readonly and os.path.lexists(str(nodes_path)):
                os.unlink(str(nodes_path))
        except OSError as e:
            raise PaceError("%s を消せませんでした（%s）" % (nodes_path, e.strerror or e))
        read = read_plan(self.plan)
        if read is None:
            raise PaceError("プランを読めません（無いか、UTF-8 ではありません）: %s。init を打ってください" % self.plan)
        data, text = read
        if not os.path.lexists(str(self.work / "plan.md")):
            raise PaceError("%s がありません。init を打ってください" % (self.work / "plan.md"))
        try:
            copy = read_regular(self.work / "plan.md")
        except (OSError, ValueError) as e:
            raise PaceError("%s を読めません（%s）。init を打ち直してください" % (self.work / "plan.md", e))
        if copy != data:
            raise PaceError("プランが init のあとで変わりました。init を打ち直してください: %s" % self.plan)
        # report の rounds.jsonl の plan_sha256 と show の写しは、このバイト（init の写しと同じと確かめたもの）から取る
        self.plan_bytes = data
        # 単位は units.jsonl を読まず、プランから分け直す（init と同じ規則なので同じ単位になる）。
        # units.jsonl は Edit(**/.pace/**) で書き換えられうるので、網羅とノードの基準にしない
        self.units = split_units(text)
        # ノードは unit で units.jsonl の単位を指すので、init が書いたものと同じ単位であることを確かめる
        # （無い・書き換えられた・plugin を更新して分割の規則が変わったあとに init を打っていない、を止める）
        try:
            same = read_regular(self.work / "units.jsonl") == units_bytes(self.units)
        except (OSError, ValueError):
            same = False
        if not same:
            raise PaceError("%s が、いまのプランを分けた単位と違います（無いか、書き換えられたか、分割の規則が"
                            "変わりました）。init を打ち直してください" % (self.work / "units.jsonl"))
        # 前後の文は文書の中の位置で引く（節をまたいでよい。docs/idea.md 段2 の neighbors）
        self.pos = {u["id"]: i for i, u in enumerate(self.units)}
        source = self.work / "source.json"
        self.plan_file = load_work_json(source)["plan_file"] if os.path.lexists(str(source)) else self.plan.name
        self.agent_sha = {}
        # 同じ next の中で使い回すもの（ノード・根拠付けの入力・ノードの 2-gram）。答えを取り込んだら作り直す。
        # 根拠付けの入力は (r1/r2, 決定, 外す決定の集合) で使い回す（失敗の集合に依らない）。r2 の入力は r1 の答えに依るので、
        # r1 を判定したらその決定の r2 の入力を捨て、r1 の答えが無いあいだはメモしない
        self._nodes = None
        self._link_inputs = {}
        self._vectors = {}
        self._clear_pass_memos()

    def _clear_pass_memos(self):
        """回ごとのメモ（答え・成分・失敗の集合・記憶）を捨てる。run の (1) の判定を全部終えたところで1回呼ぶ。"""
        self._pass_answers = {}
        self._components = {}
        self._failed_link = {}
        self._failed_decycle = {}
        self._memory = {}
        self._text_index = None
        # 反論の段（17節）: analyze の結果（回 K ごと）・終わった回 K・反論の入力（決定ごと）
        self._analysis = {}
        self._review_k = None
        self._review_inputs = {}

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

    def link_key(self, link_input):
        # 鍵の入力は、プランとキャッシュから作り直した入力の全体（決定・候補・r2 なら added と previous）
        return self.cache_key("link", link_input)

    def cache_get(self, key, link_input=None):
        """キャッシュの答え。無いか、読めないか、形が違えば None（その単位はジョブを作り直し、cache_put が上書きする）。

        分類と裁定は {"labels": [...]}、根拠付けは {"answer": {...}} で、根拠付けはいまの入力に照らして確かめ直す。
        輪を断つ段の答えと記憶は別の形なので、decycle_answer_get と memory_get で読む。
        cache/ は init が残すので、壊れたものに「init を打ち直して」と案内しても直らない。
        """
        path = self.cache_dir / (key + ".json")
        if not os.path.lexists(str(path)):
            return None
        try:
            obj = json.loads(read_regular(path).decode("utf-8"))
            if link_input is not None:
                return check_link(obj["answer"], link_input)
            return check_labels(obj["labels"], key)
        except (OSError, ValueError, RecursionError, TypeError, KeyError):
            return None

    def cache_put(self, key, labels=None, answer=None):
        make_private_dir(self.cache_dir)
        write_json(self.cache_dir / (key + ".json"), {"labels": labels} if answer is None else {"answer": answer})

    def _read_cache_obj(self, key):
        path = self.cache_dir / (key + ".json")
        if not os.path.lexists(str(path)):
            return None
        try:
            return json.loads(read_regular(path).decode("utf-8"))
        except (OSError, ValueError, RecursionError):
            return None

    # 輪を断つ段のキャッシュは2つの形を持つ（16節）。鍵の入力の形が違うので、sha256 で取り違えない。
    # - 答え: cache_key("decycle", {"nodes","edges"}) に {"cuts_answer": {"cuts": [{"from","to","reason"}]}}。walk の確かめにだけ使い、
    #   キャッシュに当たってもジョブは省かない（省くと記憶を書く場面が無くなる）
    # - 記憶: cache_key("decycle", {"to_plan_text","to_text"}) に {"cuts": [{"from_plan_text","from_text","reason","round","pass"}]}

    def decycle_answer_get(self, decycle_input):
        """輪を断つ答え。無いか、読めないか、いまの入力に照らして検証に落ちれば None。"""
        obj = self._read_cache_obj(self.cache_key("decycle", decycle_input))
        try:
            return check_decycle(obj["cuts_answer"], decycle_input)
        except (ValueError, TypeError, KeyError):
            return None

    def decycle_answer_put(self, decycle_input, answer):
        make_private_dir(self.cache_dir)
        write_json(self.cache_dir / (self.cache_key("decycle", decycle_input) + ".json"), {"cuts_answer": answer})

    def review_answer_get(self, review_input):
        """反論の答え（{"defeaters": [...]}）。鍵は作り直した入力の全体。無いか、読めないか、いまの入力に照らして
        検証に落ちれば None（その決定はジョブを作り直す）。"""
        obj = self._read_cache_obj(self.cache_key(REVIEW_STAGE, review_input))
        try:
            return check_review(obj["defeaters_answer"], review_input)
        except (ValueError, TypeError, KeyError):
            return None

    def review_answer_put(self, review_input, answer):
        make_private_dir(self.cache_dir)
        write_json(self.cache_dir / (self.cache_key(REVIEW_STAGE, review_input) + ".json"), {"defeaters_answer": answer})

    def memory_key(self, node):
        """切られた側（to）の決定の文（ノードの言い換えと、その単位の原文の組）を鍵にする。"""
        return self.cache_key("decycle", {"to_plan_text": self.units[self.pos[node["unit"]]]["text"],
                                          "to_text": node["text"]})

    def memory_get(self, node):
        """決定の記憶（切った辺の列）。無いか、壊れていれば空（次に書くときに上書きする）。

        読み置きはファイルと同じ鍵（文）で持つ。同じ文の決定が2つあると記憶のファイルを共有するので、決定の id で持つと、
        片方に書き足したときにもう片方の読み置きが古いまま残り、次に書くときに先の分を消す。
        """
        key = self.memory_key(node)
        if key in self._memory:
            return self._memory[key]
        obj = self._read_cache_obj(key)
        cuts = obj.get("cuts") if isinstance(obj, dict) else None
        ok = isinstance(cuts, list) and all(
            isinstance(c, dict) and all(isinstance(c.get(k), str) for k in ("from_plan_text", "from_text", "reason"))
            and isinstance(c.get("round"), str) and ROUND_ID.fullmatch(c["round"])
            and type(c.get("pass")) is int and 1 <= c["pass"] <= MAX_DECYCLE for c in cuts)
        result = [dict(c) for c in cuts] if ok else []
        self._memory[key] = result
        return result

    def memory_add(self, to_node, from_node, reason, rid, k):
        """記憶に1件足す。from が同じものがあれば、round と pass と理由をいまのものに書き直す（walk の確かめと揃える）。"""
        cuts = [dict(c) for c in self.memory_get(to_node)]
        from_texts = (self.units[self.pos[from_node["unit"]]]["text"], from_node["text"])
        entry = {"from_plan_text": from_texts[0], "from_text": from_texts[1], "reason": reason, "round": rid, "pass": k}
        for i, c in enumerate(cuts):
            if (c["from_plan_text"], c["from_text"]) == from_texts:
                cuts[i] = entry
                break
        else:
            cuts.append(entry)
        key = self.memory_key(to_node)
        make_private_dir(self.cache_dir)
        write_json(self.cache_dir / (key + ".json"), {"cuts": cuts})
        self._memory[key] = cuts

    # --- ジョブ -----------------------------------------------------------

    def round_id(self):
        """周の印。init のあと最初の next が決め、ジョブの名前に入れる（前の起動の遅れた答えを見ないため）。"""
        path = self.jobs_dir / "round.json"
        if os.path.lexists(str(path)):
            return load_work_json(path, lambda o: isinstance(o, dict) and isinstance(o.get("round"), str)
                                  and ROUND_ID.fullmatch(o["round"]))["round"]
        if self.readonly:
            raise PaceError("周の印（%s）がありません。next を DONE まで回してください" % path)
        # 印が無いのに jobs/ があれば、どの周のものか分からないので消す（symlink でないことは __init__ で確かめた）
        if self.jobs_dir.exists():
            shutil.rmtree(str(self.jobs_dir))
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
            info = load_work_json(mark, lambda o: isinstance(o, dict) and is_str_list(o.get("jobs"))
                                  and all(JOB_ID.fullmatch(j) for j in o["jobs"]))
            if info.get("round") == rid:
                return info["jobs"]
        if self.readonly:
            # 読むだけのときは消さずに「作られていない」とみなす
            return None
        if d.exists():
            shutil.rmtree(str(d))
        return None

    def job_path(self, stage, job_id):
        return self.stage_dir(stage) / (job_id + ".json")

    def load_job(self, stage, job_id):
        """列の ID のジョブを読む。

        job_id・out_path・variant は、ファイルの値でなく段のディレクトリと列の ID から決める。分類は入力の uid が
        単位にあり重ならないこと、裁定は uid が列の ID のものと同じで単位にあることを確かめる。
        """
        path = self.job_path(stage, job_id)
        m = JOB_ID.fullmatch(job_id)
        if m is None:  # created() が先に断るので、ここは二重の守り
            raise PaceError("%s の名前の形が違います。init を打ち直してください" % path)
        job = load_work_json(path, lambda o: valid_job(o) and o["stage"] == stage)
        # 名前は段の名前で始まる（回の番号がディレクトリと違う名前を読まない）
        named = job_id.startswith(stage + "_")
        if stage_kind(stage) == "link":
            # 根拠付けの入力は、プランとキャッシュから作り直したものと同じでなければならない
            # （ジョブファイルは Edit(**/.pace/**) で書き換えられうるので、検証の照らし先にしない）
            d = m.group("decision")
            ok = (named and d is not None and job["input"]["target"]["id"] == d
                  and job["input"] == self.link_input(stage, d))
        elif stage_kind(stage) == "decycle":
            # 輪を断つ段の入力も、回 k の答えから作り直した成分の列の、連番の成分の入力と同じでなければならない
            seq = m.group("seq")
            comps = self.decycle_components(stage_pass(stage)) if named and seq is not None else []
            i = int(seq) - 1 if seq is not None else -1
            ok = named and 0 <= i < len(comps) and job["input"] == self.decycle_input(stage_pass(stage), comps[i])
        elif stage == REVIEW_STAGE:
            # 反論の段の入力も、終わった回 K の答えから作り直したものと同じでなければならない（対象でない決定は None）
            d = m.group("review_decision")
            ok = (named and d is not None and job["input"]["target"]["id"] == d
                  and job["input"] == self.review_input(d))
        elif stage == "classify":
            uids = [u["uid"] for u in job["input"]["units"]]
            ok = (named and m.group("variant") is not None and all(u in self.pos for u in uids)
                  and len(set(uids)) == len(uids))
        else:
            uid = m.group("uid")
            ok = named and uid is not None and job["input"]["unit"]["uid"] == uid and uid in self.pos
        if not ok:
            raise PaceError("%s の形が違います。init を打ち直してください" % path)
        job["job_id"] = job_id
        job["out_path"] = str((self.stage_dir(stage) / (job_id + ".out.json")).absolute())
        job["variant"] = m.group("variant") if stage == "classify" else ""
        return job

    def new_job(self, stage, job_id, job_input, variant=None):
        d = self.stage_dir(stage)
        job = {"job_id": job_id, "stage": stage}
        if variant:
            job["variant"] = variant
        # cache_key は idea.md 6.4 の形に合わせて空で持つ。鍵は判定のときに単位とキャッシュから作り直す
        job.update({"cache_key": "",
                "prompt_version": dict(STAGE_VERSIONS)[stage_kind(stage)],
                "instructions": INSTRUCTIONS[stage_kind(stage)],
                "input": job_input, "out_path": str((d / (job_id + ".out.json")).absolute()),
                "attempts": 0, "previous_errors": [], "status": "pending"})
        return job

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
                    entry = self.unit_entry(unit)
                    if chunk:
                        # 字数は、実際に付ける名前と同じ長さの名前で測る（out_path の長さも字数に入る）
                        trial = self.new_job("classify", "classify_%s_%03d_%s" % (variant, seq + 1, rid),
                                             {"units": chunk + [entry]}, variant=variant)
                        if len(chunk) >= CLASSIFY_BATCH or len(dumps(trial)) > JOB_MAX_CHARS:
                            seq += 1
                            jobs.append(self.new_job("classify", "classify_%s_%03d_%s" % (variant, seq, rid),
                                                     {"units": chunk}, variant=variant))
                            chunk = []
                    chunk.append(entry)
                if chunk:
                    seq += 1
                    jobs.append(self.new_job("classify", "classify_%s_%03d_%s" % (variant, seq, rid),
                                             {"units": chunk}, variant=variant))
        return jobs

    def failed_jobs(self):
        """この周の失敗したジョブ。列の ID のジョブファイルの status から数える。

        failed_jobs.jsonl は人間が理由を読むための記録で、網羅とノードの基準にしない
        （Edit(**/.pace/**) で書き換えられうるので、偽の記録で単位を外させない）。
        """
        rid = self.round_id()
        out = []
        for stage in JOB_STAGES + (REVIEW_STAGE,):
            for job_id in self.created(stage, rid) or []:
                job = self.load_job(stage, job_id)
                if job["status"] == "failed":
                    out.append(job)
        return out

    def failed_units(self):
        """ノードの無い単位。分類と裁定の段だけを読む。

        根拠付けの失敗ではノードを消さない（消すとほかの決定の候補が変わり、根拠付けが連鎖してやり直しになる）。
        根拠付けの load_job は nodes() を呼ぶので、ここで根拠付けの段を読むと再帰する。
        """
        rid = self.round_id()
        out = set()
        for stage in NODE_STAGES:
            for job_id in self.created(stage, rid) or []:
                job = self.load_job(stage, job_id)
                if job["status"] != "failed":
                    continue
                if stage == "classify":
                    out.update(u["uid"] for u in job["input"]["units"])
                else:
                    out.add(job["input"]["unit"]["uid"])
        return out

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
            jobs.append(self.new_job("adjudicate", "adjudicate_%s_%s" % (unit["id"], rid), job_input))
        return jobs

    # --- 根拠付け -----------------------------------------------------------

    def node_list(self):
        """ノード。nodes.jsonl を読まず、キャッシュから作り直す（同じ next の中では使い回す）。"""
        if self._nodes is None:
            self._nodes = self.nodes()
        return self._nodes

    def decisions(self):
        return [n for n in self.node_list() if n["kind"] == "decision"]

    def node_by_id(self, node_id):
        return next((n for n in self.node_list() if n["id"] == node_id), None)

    def exclusions(self, k, decision):
        """回 k の決定の根拠付けで候補から外す決定（切った辺の記憶。16節）。

        記憶のうち、from の2つの文がいまの決定と同じで、かつ前の周までに切ったもの（round がいまの周の印でない）か、
        この周の前の回で切ったもの（pass が k より小さい）。同じ文の決定が2つあれば両方を外す。
        """
        rid = self.round_id()
        if self._text_index is None:
            self._text_index = {}
            for n in self.decisions():
                key = (self.units[self.pos[n["unit"]]]["text"], n["text"])
                self._text_index.setdefault(key, []).append(n["id"])
        out = set()
        for c in self.memory_get(decision):
            if c["round"] != rid or c["pass"] < k:
                out.update(self._text_index.get((c["from_plan_text"], c["from_text"]), []))
        out.discard(decision["id"])
        return frozenset(out)

    def link_input(self, stage, decision_id):
        """根拠付けの入力を、プランとキャッシュ（と記憶）から作り直す。作れない（決定が無い・r2 を作らない）ときは None。

        入力は失敗の集合を見ない（16節）。r2 の入力は回 k の r1 のキャッシュの答えだけから作る。
        """
        nodes = self.node_list()
        decision = next((n for n in nodes if n["id"] == decision_id and n["kind"] == "decision"), None)
        if decision is None:
            return None
        k = stage_pass(stage)
        excluded = self.exclusions(k, decision)
        r1_key = ("r1", decision_id, excluded)
        if r1_key not in self._link_inputs:
            self._link_inputs[r1_key] = select_candidates(self.units, nodes, decision, excluded)
        r1 = self._link_inputs[r1_key]
        if stage.startswith("link_r1_"):
            return r1
        r2_key = ("r2", decision_id, excluded)
        if r2_key in self._link_inputs:
            return self._link_inputs[r2_key]
        answer = self.cache_get(self.link_key(r1), r1)
        if answer is None:
            # r1 の答えが無いあいだはメモしない（同じ next の中で r1 の答えが書かれうる）
            return None
        result = None
        if answer["need"]:
            result = expand_candidates(self.units, nodes, decision, r1, answer, self._vectors, excluded)
        self._link_inputs[r2_key] = result
        return result

    def r2_dropped(self, k, decision):
        """字数で r2 を作れなかった決定か（足す候補はあったが、40,000字で全部削った）。"""
        r1 = self.link_input("link_r1_%d" % k, decision["id"])
        answer = self.cache_get(self.link_key(r1), r1)
        if answer is None or not answer["need"] or self.link_input("link_r2_%d" % k, decision["id"]) is not None:
            return False
        return bool(pick_additions(self.units, self.node_list(), decision, r1, answer, self._vectors,
                                   self.exclusions(k, decision)))

    def failed_link(self, k):
        """F_link(k): 回 k までに根拠付けのジョブが失敗した決定（作られた段の status と、作り直した入力から辿る）。"""
        if k not in self._failed_link:
            rid = self.round_id()
            out = set()
            for j in range(1, k + 1):
                for r in ("link_r1", "link_r2"):
                    stage = "%s_%d" % (r, j)
                    for job_id in self.created(stage, rid) or []:
                        job = self.load_job(stage, job_id)
                        if job["status"] == "failed":
                            out.add(job["input"]["target"]["id"])
            self._failed_link[k] = frozenset(out)
        return self._failed_link[k]

    def failed_decycle(self, k):
        """F_dec(<k): 回 k より前の輪を断つ段で、失敗したジョブの成分の決定。"""
        if k not in self._failed_decycle:
            rid = self.round_id()
            out = set()
            for j in range(1, min(k, MAX_DECYCLE + 1)):
                stage = "decycle_%d" % j
                for job_id in self.created(stage, rid) or []:
                    job = self.load_job(stage, job_id)
                    if job["status"] == "failed":
                        out.update(n["id"] for n in job["input"]["nodes"])
            self._failed_decycle[k] = frozenset(out)
        return self._failed_decycle[k]

    def pass_answers(self, k):
        """回 k の答え {決定: 答えか None}。F_link(k) の決定は、キャッシュに当たっても答え無しとする。

        r2 を作った決定は r2 の答え、作らなかった決定は r1 の答え（r2 だけが失敗した決定も r1 で代えない）。
        """
        if k not in self._pass_answers:
            failed = self.failed_link(k)
            out = {}
            for d in self.decisions():
                if d["id"] in failed:
                    out[d["id"]] = None
                    continue
                answer = None
                for stage in ("link_r2_%d" % k, "link_r1_%d" % k):
                    job_input = self.link_input(stage, d["id"])
                    if job_input is not None:
                        answer = self.cache_get(self.link_key(job_input), job_input)
                        break
                out[d["id"]] = answer
            self._pass_answers[k] = out
        return self._pass_answers[k]

    def pass_edges(self, k):
        """回 k の決定どうしの辺 {(from, to): [warrant, …]}。to の答えの step の from に入った決定から張る。"""
        ids = {d["id"] for d in self.decisions()}
        edges = {}
        for to, answer in self.pass_answers(k).items():
            if answer is None:
                continue
            for step in answer["steps"]:
                for f in step["from"]:
                    if f in ids and f != to:
                        edges.setdefault((f, to), [])
                        if step["warrant"] not in edges[(f, to)]:
                            edges[(f, to)].append(step["warrant"])
        return edges

    def pass_components(self, k):
        """回 k の答えの2ノード以上の強連結成分（輪）。"""
        if k not in self._components:
            self._components[k] = strong_components([d["id"] for d in self.decisions()], list(self.pass_edges(k)))
        return self._components[k]

    def decycle_components(self, k):
        """回 k の輪を断つジョブにする成分（F_dec(<k) の決定を1つも含まない成分）。"""
        failed = self.failed_decycle(k)
        return [c for c in self.pass_components(k) if not set(c) & failed]

    def decycle_input(self, k, comp):
        nodes = {n["id"]: n for n in self.decisions()}
        order = {d: i for i, d in enumerate(comp)}
        edges = sorted(((f, t), w) for (f, t), w in self.pass_edges(k).items() if f in order and t in order)
        edges.sort(key=lambda e: (order[e[0][0]], order[e[0][1]]))
        return {"nodes": [{"id": d, "text": nodes[d]["text"], "plan_text": self.units[self.pos[nodes[d]["unit"]]]["text"]}
                          for d in comp],
                "edges": [{"from": f, "to": t, "warrants": w} for (f, t), w in edges]}

    def make_link(self, stage, rid):
        """決定ごとに、キャッシュに答えの無いものだけ根拠付けのジョブを作る（r2 は追加の候補がある決定だけ）。

        この周の前の回で根拠付けが失敗した決定（F_link(k-1)）には作らない（入力が変わっても出し直さない）。
        """
        failed = self.failed_link(stage_pass(stage) - 1)
        jobs = []
        for d in self.decisions():
            if d["id"] in failed:
                continue
            job_input = self.link_input(stage, d["id"])
            if job_input is None or self.cache_get(self.link_key(job_input), job_input) is not None:
                continue
            jobs.append(self.new_job(stage, "%s_%s_%s" % (stage, d["id"], rid), job_input))
        return jobs

    def make_decycle(self, stage, rid):
        k = stage_pass(stage)
        return [self.new_job(stage, "%s_%03d_%s" % (stage, i, rid), self.decycle_input(k, comp))
                for i, comp in enumerate(self.decycle_components(k), start=1)]

    def check_pass(self, k):
        """回 k の網羅: どの決定にも回 k の答えか F_link(k) の失敗があるか。無ければ 1 で終わる。"""
        failed = self.failed_link(k)
        for d, answer in self.pass_answers(k).items():
            if answer is None and d not in failed:
                raise PaceError("決定 %s の回 %d の根拠付けの答えがありません。init を打ち直してください" % (d, k))

    def check_decycle_stage(self, k, ids, rid):
        """作られた decycle_k の列を作り直した成分の列と丸ごと比べ、done のジョブの痕跡（答えのキャッシュと記憶）を確かめる。

        decycle_6 の無い回6でも、いまの周に切った辺の記憶の消失と、失敗したジョブを done に書き換えたことを見つけるため（16節）。
        前の周に切った辺の記憶の消失は、作り直した輪か入力が変わるとき（この周に作った回1のジョブの入力を含む）に
        （列の比べか load_job で）見つかる。done を failed に書き換えると、その成分の決定は F_dec に入って後の回の輪を
        断つ段の対象から外れる。後の回の輪を断つジョブが既に作られていて、その決定を含むなら、列の比べか load_job で
        見つかる。作られる前なら反復が早く終わりうり、終わった回 K の輪と重なれば決定は U に入る。どれにも当たらなければ
        見つけず、failed_jobs だけが増える（突き合わせる痕跡が残らない。10節）。
        """
        stage = "decycle_%d" % k
        want = ["%s_%03d_%s" % (stage, i, rid) for i in range(1, len(self.decycle_components(k)) + 1)]
        if list(ids) != want:
            raise PaceError("%s の輪を断つジョブの列が、作り直した輪と違います（記憶が消えたか、書き換えられました）。"
                            "init を打ち直してください" % self.stage_dir(stage))
        nodes = {n["id"]: n for n in self.decisions()}
        for job_id in ids:
            job = self.load_job(stage, job_id)
            if job["status"] != "done":
                continue
            answer = self.decycle_answer_get(job["input"])
            ok = answer is not None
            for cut in (answer or {"cuts": []})["cuts"]:
                to, fr = nodes[cut["to"]], nodes[cut["from"]]
                texts = (self.units[self.pos[fr["unit"]]]["text"], fr["text"])
                if not any((c["from_plan_text"], c["from_text"]) == texts and c["round"] == rid and c["pass"] == k
                           for c in self.memory_get(to)):
                    ok = False
            if not ok:
                raise PaceError("%s の答えか切った辺の記憶がありません（消えたか、書き換えられました）。"
                                "init を打ち直してください" % self.job_path(stage, job_id))

    def final_pass(self):
        """終わった回 K。next が終わった回 K まで進んでいなければ 1 で終わる（段は作らない。反論の段は見ない）。

        readonly でない Round では、round_id と created が印の無い jobs/ や印の合わない段を消しうるので、
        next の外から呼ぶ段（report・show など）は readonly の Round を使う。
        """
        return self.walk(self.round_id(), create=False)[1]

    def link_answer(self, decision_id, k=None):
        """決定の回 k（省けば終わった回 K）の根拠付けの答え。無ければ None。後の段とテストが使う。

        k を省くと final_pass を通るので、next の外からは readonly の Round で呼ぶ（final_pass の注意を参照）。
        """
        if k is None:
            k = self.final_pass()
        return self.pass_answers(k).get(decision_id)

    # --- 結合と検証 ---------------------------------------------------------

    def affected(self, k):
        """U（影響を受けうる決定）と、ノードの無い単位（分類か裁定が失敗した単位）を返す（16節）。

        - 根拠付けが失敗した決定（F_link(K)）
        - 回 K の2ノード以上の成分のうち、輪を断つ段が失敗した決定を1つでも含む成分の決定
        - 失敗した単位を候補の規則の範囲に持つ決定
        - 字数で r2 を作れなかった決定（足す候補はあったが全部削った）
        """
        u = set(self.failed_link(k))
        failed_dec = self.failed_decycle(MAX_DECYCLE + 1)
        for comp in self.pass_components(k):
            if set(comp) & failed_dec:
                u.update(comp)
        failed_units = self.failed_units()
        failed_pos = [i for i, unit in enumerate(self.units) if unit["id"] in failed_units]
        for d in self.decisions():
            if failed_pos:
                hit = candidate_rule(self.units, self.pos[d["unit"]])
                if any(hit(i) for i in failed_pos):
                    u.add(d["id"])
            if d["id"] not in u and self.r2_dropped(k, d):
                u.add(d["id"])
        return u, [unit["id"] for unit in self.units if unit["id"] in failed_units]

    def analyze(self, k=None):
        """終わった回 K まで進んだ作業ディレクトリから、結合したグラフの根への到達と gap の効きを計算する（verify と report）。

        k を省けば、walk と同じ確かめ（網羅・輪を断つ段の列と痕跡）を通って K を決めてから計算する。外れれば 1 で終わる。
        結果は回ごとに1回だけ計算して置く（反論の段の入力を決定ごとに作るたびに計算し直さない）。
        U・unverified・uncertain の計算は #6 のまま（16節）。戻り値の "u" は report と反論の段が使う。
        """
        if k is None:
            k = self.final_pass()
        if k not in self._analysis:
            self._analysis[k] = self._analyze(k)
        return self._analysis[k]

    def _analyze(self, k):
        decisions = self.decisions()
        ids = [d["id"] for d in decisions]
        premises = frozenset(n["id"] for n in self.node_list() if n["kind"] == "premise")
        answers = self.pass_answers(k)
        u, units = self.affected(k)
        optimistic = strengths(ids, answers, {d: ROOTED for d in u}, premises)
        pessimistic = strengths(ids, answers, None, premises)
        status = {}
        for d in ids:
            if optimistic[d] == ROOTED and pessimistic[d] < ROOTED:
                status[d] = "unverified"
            else:
                # 楽観は悲観より弱くならない。楽観でも届かない決定は、いまの答えのままでは失敗に依らず届かない
                status[d] = STATUS_NAMES[optimistic[d]]
        local = {}
        for d in ids:
            if status[d] == "unverified":
                continue
            fixed = {o: ROOTED for o in ids if o != d}
            local[d] = strengths(ids, answers, fixed, premises)[d] == ROOTED
        lev = leverage(ids, answers, status)
        gaps = []
        for d in ids:
            answer = answers.get(d)
            for g in (answer["gaps"] if answer else []):
                gaps.append({"id": "G_%s_%s" % (d, g["id"]), "ptype": g["ptype"],
                             "uncertain": status[d] == "unverified", "leverage": lev.get((d, g["id"]), 0)})
        cycles = [c for c in self.pass_components(k) if not set(c) & u]
        return {"pass": k, "decisions": ids, "status": status, "local": local, "gaps": gaps, "cycles": cycles,
                "unverified_units": units, "u": frozenset(u), "answers": answers}

    # --- 反論（17節） --------------------------------------------------------

    def review_k(self):
        """反論の段が審査する回 K。終わった回 K まで全部の段が済んでいれば決まる（読むだけの walk）。"""
        if self._review_k is None:
            self._review_k = self.final_pass()
        return self._review_k

    def review_targets(self):
        """反論にかける決定（決定の順）。unverified でなく、U に入らず、local で、回 K の答えに gap が1つも無いもの。

        pace.py の local は step の強い方を採るので gap を抱えた決定も local になりうる。それをかけると gap を埋めたときに
        反論もやり直しになり、gap の項目と同じ穴を指す accepted が二重に出るので外す（idea.md:136 の定義に揃える）。
        """
        a = self.analyze(self.review_k())
        out = []
        for d in a["decisions"]:
            answer = a["answers"].get(d)
            if (answer is not None and a["status"][d] != "unverified" and d not in a["u"] and a["local"].get(d)
                    and not answer["gaps"]):
                out.append(d)
        return out

    def review_input(self, decision_id):
        """反論の入力を、回 K の答えと根拠付けの入力から作り直す。対象でない決定は None。

        nodes は、回 K の答えの claims と、steps の from に出る候補のノードと、回 K の根拠付けの入力の候補（r2 の追加を
        含む）にある前提のノード（steps には出なくてよい。プランの別の節で答えている反論を、前提で反証できるように）。
        steps の id は答えの並びの順に s1… と振る。
        """
        if decision_id in self._review_inputs:
            return self._review_inputs[decision_id]
        result = None
        if decision_id in self.review_targets():
            k = self.review_k()
            decision = self.node_by_id(decision_id)
            answer = self.analyze(k)["answers"][decision_id]
            link = self.link_input("link_r2_%d" % k, decision_id) or self.link_input("link_r1_%d" % k, decision_id)
            plan_text = {}
            for g in link["candidates"]:
                for n in g["nodes"]:
                    plan_text[n["id"]] = g["plan_text"]
            nodes, seen = [], set()

            def add(node_id):
                if node_id in seen:
                    return
                n = self.node_by_id(node_id)
                if n is None:
                    return
                seen.add(node_id)
                nodes.append({"id": node_id, "kind": "decision" if n["kind"] == "decision" else n["ptype"],
                              "text": n["text"], "plan_text": plan_text.get(node_id,
                                                                            self.units[self.pos[n["unit"]]]["text"])})
            claim_ids = {c["id"] for c in answer["claims"]}
            for step in answer["steps"]:
                for f in step["from"]:
                    if f not in claim_ids:
                        add(f)
            for c in answer["claims"]:
                nodes.append({"id": c["id"], "kind": "claim", "text": c["text"]})
            for g in link["candidates"]:
                for n in g["nodes"]:
                    if n["kind"] in PTYPES:
                        add(n["id"])
            result = {"target": {"id": decision_id, "text": decision["text"],
                                 "alternatives": decision["alternatives"],
                                 "plan_text": self.units[self.pos[decision["unit"]]]["text"]},
                      "nodes": nodes,
                      "steps": [{"id": "s%d" % i, "from": list(s["from"]), "to": s["to"], "warrant": s["warrant"]}
                                for i, s in enumerate(answer["steps"], start=1)]}
        self._review_inputs[decision_id] = result
        return result

    def make_review(self, rid):
        """反論にかける決定ごとに、キャッシュに答えの無いものだけジョブを作る（根拠付けと同じく、当たれば作らない）。"""
        jobs = []
        for d in self.review_targets():
            job_input = self.review_input(d)
            if self.review_answer_get(job_input) is not None:
                continue
            jobs.append(self.new_job(REVIEW_STAGE, "%s_%s_%s" % (REVIEW_STAGE, d, rid), job_input))
        return jobs

    def review_done(self, rid):
        """反論の段が済んだことを確かめる（run の DONE の前・report・show）。外れれば 1 で終わる。

        (1) 段が作られていて、ジョブが残っていない。(2) 作られた列のどのジョブも、いまの対象の決定のもの（列は対象の部分。
        当たった決定にはジョブを作らないので、丸ごとは比べない）。(3) どの対象の決定にも、いまの入力で検証し直して通る
        キャッシュの答えか、列の中の失敗したジョブがある（ジョブの status を先に見る。failed ならキャッシュがあっても失敗）。
        """
        ids = self.created(REVIEW_STAGE, rid)
        if ids is None:
            raise PaceError("反論の段がまだありません。next を DONE まで回してください")
        targets = self.review_targets()
        status = {}
        for job_id in ids:
            job = self.load_job(REVIEW_STAGE, job_id)
            d = job["input"]["target"]["id"]
            if job["status"] in ("pending", "issued"):
                raise PaceError("反論の段にジョブが残っています。next を DONE まで回してください")
            if d not in targets:
                raise PaceError("%s は、いまの反論の対象の決定のものではありません（輪を断つジョブなどが書き換えられました）。"
                                "init を打ち直してください" % self.job_path(REVIEW_STAGE, job_id))
            status[d] = job["status"]
        for d in targets:
            if status.get(d) == "failed":
                continue
            if self.review_answer_get(self.review_input(d)) is None:
                raise PaceError("決定 %s の反論の答えがありません（消えたか、書き換えられました）。init を打ち直してください" % d)
        return ids

    # --- 直すものと訊くもの（18節） ------------------------------------------

    def findings(self):
        """反論の段まで済んだ作業ディレクトリから、fixes・questions・確かめられなかったもの・反論を作る（report と show）。

        項目は gap 1つ（questions の gap だけ、ptype と normalize した本文が同じものを1つにまとめる）・accepted か
        needs_human の反論1つ・残った輪1つ・根拠の無い決定1つ。U の決定は、悲観でも根に届いて rooted でも、その gap と
        反論を数えず「確かめられなかったもの」に出す（性質3。U の集合と計算は #6 のまま）。refuted は数えない（根拠は前提に
        限るので必ず根に届く）。
        """
        rid = self.round_id()
        k = self.review_k()
        review_ids = self.review_done(rid)
        a = self.analyze(k)
        ids = a["decisions"]
        order = {d: i for i, d in enumerate(ids)}
        answers, status, u = a["answers"], a["status"], a["u"]
        sets = leverage_sets(ids, answers, status)
        fact_gaps, merged = [], {}
        for d in ids:
            answer = answers.get(d)
            if d in u or answer is None:
                continue
            for g in answer["gaps"]:
                blocked = sets.get((d, g["id"]), set())
                if g["ptype"] == "fact":
                    fact_gaps.append({"kind": "gap", "ptype": "fact", "text": g["text"], "owners": [d],
                                      "blocked": set(blocked), "ids": ["G_%s_%s" % (d, g["id"])]})
                    continue
                item = merged.setdefault((g["ptype"], normalize(g["text"])),
                                         {"kind": "gap", "ptype": g["ptype"], "text": g["text"], "owners": [],
                                          "blocked": set(), "ids": []})
                if d not in item["owners"]:
                    item["owners"].append(d)
                item["blocked"] |= blocked
                item["ids"].append("G_%s_%s" % (d, g["id"]))

        def by_weight(item):
            return (-len(item["blocked"]), order[item["owners"][0]])
        review_status = {}
        for job_id in review_ids:
            job = self.load_job(REVIEW_STAGE, job_id)
            review_status[job["input"]["target"]["id"]] = job["status"]
        accepted, needs_human, refuted, review_failed = [], [], [], []
        for d in self.review_targets():
            if review_status.get(d) == "failed":
                review_failed.append(d)
                continue
            for df in self.review_answer_get(self.review_input(d))["defeaters"]:
                entry = dict(df, decision=d, kind="defeater")
                {"accepted": accepted, "needs_human": needs_human, "refuted": refuted}[df["status"]].append(entry)
        edges = self.pass_edges(k)
        cycles = [{"kind": "cycle", "decisions": list(c),
                   "edges": [(f, t, w) for (f, t), w in sorted(edges.items(), key=lambda e: (order[e[0][0]], order[e[0][1]]))
                             if f in c and t in c]} for c in a["cycles"]]
        in_cycle = {d for c in a["cycles"] for d in c}
        ungrounded = [{"kind": "ungrounded", "decision": d} for d in ids
                      if status[d] == "ungrounded" and d not in in_cycle and d not in u]
        fixes = sorted(fact_gaps, key=by_weight) + accepted + cycles + ungrounded
        questions = sorted(merged.values(), key=by_weight) + needs_human
        failed = self.failed_jobs()
        return {"pass": k, "analysis": a, "fixes": fixes, "questions": questions, "refuted": refuted,
                "failed_jobs": failed, "u": sorted(u, key=order.get), "review_failed": review_failed,
                "unverified_units": a["unverified_units"], "review_targets": self.review_targets(),
                "defeaters": {"accepted": len(accepted), "refuted": len(refuted), "needs_human": len(needs_human)}}

    def verify_lines(self):
        a = self.analyze()
        counts = {name: sum(1 for s in a["status"].values() if s == name)
                  for name in ("rooted", "needs_gap", "ungrounded", "unverified")}
        certain = [g for g in a["gaps"] if not g["uncertain"]]
        lines = ["VERIFY decisions=%d rooted=%d needs_gap=%d ungrounded=%d unverified=%d local=%d cycles=%d gaps=%d "
                 "uncertain=%d unverified_units=%d"
                 % (len(a["decisions"]), counts["rooted"], counts["needs_gap"], counts["ungrounded"],
                    counts["unverified"], sum(1 for v in a["local"].values() if v), len(a["cycles"]), len(certain),
                    len(a["gaps"]) - len(certain), len(a["unverified_units"]))]
        lines += ["CYCLE " + " ".join(c) for c in a["cycles"]]
        lines += ["DECISION %s %s%s" % (d, a["status"][d], " local" if a["local"].get(d) else "")
                  for d in a["decisions"]]
        lines += ["GAP %s %s leverage=%d" % (g["id"], g["ptype"], g["leverage"])
                  for g in sorted(certain, key=lambda g: (-g["leverage"], g["id"]))]
        lines += ["GAP %s %s uncertain" % (g["id"], g["ptype"])
                  for g in sorted((g for g in a["gaps"] if g["uncertain"]), key=lambda g: g["id"])]
        lines += ["UNIT %s unverified" % uid for uid in a["unverified_units"]]
        return lines

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
                if stage_kind(stage) == "link":
                    # load_job が、ジョブの入力をプランとキャッシュから作り直したものと同じと確かめてある
                    result = check_link(answer, job["input"])
                elif stage_kind(stage) == "decycle":
                    # load_job が、入力を回 k の答えから作り直した成分の入力と同じと確かめてある
                    result = check_decycle(answer, job["input"])
                elif stage == REVIEW_STAGE:
                    # load_job が、入力を回 K の答えから作り直したものと同じと確かめてある
                    result = check_review(answer, job["input"])
                elif stage == "classify":
                    result = check_classify(answer, job)
                else:
                    result = check_adjudicate(answer, job)
            except (OSError, ValueError, RecursionError) as e:
                error = "答えが検証に落ちました: %s" % e
        path = self.job_path(stage, job["job_id"])
        if error is None:
            # キャッシュに書いてから done にする（間で落ちても、次の next が同じ答えを検証し直すだけ）
            # 鍵はジョブファイルから読まず、プランから分け直した単位とキャッシュから作り直す（外を指させない）
            if stage_kind(stage) == "link":
                self.cache_put(self.link_key(job["input"]), answer=result)
                # r2 の入力は r1 の答えから作るので、その決定の r2 の入力を作り直させる
                d = job["input"]["target"]["id"]
                for key in [key for key in self._link_inputs if key[0] == "r2" and key[1] == d]:
                    del self._link_inputs[key]
            elif stage_kind(stage) == "decycle":
                # 答えのキャッシュ → 記憶 → done の順に書く（間で落ちても、次の next が同じ答えを検証し直して書き直す）
                self.decycle_answer_put(job["input"], result)
                k = stage_pass(stage)
                rid = self.round_id()
                for cut in result["cuts"]:
                    self.memory_add(self.node_by_id(cut["to"]), self.node_by_id(cut["from"]), cut["reason"], rid, k)
            elif stage == REVIEW_STAGE:
                self.review_answer_put(job["input"], result)
            elif stage == "classify":
                self._nodes = None
                for uid, labels in result.items():
                    self.cache_put(self.classify_key(job["variant"], self.units[self.pos[uid]]), labels)
            else:
                self._nodes = None
                unit = self.units[self.pos[job["input"]["unit"]["uid"]]]
                a, b = self.classified(unit)
                if a is None or b is None:
                    raise PaceError("単位 %s の分類の答えがありません。init を打ち直してください" % unit["id"])
                self.cache_put(self.adjudicate_key(unit, a, b), result)
            job["status"] = "done"
            write_json(path, job)
            return
        n = job["attempts"] + 1
        if os.path.lexists(str(out)):
            # 古い答えの名前を変えてから attempts を増やす（同じ名前で残すと、次の next が同じ答えを検証し直す）
            os.replace(str(out), str(out.parent / ("%s.rejected-%d.json" % (job["job_id"], n))))
        job["attempts"] = n
        # AI の答えの uid などに UTF-8 で書けない字があっても、理由をジョブと記録に書けるようにする
        error = error.encode("utf-8", "backslashreplace").decode("utf-8")
        if len(error) > ERROR_MAX_CHARS:
            error = error[:ERROR_MAX_CHARS] + "…"
        job["previous_errors"].append(error)
        if n > MAX_RETRIES:
            # 人間が理由を読むための記録を書き足してから failed にする（数えるのはジョブの status）。
            # 1行1件の JSON は ensure_ascii で書く（U+2028 などで行が割れないように）
            # units は「ノードの無い単位」。根拠付けの失敗ではノードを消さないので空にし、決定を decision に書く。
            # 輪を断つ段の失敗は、成分の決定（複数）を decisions に書く
            rec = {"job_id": job["job_id"], "stage": stage}
            if stage_kind(stage) in ("link", REVIEW_STAGE):
                rec.update({"units": [], "decision": job["input"]["target"]["id"]})
            elif stage_kind(stage) == "decycle":
                rec.update({"units": [], "decisions": [n["id"] for n in job["input"]["nodes"]]})
            else:
                rec["units"] = ([u["uid"] for u in job["input"]["units"]] if stage == "classify"
                                else [job["input"]["unit"]["uid"]])
            rec["errors"] = job["previous_errors"]
            path_f = self.work / "failed_jobs.jsonl"
            try:
                old = read_regular(path_f) if os.path.lexists(str(path_f)) else b""
            except ValueError:
                raise PaceError("%s がふつうのファイルではありません。init を打ち直してください" % path_f)
            write_atomic(path_f, old + (json.dumps(rec) + "\n").encode("ascii"), PRIVATE_FILE)
            job["status"] = "failed"
        else:
            job["status"] = "pending"
        write_json(path, job)

    # --- 波 ---------------------------------------------------------------

    @staticmethod
    def bundles(jobs, max_chars=BUNDLE_MAX_CHARS, max_jobs=BUNDLE_MAX_JOBS):
        """pending のジョブを束にする。A と B は別の束にし、交互に並べる。上限は段ごとに渡す（bundle_limits）。"""
        groups = {}
        for job in jobs:
            groups.setdefault(job.get("variant", ""), []).append(job)
        per_group = []
        for variant in sorted(groups):
            out = []
            cur, size = [], 0
            for job in groups[variant]:
                n = len(dumps(job))
                if cur and (size + n > max_chars or len(cur) >= max_jobs):
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
        chosen = self.bundles(pending, *bundle_limits(stage))[:PARALLEL]
        wave = []
        for k, bundle in enumerate(chosen, start=1):
            for job in bundle:
                job["status"] = "issued"
                write_json(self.job_path(stage, job["job_id"]), job)
            wave.append({"bundle_id": "b%03d" % k, "agent": "pace:" + STAGE_AGENT[stage_kind(stage)],
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

    def walk(self, rid, create=True):
        """段を前から順に見る。ジョブが残っている段で止め、("emit", 行) を返す。終わった回 K まで通れば ("done", K)。

        create が偽（読むだけ）なら段を作らず、作られていない段かジョブの残る段があれば 1 で終わる。
        decycle_k を通るたびに、回 k の網羅と、作られた輪を断つ段の列と痕跡を確かめる（16節）。
        decycle_k にジョブが無ければ、そこで終える（K=k。後の段は作らない）。
        """
        for stage in JOB_STAGES:
            ids = self.created(stage, rid)
            if ids is None:
                if not create:
                    raise PaceError("段 %s がまだありません。next を DONE まで回してください" % stage)
                if stage == "classify":
                    jobs = self.make_classify(rid)
                elif stage == "adjudicate":
                    jobs = self.make_adjudicate(rid)
                elif stage_kind(stage) == "decycle":
                    self.check_pass(stage_pass(stage))
                    jobs = self.make_decycle(stage, rid)
                else:
                    jobs = self.make_link(stage, rid)
                ids = self.write_stage(stage, rid, jobs)
            pending = []
            waiting = False
            for job_id in ids:
                job = self.load_job(stage, job_id)
                if job.get("status") == "pending":
                    pending.append(job)
                elif job.get("status") == "issued":
                    waiting = True
            if (pending or waiting) and not create:
                raise PaceError("段 %s にジョブが残っています。next を DONE まで回してください" % stage)
            if pending:
                return "emit", self.emit(stage, pending)
            if waiting:
                # 出したジョブの答えを待っている（判定のあとなので、ここには来ない見込み）
                raise PaceError("答えを待っているジョブがあります。束を subagent に渡してから打ってください")
            if stage_kind(stage) == "decycle":
                k = stage_pass(stage)
                self.check_pass(k)
                self.check_decycle_stage(k, ids, rid)
                if not ids:
                    return "done", k
        self.check_pass(PASSES)
        return "done", PASSES

    def run(self):
        rid = self.round_id()
        # (1) 判定: issued のジョブだけを判定する
        for stage in JOB_STAGES:
            ids = self.created(stage, rid)
            for job_id in ids or []:
                job = self.load_job(stage, job_id)
                if job.get("status") == "issued":
                    self.judge(job)
        # 判定で増えた失敗と記憶を、後の段を作るときに見落とさないよう、回ごとのメモを1回だけ捨てる
        self._clear_pass_memos()
        # (1b) 反論の段の issued のジョブを判定する。反論のジョブがあるのは終わった回 K まで全部の段が済んだあとだけなので、
        # K は読むだけの walk（final_pass）で取れる。取れないのは前の段が書き換えられたときだけ
        review_ids = self.created(REVIEW_STAGE, rid)
        if review_ids:
            try:
                self.review_k()
            except PaceError:
                raise PaceError("反論の段の前の段が揃っていません（輪を断つジョブなどが書き換えられました）。"
                                "init を打ち直してください")
            for job_id in review_ids:
                job = self.load_job(REVIEW_STAGE, job_id)
                if job.get("status") == "issued":
                    self.judge(job)
        # (2) 段を作る: 段を前から順に見る。ジョブが残っている段で止める
        kind, value = self.walk(rid)
        if kind == "emit":
            return value
        # (2b) 終わった回 K まで通った（網羅は walk が確かめた）。反論の段を見る（無ければ作る。ジョブが残れば出す）
        self._review_k = value
        review_ids = self.created(REVIEW_STAGE, rid)
        if review_ids is None:
            review_ids = self.write_stage(REVIEW_STAGE, rid, self.make_review(rid))
        pending = []
        for job_id in review_ids:
            job = self.load_job(REVIEW_STAGE, job_id)
            if job.get("status") == "pending":
                pending.append(job)
            elif job.get("status") == "issued":
                raise PaceError("答えを待っているジョブがあります。束を subagent に渡してから打ってください")
        if pending:
            return self.emit(REVIEW_STAGE, pending)
        self.review_done(rid)
        # (3) 反論の段まで済んだ。ノードを書く
        nodes = self.node_list()
        write_atomic(self.work / "nodes.jsonl",
                     "".join(json.dumps(n, ensure_ascii=False) + "\n" for n in nodes).encode("utf-8"),
                     PRIVATE_FILE)
        write_json(self.work / "pending.json", {"bundles": []})
        decisions = sum(1 for n in nodes if n["kind"] == "decision")
        return ["DONE decisions=%d premises=%d failed_jobs=%d"
                % (decisions, len(nodes) - decisions, len(self.failed_jobs()))]

def next_(plan_arg):
    try:
        lines = Round(plan_arg).run()
    except OSError as e:
        raise PaceError("作業ディレクトリに書けませんでした（%s）: %s" % (e.strerror or e, e.filename or plan_arg))
    except RecursionError:
        # 読めるが書き戻せない深さの入れ子（書き換えられたジョブファイルなど）
        raise PaceError("作業ディレクトリのファイルの入れ子が深すぎます。init を打ち直してください")
    try:
        sys.stdout.write("".join(line + "\n" for line in lines))
    except UnicodeEncodeError:
        raise PaceError("ジョブのパスが UTF-8 で表せないので、出力できません")
    return 0


def verify_(plan_arg):
    try:
        lines = Round(plan_arg, readonly=True).verify_lines()
    except OSError as e:
        raise PaceError("作業ディレクトリを読めませんでした（%s）: %s" % (e.strerror or e, e.filename or plan_arg))
    except RecursionError:
        raise PaceError("作業ディレクトリのファイルの入れ子が深すぎます。init を打ち直してください")
    try:
        sys.stdout.write("".join(line + "\n" for line in lines))
    except UnicodeEncodeError:
        raise PaceError("出力を UTF-8 で表せません")
    return 0


# ---------------------------------------------------------------------------
# report・show・shown（docs/plans/pace_design.md の18〜22節）
# ---------------------------------------------------------------------------

# show.md の境目の行。pace.py が書く最後の行で、書き手はその後ろにだけ注記を足す。チャットでも GitHub でも見えるよう、
# HTML コメントにしない。読む人は「囲みの外で最初に現れる境目の行」で探す（プランの原文と差分はいつも囲みの中）
BOUNDARY = "---- ここから下は書き手の注記で、pace.py は書いていない ----"
SHOW_NOTE = "差分の中の事実・価値の文・回答の引用と、境目より後ろの注記は書き手が書いたもので、照合していない。"
GAP_LABEL = {"fact": "足りない事実", "requirement": "要求", "value": "価値判断"}
STAGE_LABEL = {"classify": "分類", "adjudicate": "裁定", "link": "根拠付け", "decycle": "輪を断つ", REVIEW_STAGE: "反論"}
SHA256 = re.compile(r"[0-9a-f]{64}")
NEWLINES = re.compile(r"\r\n|\r|\n| | |\x85")


def fence(text, lang, indent):
    """原文や差分をコードブロックで囲む。囲みは、中に出る backtick のいちばん長い並びより1つ長くする（3つ以上）。"""
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    mark = "`" * max(3, longest + 1)
    return [indent + mark + lang] + [(indent + line).rstrip() for line in text.split("\n")] + [indent + mark]


def inline(text, table=False):
    """AI の文を1行のインラインコードにする（改行は空白1つ）。中身が backtick で始まるか終わるときは、前後に空白を
    1つずつ足す（GFM の決まり。足さないと囲みが成り立たず、中の <!-- --> が効いて後ろが消える）。表の中では | を \\| にする。"""
    t = NEWLINES.sub(" ", text)
    longest = max((len(m) for m in re.findall(r"`+", t)), default=0)
    mark = "`" * (longest + 1)
    if t.startswith("`") or t.endswith("`"):
        t = " " + t + " "
    out = mark + t + mark
    return out.replace("|", "\\|") if table else out


def read_rounds(work):
    """rounds.jsonl の読めた行。読めない行・形の違う行は読み飛ばし、ふつうのファイルとして読めなければ空とみなす
    （init は rounds.jsonl を残すので、壊れたものに「init を打ち直して」と案内しても直らない。cache/ と同じ）。"""
    try:
        data = read_regular(Path(work) / "rounds.jsonl")
    except (OSError, ValueError):
        return []
    out = []
    for raw in data.split(b"\n"):
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (ValueError, RecursionError):
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("kind") == "report":
            ok = (type(obj.get("round")) is int and isinstance(obj.get("run"), str) and ROUND_ID.fullmatch(obj["run"])
                  and isinstance(obj.get("plan_sha256"), str) and SHA256.fullmatch(obj["plan_sha256"])
                  and all(type(obj.get(key)) is int for key in ("failed_jobs", "fixes", "questions", "since_shown"))
                  and obj.get("next") in ("continue", "show"))
        elif obj.get("kind") == "shown":
            ok = (type(obj.get("after_round")) is int and isinstance(obj.get("snapshot_sha256"), str)
                  and SHA256.fullmatch(obj["snapshot_sha256"]))
        else:
            ok = False
        if ok:
            out.append(obj)
    return out


def write_rounds(work, lines):
    """読めた行に新しい行を足した全体を書く（読めない行は、このときに落ちる）。1行1件は ensure_ascii で書く。"""
    write_atomic(Path(work) / "rounds.jsonl", "".join(json.dumps(x) + "\n" for x in lines).encode("ascii"), PRIVATE_FILE)


def status_line(failed_jobs, fixes, questions):
    return "PACE-STATUS: errors=0 failed_jobs=%d fixes=%d questions=%d" % (failed_jobs, fixes, questions)


class Writer:
    """report.md・fixes.md・questions.md・show.md の中身を作る。パスはプランと同じディレクトリからの相対だけを書く。"""

    def __init__(self, rnd, f):
        self.rnd, self.f = rnd, f
        self.nodes = {n["id"]: n for n in rnd.node_list()}

    def unit_text(self, node_id):
        n = self.nodes[node_id]
        return self.rnd.units[self.rnd.pos[n["unit"]]]["text"]

    def decision_block(self, d, label, indent):
        n = self.nodes[d]
        return ["%s- %s（行%d）:" % (indent, label, n["line"])] + fence(self.unit_text(d), "text", indent + "  ")

    def item(self, no, it):
        """項目1つの行。番号付きの箇条書きで、原文はコードブロック、AI の文はインラインコード。"""
        sub = "   "
        if it["kind"] == "gap":
            lines = ["%d. %s: %s（止めている決定 %d件）" % (no, GAP_LABEL[it["ptype"]], inline(it["text"]), len(it["blocked"]))]
            for d in it["owners"]:
                lines += self.decision_block(d, "この gap を持つ決定", sub)
                if it["ptype"] != "fact":
                    alts = self.nodes[d].get("alternatives", [])
                    if alts:
                        lines.append("%s- 分類の段で出た別案: %s" % (sub, "、".join(inline(x) for x in alts)))
            return lines
        if it["kind"] == "defeater":
            lines = ["%d. 反論（%s。%s）: %s" % (no, it["status"], it["type"], inline(it["text"]))]
            lines += self.decision_block(it["decision"], "決定", sub)
            if it["target"] != it["decision"]:
                lines.append("%s- 反論の向き先: %s" % (sub, it["target"]))
            if it["note"].strip():
                lines.append("%s- 注: %s" % (sub, inline(it["note"])))
            return lines
        if it["kind"] == "cycle":
            lines = ["%d. 残った輪（決定 %d件。輪を断つ段を回しても残った）" % (no, len(it["decisions"]))]
            for d in it["decisions"]:
                lines += self.decision_block(d, "決定 %s" % d, sub)
            for f, t, ws in it["edges"]:
                lines.append("%s- 辺 %s → %s: %s" % (sub, f, t, "、".join(inline(w) for w in ws)))
            return lines
        return ["%d. 根拠の無い決定（行%d）:" % (no, self.nodes[it["decision"]]["line"])] + fence(
            self.unit_text(it["decision"]), "text", sub)

    def items(self, items):
        if not items:
            return ["無し"]
        out = []
        for i, it in enumerate(items, start=1):
            out += self.item(i, it)
        return out

    def unverified(self):
        f = self.f
        out = []
        for job in f["failed_jobs"]:
            out.append("- 失敗したジョブ: %s（%s。差し戻し%d回）" % (job["job_id"], STAGE_LABEL[stage_kind(job["stage"])],
                                                          MAX_RETRIES))
        for d in f["u"]:
            out += self.decision_block(d, "失敗の影響を受けうる決定", "")
        for d in f["review_failed"]:
            out += self.decision_block(d, "反論を確かめられなかった決定", "")
        for uid in f["unverified_units"]:
            unit = self.rnd.units[self.rnd.pos[uid]]
            out += ["- 確かめられなかった単位（行%d）:" % unit["line"]] + fence(unit["text"], "text", "  ")
        return out or ["無し"]

    def list_file(self, title, items, status):
        return "\n".join(["# %s: %s（%d件）" % (title, self.rnd.plan.name, len(items)), "", status, ""] + self.items(items)) + "\n"

    def report(self, status, record, note=None):
        f, a = self.f, self.f["analysis"]
        counts = {name: sum(1 for s in a["status"].values() if s == name)
                  for name in ("rooted", "needs_gap", "ungrounded", "unverified")}
        premises = sum(1 for n in self.nodes.values() if n["kind"] == "premise")
        lines = ["# PACE の結果（全体）: %s" % self.rnd.plan.name, "", status, ""]
        if note:
            lines += [note, ""]
        lines += ["- 周: %d（最後に見せてから%d周目）" % (record["round"], record["since_shown"]),
                  "- 決定 %d（rooted %d・needs_gap %d・ungrounded %d・unverified %d）、ほかの決定を根に届くとみなせば届く"
                  "決定（verify の local）%d、うち反論にかけた決定（U に入らず、答えに gap が無いもの）%d、前提 %d"
                  % (len(a["decisions"]), counts["rooted"], counts["needs_gap"], counts["ungrounded"],
                     counts["unverified"], sum(1 for v in a["local"].values() if v), len(f["review_targets"]), premises),
                  "", "## 決定ごとの状態", "", "| 決定 | 行 | 状態 | ほかを根とみなしたとき |", "| --- | --- | --- | --- |"]
        targets = set(f["review_targets"])
        for d in a["decisions"]:
            local = "local" if a["local"].get(d) else ("（unverified）" if a["status"][d] == "unverified" else "届かない")
            if d in targets:
                local += "・gap 無し（反論にかけた）"
            lines.append("| %s | %d | %s | %s |" % (d, self.nodes[d]["line"], a["status"][d], local))
        defeaters = ([("refuted", x) for x in f["refuted"]]
                     + [("accepted", x) for x in f["fixes"] if x["kind"] == "defeater"]
                     + [("needs_human", x) for x in f["questions"] if x["kind"] == "defeater"])
        lines += ["", "## 反論（%d件）" % len(defeaters), ""]
        if defeaters:
            lines += ["| 決定 | 向き先 | 種類 | 処理 | 中身 | 反証の根拠（refuted だけ） |", "| --- | --- | --- | --- | --- | --- |"]
            for _, x in defeaters:
                lines.append("| %s | %s | %s | %s | %s | %s |" % (x["decision"], x["target"], x["type"], x["status"],
                                                                  inline(x["text"], table=True), " ".join(x["by"])))
        else:
            lines.append("無し")
        cycles = [x for x in f["fixes"] if x["kind"] == "cycle"]
        lines += ["", "## 残った輪（%d件）" % len(cycles), ""]
        lines += [" ".join(c["decisions"]) for c in cycles] or ["無し"]
        cuts = []
        for d in a["decisions"]:
            for c in self.rnd.memory_get(self.nodes[d]):
                cuts.append((d, c))
        lines += ["", "## 切った辺（参考）", ""]
        if cuts:
            lines += ["| 切られた側 | 理由にした側（原文） | 理由 |", "| --- | --- | --- |"]
            for d, c in cuts:
                lines.append("| %s | %s | %s |" % (d, inline(c["from_plan_text"], table=True), inline(c["reason"], table=True)))
        else:
            lines.append("無し")
        lines += ["", "## 確かめられなかったもの", ""] + self.unverified()
        return "\n".join(lines) + "\n"

    def show(self, status, diff_lines, first):
        f = self.f
        lines = ["# PACE の結果: %s" % self.rnd.plan.name, "", status, "",
                 "## 人間への質問（%d件）" % len(f["questions"]), ""] + self.items(f["questions"])
        lines += ["", "## 残った直すもの（%d件）" % len(f["fixes"]), ""] + self.items(f["fixes"])
        lines += ["", "## 確かめられなかったもの", ""] + self.unverified()
        lines += ["", "## 最後に見せてからの差分", ""]
        if first:
            lines += ["初回（まだ人間に見せていない基準。rounds.jsonl に見せた記録が無いときもこう出し、そのときは基準が人間に"
                      "見せたものかは分からない）。", ""]
        if diff_lines:
            lines += fence("\n".join(diff_lines), "diff", "")
        else:
            lines.append("最後に見せてから変わっていません。")
        lines += ["", "## 注意", "", SHOW_NOTE, "", BOUNDARY]
        return "\n".join(lines) + "\n"


def unreadable_show(name):
    return "\n".join(["# PACE の結果: %s" % name, "", UNREADABLE_STATUS, "",
                      "プランを読めませんでした（無いか、UTF-8 ではありません）。", "", BOUNDARY]) + "\n"


def plan_lines(data):
    return LINE_END.split(data.decode("utf-8-sig", errors="replace"))


def wrap_os(e, what, where):
    return PaceError("%s（%s）: %s" % (what, e.strerror or e, e.filename or where))


def report_(plan_arg):
    try:
        rnd = Round(plan_arg, readonly=True)
        f = rnd.findings()
        rid = rnd.round_id()
        sha = hashlib.sha256(rnd.plan_bytes).hexdigest()
        rounds = read_rounds(rnd.work)
        fixes, questions, failed = len(f["fixes"]), len(f["questions"]), len(f["failed_jobs"])
        record = next((x for x in rounds if x["kind"] == "report" and x["run"] == rid), None)
        note = None
        new = record is None
        if not new:
            # 同じ周の2回目は行を足さず、記録した値をそのまま使う（同じ周を2回数えると、上限に早く着く）
            if (record["failed_jobs"], record["fixes"], record["questions"]) != (failed, fixes, questions):
                note = ("この周の記録（failed_jobs=%d fixes=%d questions=%d）と、計算し直した件数が違います（間にジョブの "
                        "status が書き換えられたとみられます）。標準出力の2行は記録の値です。"
                        % (record["failed_jobs"], record["fixes"], record["questions"]))
        else:
            last_shown = max((i for i, x in enumerate(rounds) if x["kind"] == "shown"), default=-1)
            since = [x for x in rounds[last_shown + 1:] if x["kind"] == "report"]
            since_shown = len(since) + 1
            prev = since[-1] if since else None
            # 順1〜4（7節）。順1は最後に見せたあとの1つ前の周とだけ比べ、その周に失敗したジョブがあれば当てない
            if prev is not None and prev["plan_sha256"] == sha and prev["failed_jobs"] == 0:
                nxt = "show"
            elif since_shown >= 3:
                nxt = "show"
            elif fixes == 0:
                nxt = "show"
            else:
                nxt = "continue"
            a = f["analysis"]
            gaps = {p: sum(1 for g in a["gaps"] if g["ptype"] == p) for p in PTYPES}
            record = {"kind": "report", "round": max((x["round"] for x in rounds if x["kind"] == "report"), default=0) + 1,
                      "run": rid, "plan_sha256": sha, "units": len(rnd.units), "decisions": len(a["decisions"]),
                      "rooted": sum(1 for s in a["status"].values() if s == "rooted"),
                      "needs_gap": sum(1 for s in a["status"].values() if s == "needs_gap"),
                      "ungrounded": sum(1 for s in a["status"].values() if s == "ungrounded"),
                      "unverified": sum(1 for s in a["status"].values() if s == "unverified"),
                      "gaps": gaps, "defeaters": f["defeaters"], "failed_jobs": failed, "fixes": fixes,
                      "questions": questions, "since_shown": since_shown, "next": nxt}
            rounds.append(record)
        status = status_line(record["failed_jobs"], record["fixes"], record["questions"])
        w = Writer(rnd, f)
        work = rnd.work
        write_atomic(work / "report.md", w.report(status, record, note).encode("utf-8"), PRIVATE_FILE)
        write_atomic(work / "fixes.md", w.list_file("残った直すもの", f["fixes"], status).encode("utf-8"), PRIVATE_FILE)
        write_atomic(work / "questions.md", w.list_file("人間への質問", f["questions"], status).encode("utf-8"),
                     PRIVATE_FILE)
        if new:
            write_rounds(work, rounds)
    except OSError as e:
        raise wrap_os(e, "作業ディレクトリに書けませんでした", plan_arg)
    except RecursionError:
        raise PaceError("作業ディレクトリのファイルの入れ子が深すぎます。init を打ち直してください")
    except UnicodeEncodeError:
        raise PaceError("UTF-8 で書けない字があるので書けません。init を打ち直してください")
    try:
        sys.stdout.write("FILE fixes %s\n%s\nNEXT: %s\n" % ((work / "fixes.md").absolute(), status, record["next"]))
    except UnicodeEncodeError:
        raise PaceError("出力を UTF-8 で表せません")
    return 0


def show_(plan_arg):
    plan = Path(plan_arg)
    pace_dir, work = workdir_for(plan)
    try:
        check_dir(pace_dir)
        check_dir(work)
        check_owner(plan, work)
        # 古い写しは、どの道でも最初に消す（前の show のコマンド行で、人間が見ていない版を繰り上げさせない）
        snap = work / "show_snapshot.md"
        if os.path.lexists(str(snap)):
            if os.path.isdir(str(snap)) and not os.path.islink(str(snap)):
                raise PaceError("%s がディレクトリです。消してから打ち直してください" % snap)
            os.unlink(str(snap))
    except OSError as e:
        raise wrap_os(e, "作業ディレクトリを確かめられませんでした", work)
    read = read_plan(plan)
    if read is None:
        # 性質5: 「読めなかった」だけを書き、写しを作らない。.pace/ を作るなら .gitignore も init と同じく書く。
        # source.json は書かない（無ければ check_owner がいまのプランのものとみなすので、あとの init は通る。
        # 打ち間違えたパスの名前を控えて、正しいプランの init を止めないため）
        try:
            make_private_dir(pace_dir)
            write_atomic(pace_dir / ".gitignore", b"*\n", PRIVATE_FILE)
            make_private_dir(work)
            write_atomic(work / "show.md", unreadable_show(plan.name).encode("utf-8"), PRIVATE_FILE)
        except OSError as e:
            raise wrap_os(e, "作業ディレクトリに書けませんでした", work)
        except UnicodeEncodeError:
            raise PaceError("プランの名前が UTF-8 で表せないので、書けません")
        sys.stdout.write("FILE show %s\n%s\n" % ((work / "show.md").absolute(), UNREADABLE_STATUS))
        return 0
    try:
        rnd = Round(plan_arg, readonly=True)
        f = rnd.findings()
        shown = work / "shown.md"
        if not os.path.lexists(str(shown)):
            raise PaceError("%s がありません。init を打ち直してください（init が作り直します）" % shown)
        try:
            base = read_regular(shown)
        except ValueError:
            raise PaceError("%s がふつうのファイルではありません。消してから init を打ってください" % shown)
        rounds = read_rounds(work)
        shown_lines = [x for x in rounds if x["kind"] == "shown"]
        first = not shown_lines or shown_lines[-1]["snapshot_sha256"] != hashlib.sha256(base).hexdigest()
        diff = list(difflib.unified_diff(plan_lines(base), plan_lines(rnd.plan_bytes), fromfile="最後に見せた版",
                                         tofile="いまの版", n=1, lineterm=""))
        status = status_line(len(f["failed_jobs"]), len(f["fixes"]), len(f["questions"]))
        body = Writer(rnd, f).show(status, diff, first)
        write_atomic(work / "show.md", body.encode("utf-8"), PRIVATE_FILE)
        write_atomic(work / "show_snapshot.md", rnd.plan_bytes, PRIVATE_FILE)
    except OSError as e:
        raise wrap_os(e, "作業ディレクトリに書けませんでした", work)
    except RecursionError:
        raise PaceError("作業ディレクトリのファイルの入れ子が深すぎます。init を打ち直してください")
    except UnicodeEncodeError:
        raise PaceError("UTF-8 で書けない字があるので書けません。init を打ち直してください")
    try:
        sys.stdout.write("FILE show %s\n%s\nSNAPSHOT %s\n" % ((work / "show.md").absolute(), status,
                                                          hashlib.sha256(rnd.plan_bytes).hexdigest()))
    except UnicodeEncodeError:
        raise PaceError("出力を UTF-8 で表せません")
    return 0


def shown_(plan_arg, sha):
    """写しの sha256 が合うときだけ、shown.md を写しで置き換え、rounds.jsonl に shown の行を足し、写しを消す（この順）。"""
    if not SHA256.fullmatch(sha):
        raise PaceError("sha256 の形（64桁の16進の小文字）ではありません: %s" % sha)
    plan = Path(plan_arg)
    pace_dir, work = workdir_for(plan)
    try:
        # shown は Round を作らない（プランが読めなくても打てる）ので、作業ディレクトリが自分のプランのものかをここで確かめる
        check_dir(pace_dir)
        check_dir(work)
        check_owner(plan, work)
        snap = work / "show_snapshot.md"
        if not os.path.lexists(str(snap)):
            raise PaceError("写し（%s）がありません。show から打ち直してください" % snap)
        try:
            data = read_regular(snap)
        except ValueError:
            raise PaceError("写し（%s）がふつうのファイルではありません。show から打ち直してください" % snap)
        if hashlib.sha256(data).hexdigest() != sha:
            raise PaceError("写しの sha256 が合いません（show を打ち直したか、写しが書き換えられました）。何も変えていません")
        shown = work / "shown.md"
        if os.path.lexists(str(shown)) and (os.path.islink(str(shown)) or not stat.S_ISREG(os.lstat(str(shown)).st_mode)):
            # init と同じく、作業ディレクトリの外を指すかもしれないリンクを壊さない
            raise PaceError("%s がふつうのファイルではありません。消してから init を打ってください" % shown)
        write_atomic(shown, data, PRIVATE_FILE)
        rounds = read_rounds(work)
        after = max((x["round"] for x in rounds if x["kind"] == "report"), default=0)
        rounds.append({"kind": "shown", "after_round": after, "snapshot_sha256": sha})
        write_rounds(work, rounds)
        os.unlink(str(snap))
    except OSError as e:
        raise wrap_os(e, "作業ディレクトリに書けませんでした", work)
    sys.stdout.write("SHOWN after_round=%d\n" % after)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="pace.py", description="PACE の機械の仕事を受け持つ")
    parser.add_argument("--version", action="store_true", help="段ごとの版の一覧を出す")
    sub = parser.add_subparsers(dest="command")
    p_init = sub.add_parser("init", help="プランを単位に分けて作業ディレクトリに置く")
    p_init.add_argument("plan", help="プランのパス")
    p_next = sub.add_parser("next", help="前の波の答えを取り込み、次の波の束を出す")
    p_next.add_argument("plan", help="プランのパス")
    p_verify = sub.add_parser("verify", help="終わった回 K まで進んだ作業ディレクトリを書き換えずに読み、根への到達と gap の効きを"
                                             "出す（反論の段は見ない）")
    p_verify.add_argument("plan", help="プランのパス")
    p_report = sub.add_parser("report", help="反論の段まで済んだ作業ディレクトリから、直すものと訊くものを書き、周を続けるかを出す")
    p_report.add_argument("plan", help="プランのパス")
    p_show = sub.add_parser("show", help="人間に見せる show.md と、プランの写しを書く")
    p_show.add_argument("plan", help="プランのパス")
    p_shown = sub.add_parser("shown", help="見せた写しを、次に見せるときの差分の基準にする")
    p_shown.add_argument("plan", help="プランのパス")
    p_shown.add_argument("sha256", help="show が出した写しの sha256")
    args = parser.parse_args(argv)
    if args.version:
        for name, version in STAGE_VERSIONS:
            print(f"{name} {version}")
        return 0
    commands = {"init": init, "next": next_, "verify": verify_, "report": report_, "show": show_,
                "shown": lambda plan: shown_(plan, args.sha256)}
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
