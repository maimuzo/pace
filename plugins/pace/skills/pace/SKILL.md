---
name: pace
description: 設計プランの根拠を辿る PACE を回す。いまは pace.py の段ごとの版の一覧を出すだけで、周の回し方は後の版で足す。
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/pace.py" *)
---

# PACE

次のコマンドを、前置きも後置きも足さずに、このまま1回叩いてください。出力をそのまま利用者に見せて終わります。

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/pace.py" --version
```

- 上のコマンドの `${CLAUDE_PLUGIN_ROOT}` が、plugin のディレクトリの絶対パスに置き換わっていないとき（`${CLAUDE_PLUGIN_ROOT}` の文字のまま見えるとき）は、叩かずに止まり、置き換わっていなかったことを利用者に伝えてください。pace.py をほかの経路で探して叩かないでください。
- いまの版は引数を使いません。プランのパスなどが渡されても、上のコマンドだけを叩きます。
- 周の回し方（`init`・`next`・`report`・`show`・`shown` と subagent への束の渡し方）は、後の版（maimuzo/pace#7）で書きます。
