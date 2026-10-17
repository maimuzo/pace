# pace
PACE: Pathway Autonomous Construction Engine A framework for autonomous construction and validation of logical decision graphs in AI agents.

AI が書いた設計プランから根拠のグラフを作り、根拠の無い決定・循環論法・プランに無い前提への依存を洗い出す Claude Code プラグインです。
設計の正は [docs/plans/pace_design.md](docs/plans/pace_design.md) にあります。

`/pace:pace <プランのパス>` は、プランを単位に分け、AI に頼む仕事（分類と、2回の分類が食い違ったときの裁定と、決定ごとの根拠付けと、決定どうしが互いを理由にする輪を断つことと、反論）を束にして subagent へ渡します。そのうえで、根拠の穴を、書き手（skill を起動したセッション）が直すもの（fixes）と、人間が決めるもの（questions）に分けます。

- **前半（周を回す）**: `init` → `next` を束が尽きるまで → `report` が1周です。`report` の最後の `NEXT` の行が、周を続けるか止めるかを決めます。`NEXT: continue` なら、書き手は fixes を直して次の周へ進みます。止まるのは、前の周とプランが同じとき・最後に見せてから3周目・fixes が0件のときです。
- **後半（見せる）**: `show` が、質問・残った直すもの・確かめられなかったもの・最後に見せてからのプランの差分を `show.md` にまとめます。既定の起動では、それをチャットに出し、`shown` で「見せた」と記録して止まります。`--no-ask` を付けると見せずに、`show.md` のパスと、`shown` を打つコマンド行を呼び出し側へ返します。
- 人間が答えたら、もう一度 `/pace:pace <プランのパス>` を打ちます。書き手は回答を、質問の文と人間の原文の組でプランへ書いてから、次の周を回します。

中間のファイルは、プランの隣の `.pace/<プランの名前>/` に置きます（`.pace/.gitignore` で丸ごと無視されます）。fixes.md・questions.md・report.md（全体）・show.md と、周ごとの記録 rounds.jsonl もここにあります。

結合したグラフの根への到達と gap の効きは、`DONE` のあとに `python3 "<pace.py の絶対パス>" verify <プランのパス>` で見られます（パスは下の「権限の設定」のとおり。決定と gap ごとに1行を出すので、長いプランでは千行になります。反論の段は見ません）。skill は `verify` を叩きません。

## 前提

- macOS か Linux（確かめたのは macOS だけです）
- Python 3.9 以上（標準ライブラリだけを使います。macOS の Command Line Tools の `python3` で動きます）
- Claude Code（確かめたのは 2.1.301 と 2.1.302 だけです。根拠付けは 2.1.302 で確かめました。ほかの版では、権限とモデルの振る舞いが違いうります。たとえば公式文書の要約では、2.1.251 より前は `CLAUDE_CODE_SUBAGENT_MODEL` が agent の定義の `model` より優先されます）

## 入れ方

```bash
claude plugin marketplace add maimuzo/pace
claude plugin install pace@pace
```

入ったら、Claude Code のセッションで `/pace:pace <プランのパス>` を打ちます（下の「権限の設定」の規則を先に足してください）。

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
確認を出すモード（`manual`）で確かめたところ、起動したターンの中では確認なしで進みましたが、subagent はバックグラウンドで動き、その完了の知らせで始まったターンで `pace.py` を叩くときに確認が出ました（2.1.301、user settings を読む起動）。確認を出したくないときは、下の書き込みの規則と一緒に、pace.py の絶対パスを書いた規則を足してください。パスは、`/pace:pace` を打ったときの確認の画面か、Bash のコマンドの表示に出ます（入れた plugin なら `~/.claude/plugins/cache/pace/pace/<commit の sha>/scripts/pace.py` の形で、plugin を更新すると変わるので、そのたびに直してください）。

```json
{"permissions": {"allow": ["Bash(python3 \"/Users/you/.claude/plugins/cache/pace/pace/0123abcd/scripts/pace.py\" *)", "Edit(**/.pace/**)"]}}
```

- `Bash(python3 *scripts/pace.py*)` のように途中を `*` にすると、`scripts/pace.py` という文字列を含むどんな `python3` のコマンドも確認なしで通るので、勧めません。
- 規則を足すと、確認なしで最後まで回せました。途中を `*` にした規則で continuo の2本のプラン（89単位と932単位）を分類と裁定まで（2.1.301）、絶対パスの規則で89単位のプランの写しを根拠付けまで（2.1.302）確かめました。932単位のプランは、根拠付けまでは回していません。

**subagent の書き込み（Write）**

