#!/usr/bin/env python3
"""PACE の機械の仕事を受け持つスクリプト。

いまは --version（段ごとの版の一覧）だけを持つ。init・next・verify・report・show・shown は
後の issue（maimuzo/pace#3 以降）で足す。Python 3.9 以上・標準ライブラリだけで動かす。
"""

import argparse
import sys

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


def main(argv=None):
    parser = argparse.ArgumentParser(prog="pace.py", description="PACE の機械の仕事を受け持つ")
    parser.add_argument("--version", action="store_true", help="段ごとの版の一覧を出す")
    args = parser.parse_args(argv)
    if args.version:
        for name, version in STAGE_VERSIONS:
            print(f"{name} {version}")
        return 0
    parser.print_help(sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
