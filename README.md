# pace
PACE: Pathway Autonomous Construction Engine A framework for autonomous construction and validation of logical decision graphs in AI agents.

AI が書いた設計プランから根拠のグラフを作り、根拠の無い決定・循環論法・プランに無い前提への依存を洗い出す Claude Code プラグインです。
設計の正は [docs/plans/pace_design.md](docs/plans/pace_design.md) にあります。

**いまの版は、プラグインの骨組みだけです。**`/pace:pace` は段ごとの版の一覧を出すだけで、周を回す仕組みは後の版で足します。

## 前提

- macOS か Linux（確かめたのは macOS だけです）
- Python 3.9 以上（標準ライブラリだけを使います。macOS の Command Line Tools の `python3` で動きます）
- Claude Code（確かめたのは 2.1.301 だけです。ほかの版では、権限とモデルの振る舞いが違いうります。たとえば公式文書の要約では、2.1.251 より前は `CLAUDE_CODE_SUBAGENT_MODEL` が agent の定義の `model` より優先されます）

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

## 手元から試す（マージ前）

試すだけなら、入れずにそのセッションだけ読み込みます。同じ名前の、入っている plugin は置き換えられます（公式文書の要約）。

```bash
claude --plugin-dir <checkout>/plugins/pace
```

次の2つの形は、どちらも marketplace の名前が `pace` になります。GitHub の main から入れた `pace` が既にあるときは名前がぶつかる見込みなので（確かめていません）、先に `claude plugin marketplace remove pace` で外してください。そこから入れた plugin も使えなくなるので、試し終えたら「入れ方」のとおりに入れ直してください。

push 済みの branch から入れるなら、次の形で入れられました（2.1.301 で確かめました）。`--scope local` にすると、いまのディレクトリの設定にだけ入ります。

```bash
claude plugin marketplace add 'maimuzo/pace#<branch 名>' --scope local
claude plugin install pace@pace --scope local
```

checkout のパスを marketplace にして入れる形もあります（この手順そのものは確かめていません）。

```bash
claude plugin marketplace add <checkout のパス>
claude plugin install pace@pace
```

入れたものは plugin の cache（commit の sha のディレクトリ）に写ります。編集を反映するには、commit して（branch の形なら push もして）から次を叩き、Claude Code を起動し直します（`claude plugin update` は、入れた scope を自分で見つけます）。

```bash
claude plugin marketplace update pace
claude plugin update pace@pace
```

試し終えたら外します（branch の形の例。checkout の形で入れたときは `--scope local` を付けません）。

```bash
claude plugin uninstall pace@pace --scope local
claude plugin marketplace remove pace --scope local
```

## 権限の設定

**pace.py（Bash）**

`/pace:pace` は、skill の `allowed-tools` で `pace.py` を叩くことを許しています。公式文書の要約では、skill を起動したターンの間は確認なしで叩け、次に人間が発言すると、この許可は消えます。
確かめたのは、user settings を読まない起動（`--setting-sources project,local`）で1回叩けたことだけです。同じターンの中で何度も叩いても許可が続くかは、確かめていません。

**subagent の書き込み（Write）**

後の版の PACE は、実行役の subagent（`pace:pace-worker` と `pace:pace-judge`）にジョブを渡し、答えをプランの隣の `.pace/` の下へ書かせます（いまの版は、まだ subagent を呼びません）。
確認を出すモードでは、書き込みごとに確認が出る見込みです（公式文書の要約。確かめていません）。確認を出したくないときは、プロジェクトの `.claude/settings.local.json` か `.claude/settings.json` に次を足してください。

```json
{"permissions": {"allow": ["Edit(**/.pace/**)"]}}
```

- `Write(…)` ではなく `Edit(…)` で書きます。ファイルの規則は Edit と Read で見られ、Write の規則は見られないためです（公式文書の要約）。
- この規則を足し、確認を出すモード（`manual`）で subagent に書かせたところ、確認は出ませんでした。確かめた条件は、2.1.301、user settings を読まない起動（`--setting-sources project,local`）、subagent はバックグラウンドで動いた、です。規則を足さないときに確認が出るかは確かめていません。
- 規則が当たるのは、プランが Claude Code を起動したディレクトリの下にあるときだけの見込みです（パスの規則の書き方からの推論。確かめていません）。
- 規則は subagent に限らず、メインのセッションの `.pace/` への書き込みにも効きます（公式文書の要約。確かめていません）。そのため、メインのセッションが subagent の答え（`*.out.json`）を自分で書いても、確認の画面では気づけません。

## 判定役のモデル

`pace-worker` は `sonnet`、`pace-judge` は `opus` で動くように書いてあります。

- 組織の `availableModels` で `opus` が禁じられていると、許された版に置き換わるか、親のセッションのモデルで動きます（公式文書の要約）。
- 契約やプロバイダの都合で `opus` が使えないときの振る舞いは、環境ごとに違い、確かめていません。
- モデルを差し替えたいときは、settings の `env` に次を書きます。公式文書の要約では、これは全部の subagent に効き、`pace-worker` と `pace-judge` は同じモデルになり、ほかの plugin の subagent も同じモデルになります（確かめていません）。

```json
{"env": {"CLAUDE_CODE_SUBAGENT_MODEL": "sonnet", "CLAUDE_CODE_SUBAGENT_MODEL_FORCE": "1"}}
```

## plan mode では使わない

**プランは plan mode の外で書き、PACE も plan mode の外で起動してください。**

plan mode のセッション（2.1.301、user settings を読まない起動）で1回ずつ試したところ、次のようになりました。

- `/pace:pace`（`pace.py --version` を叩くだけ）は、確認なしで動きました。書き込みを伴う pace.py の段（`init`）は、plan mode では試していません。
- 実行役の subagent にジョブを渡すよう手で頼むと、subagent を呼ばずにプランを書き、plan mode を抜けてよいかの確認で止まりました。
- plan mode のプランは `~/.claude/plans/` に置かれ、作業ディレクトリの外になりました。上の権限の規則は当たらない見込みです（推論。確かめていません）。

PACE の1回の起動は subagent にジョブを渡すことを要するので、plan mode の中では回らないと判断しました。skill から subagent を呼ばせる形は後の版で作るので、そのときに確かめ直します。