PACE は、実行役の subagent（`pace:pace-worker` と `pace:pace-judge`）にジョブを渡し、答えをプランの隣の `.pace/` の下へ書かせます。
確認を出すモードでは、書き込みごとに確認が出る見込みです（公式文書の要約。確かめていません）。確認を出したくないときは、プロジェクトの `.claude/settings.local.json` か `.claude/settings.json` に次を足してください。

```json
{"permissions": {"allow": ["Edit(**/.pace/**)"]}}
```

- `Write(…)` ではなく `Edit(…)` で書きます。ファイルの規則は Edit と Read で見られ、Write の規則は見られないためです（公式文書の要約）。
- この規則を足し、確認を出すモード（`manual`）で subagent に書かせたところ、確認は出ませんでした。確かめた条件は、2.1.301、user settings を読まない起動（`--setting-sources project,local`）と読む起動の両方、subagent はバックグラウンドで動いた、です。規則を足さないときに確認が出るかは確かめていません。前面で動く subagent では確かめていません（Agent の呼び出しに前面を選ぶ手段がありませんでした）。
- 規則が当たるのは、プランが Claude Code を起動したディレクトリの下にあるときだけの見込みです（パスの規則の書き方からの推論。確かめていません）。
- 規則は subagent に限らず、メインのセッションの `.pace/` への書き込みにも効きます（公式文書の要約。確かめていません）。そのため、メインのセッションが subagent の答え（`*.out.json`）を自分で書いても、確認の画面では気づけません。

**書き手のプランの直し（Edit）**

書き手は、fixes を直すときと、人間の回答を書くときにプランを Edit します。プランは `.pace/` の外にあるので、上の規則は当たらず、確認を出すモードでは確認が出ます（2.1.302 で、回答のあとに起動し直した skill が回答をプランへ書く Edit が、確認で止まりました）。同じ確かめで、上の2つの規則を足した状態では、起動したターンと subagent の知らせのあとのターンの `pace.py`（`report`・`show`・`shown` を含む）と、show.md への注記の Edit には確認が出ませんでした（`--no-ask` では確かめていません。引数で権限は変わらない見込みです）。書き手がプランを直すことを利用者が見られるよう、PACE はこの規則を勧めません。`--no-ask` で人の手の無い呼び出し（ほかの道具から起動するなど）から回すときは、確認で止まらないよう、呼び出し側が自分の settings でプランの Edit を許してください。

## 判定役のモデル

`pace-worker` は `sonnet`、`pace-judge` は `opus` で動くように書いてあります。

- 組織の `availableModels` で `opus` が禁じられていると、許された版に置き換わるか、親のセッションのモデルで動きます（公式文書の要約）。
- 契約やプロバイダの都合で `opus` が使えないときの振る舞いは、環境ごとに違い、確かめていません。
- モデルを差し替えたいときは、settings の `env` に次を書きます。公式文書の要約では、これは全部の subagent に効き、`pace-worker` と `pace-judge` は同じモデルになり、ほかの plugin の subagent も同じモデルになります（確かめていません）。

```json
{"env": {"CLAUDE_CODE_SUBAGENT_MODEL": "sonnet", "CLAUDE_CODE_SUBAGENT_MODEL_FORCE": "1"}}
```

AI の答えは、プランの隣の `.pace/<プランの名前>/cache/` に残し、次の起動で使い回します。agent の定義が変わると作り直しますが、次の3つの場合はモデルが変わっても気づけないので、`cache/` を空にしてから回してください。

- 上の環境変数でモデルを差し替えたとき（2.1.251 より前は `_FORCE` 無しでも効きます）
- 組織の `availableModels` で、使うモデルが置き換わったとき
- Claude Code を更新して、`sonnet`・`opus` が指すモデルが変わったとき

## plan mode では使わない

**プランは plan mode の外で書き、PACE も plan mode の外で起動してください。**

plan mode のセッション（2.1.301）で試したところ、次のようになりました。

- `/pace:pace <プランのパス>` を打つと、`init` も叩かずにプランを書き、plan mode を抜けてよいかの確認で止まりました（user settings を読む起動）。
- 実行役の subagent にジョブを渡すよう手で頼んだときも、subagent を呼ばずにプランを書き、同じ確認で止まりました（user settings を読まない起動）。
- plan mode のプランは `~/.claude/plans/` に置かれ、作業ディレクトリの外になりました。上の権限の規則は当たらない見込みです（推論。確かめていません）。

PACE の1回の起動は subagent にジョブを渡すことを要するので、plan mode の中では回りません。
