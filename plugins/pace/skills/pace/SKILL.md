---
name: pace
description: 設計プランの根拠を辿る PACE を回す。いまは pace.py の段ごとの版の一覧を出すだけで、周の回し方は後の版で足す。
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/pace.py" *)
---

# PACE

叩く前に、下のコマンドのパスを見てください。パスが絶対パスになっておらず、ドル記号と波括弧で囲んだ変数の形のまま残っているときは、叩かずに止まり、パスが置き換わっていなかったことを利用者に伝えてください。pace.py をほかの経路で探して叩かないでください。

絶対パスになっていれば、次のコマンドを、前置きも後置きも足さずに、このまま1回叩いてください。出力をそのまま利用者に見せて終わります。

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/pace.py" --version
```

- いまの版は引数を使いません。プランのパスなどが渡されても、上のコマンドだけを叩きます。
- 周の回し方は後の版（maimuzo/pace#3 以降）で書きます。subagent へ束を渡すループは #4、`report`・`show`・`shown` と周の止め方は #7 です。
