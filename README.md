# pace
PACE: Pathway Autonomous Construction Engine A framework for autonomous construction and validation of logical decision graphs in AI agents.

AI が書いた設計プランから根拠のグラフを作り、根拠の無い決定・循環論法・プランに無い前提への依存を洗い出す Claude Code プラグインです。
設計の正は [docs/plans/pace_design.md](docs/plans/pace_design.md) にあります。

**いまの版は、プラグインの骨組みだけです。**`/pace:pace` は段ごとの版の一覧を出すだけで、周を回す仕組みは後の版で足します。

## 前提

- macOS か Linux
- Python 3.9 以上（標準ライブラリだけを使います。macOS の Command Line Tools の `python3` で動きます）
- Claude Code

## 入れ方

```bash
claude plugin marketplace add maimuzo/pace
claude plugin install pace@pace
```

入ったら、Claude Code のセッションで `/pace:pace` を打つと、段ごとの版の一覧が出ます。

## 更新

第三者の marketplace は、既定では自動で新しくなりません。新しい版を入れるときは次を叩き、Claude Code を起動し直してください。

```bash
claude plugin marketplace update pace
claude plugin update pace@pace
```

`plugin.json` に version を書いていないので、版は commit の sha で決まります。

## 手元の checkout から試す（マージ前）

試すだけなら、入れずにそのセッションだけ読み込みます。同じ名前の、入っている plugin は置き換えられます。編集はセッションを起動し直すと効きます。

```bash
claude --plugin-dir <checkout>/plugins/pace
```

入れて試すなら、checkout のパスを marketplace にします（この手順そのものは確かめていません）。

```bash
claude plugin marketplace add <checkout のパス>
claude plugin install pace@pace
```

入れたものは plugin の cache に写るので、編集を反映するには commit と、上の「更新」の2つのコマンドが要ることがあります。
GitHub から入れた `pace` の marketplace が既にあるときは名前がぶつかるので、先に `claude plugin marketplace remove pace` で外してください。

## 権限の設定

**pace.py（Bash）。**`/pace:pace` は、skill の `allowed-tools` で `pace.py` を叩くことを許しています。skill を起動したターンの間は、確認なしで叩けます。
次に人間が発言すると、この許可は消えます。

**subagent の書き込み（Write）。**PACE は、実行役の subagent（`pace:pace-worker` と `pace:pace-judge`）にジョブを渡し、答えをプランの隣の `.pace/` の下へ書かせます。
確認を出すモードでは、書き込みごとに確認が出る見込みです（公式文書の要約による。確かめてはいません）。確認を出したくないときは、プロジェクトの `.claude/settings.local.json` か `.claude/settings.json` に次を足してください。

```json
{"permissions": {"allow": ["Edit(**/.pace/**)"]}}
```

- `Write(…)` ではなく `Edit(…)` で書きます。ファイルの規則は Edit と Read で見られ、Write の規則は見られないためです（公式文書の要約）。
- この規則を足して、確認を出すモード（`manual`）で subagent に書かせたところ、確認は出ませんでした（v2.1.301 で確かめました）。
- 規則が当たるのは、プランが Claude Code を起動したディレクトリの下にあるときです。
- 規則は subagent に限らず、メインのセッションの `.pace/` への書き込みにも効きます（公式文書の要約。確かめてはいません）。

## 判定役のモデル

`pace-worker` は `sonnet`、`pace-judge` は `opus` で動くように書いてあります。

- 組織の `availableModels` で `opus` が禁じられていると、許された版に置き換わるか、親のセッションのモデルで動きます（公式文書の要約）。
- 契約やプロバイダの都合で `opus` が使えないときの振る舞いは、環境ごとに違い、確かめていません。
- モデルを差し替えたいときは、settings の `env` に次を書きます（公式文書の要約による。確かめてはいません）。これは全部の subagent に効きます。`pace-worker` と `pace-judge` は同じモデルになり、ほかの plugin の subagent も同じモデルになります。

```json
{"env": {"CLAUDE_CODE_SUBAGENT_MODEL": "sonnet", "CLAUDE_CODE_SUBAGENT_MODEL_FORCE": "1"}}
```

## plan mode では使わない

**プランは plan mode の外で書き、PACE も plan mode の外で起動してください。**

plan mode のセッション（v2.1.301）で確かめたところ、次のようになりました。

- `/pace:pace`（pace.py の Bash）は、確認なしで動きました。
- 実行役の subagent にジョブを渡させると、subagent を呼ばずにプランを書き、plan mode を抜けてよいかの確認で止まりました。
- plan mode のプランは `~/.claude/plans/` に置かれ、作業ディレクトリの外になるので、上の権限の規則も当たりません。

PACE の1回の起動は、`.pace/` への書き込みと subagent の書き込みを要するので、plan mode の中では回りません。
