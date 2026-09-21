---
name: claude-upstream-review
description: "Claude 本体 (Claude Code / デスクトップアプリ) の更新を意図の台帳 (claude/intents.json) と突き合わせ、自作の手段をまだ持つべきかを意図ごとに判定して Issue へ残し、reviewed_against を進める。Use when the claude-upstream issue is open, when asked to review Claude Code updates or the changelog against this setup, when checking whether a self-made hook, skill or setting can be replaced by a built-in feature, or when asked to revisit the intents ledger."
---

# Claude 本体の更新 × 意図の台帳の見直し

正本は「何をしたいか」(`claude/intents.json` の意図) で、手段は交換可能。**立証責任は維持の側にある** — 自作の手段 (`self` / `plugin-self` / `doc`) は、見直しのたびに「本体の機能で足りないのはなぜか」を言えなければ置換の候補になる。「動いているから残す」は理由にならない。

結論はチャットに書き捨てない。判定表は PR 本文へ、変更が要るものは Issue へ残す (shinyaoguri/setup#232)。

## 前提

- このリポジトリ (shinyaoguri/setup) の checkout で実行する。作業は main から切ったブランチで
- 開始時に `gh issue list --label claude-upstream --state open` で週次の検知が立てた Issue を、`gh issue list --search "intents.json"` で前回の見直しが残した未対応の Issue を把握する
- 手順 2〜4 の読み取り調査は互いに独立なので、並列のサブエージェントへ切り出し、メインには所見と根拠だけを集める (changelog の全文や docs の現物をメインへ読み込まない)。判定と起票はメインで行う

## 手順

### 1. 材料を作る

```bash
curl -fsSL --max-time 30 https://raw.githubusercontent.com/anthropics/claude-code/main/CHANGELOG.md -o "$SCRATCH/CHANGELOG.md"
python3 .github/scripts/claude-changelog-diff.py --changelog "$SCRATCH/CHANGELOG.md" \
  --intents claude/intents.json --report "$SCRATCH/report.md"
```

`$SCRATCH` はセッションの scratchpad。標準出力の JSON が `versions: 0` なら本体側に新しい版は無い — 手順 3 (文書化されていない依存) だけ行って終える。`parse_failed: true` なら見出しの形式が変わっているので、まずスクリプトを直す。

レポートは**読む材料を絞っただけ**で、判定ではない。「本体の仕様語に当たった行」は必ず全部読む。ただし**自作を置き換えられる新機能は、台帳の語彙に無い言葉で書かれる** (新しい設定キー・新しいフックのイベント・新しい組み込みスキル)。当たりの節だけで済ませず、全抜粋から `Added` で始まる行も通読する。

### 2. 意図ごとに判定する

台帳の全意図について、次のどれかを付ける。迷ったら docs の現物で確かめる (索引は `https://code.claude.com/docs/llms.txt`)。changelog の 1 行だけで決めない — 1 行は機能の存在を言うだけで、自作の手段が見ている条件まで満たすかは docs を読まないと分からない。

| 判定 | 意味 | その後 |
| --- | --- | --- |
| **維持** | 本体にまだ無い。**足りない点を 1 文で言える** | 判定表に理由を書くだけ |
| **置換** | `sunset` の条件が満たされた、または本体機能で同じ意図が満たせる | Issue を起票 |
| **書き直し** | 意図は残るが、新しい口を使えば手段が単純になる (`gap` が埋まる場合を含む) | Issue を起票 |
| **壊れた** | 依存する仕様が変わり、手段がもう意図を満たしていない | Issue を起票 (`bug`)。急ぐ |
| **unmet の解消** | `means` が空の意図を満たせる機能が入った | Issue を起票 |

- `sunset` は判定の物差し。条件が曖昧で判定できなかったら、判定できる形へ書き直すのもこの見直しの仕事
- 「置換」と判定しても、**ここでは手段を消さない**。ガードの撤去は設計判断なので、Issue から通常のループ (プランの合意 → PR) で進める
- 自作の手段が本体機能より厳しい条件を見ているなら、それは維持の理由になる。ただし「その厳しさが今も要るか」は問い直す (実測の数字が rationale に在るなら、それが古くなっていないかも)

### 3. 文書化されていない依存を実機で確かめる

`stability: undocumented` の依存は changelog に載らない。手元の `claude --version` を記録したうえで、各依存が今も成り立つかを実際に確かめる (例: transcript の JSONL に読んでいるキーが在るか・フックの親プロセスが `claude` か・`~/.claude/plans/` にプランが落ちるか)。確かめられなかったものは「未確認」と判定表に明記し、成り立つと推定で書かない。

デスクトップアプリには機械可読な変更履歴が無い。リリースノート (`https://support.claude.com/en/articles/12138966-release-notes`) を best-effort で読み、`desktop-app` の依存に関わる変更を拾う。読めなければ、その旨を判定表に書く。

### 4. 台帳の外を見る (逆方向)

被覆テストが縛るのは「実体 → 台帳」の索引だけで、**`claude/CLAUDE.md` の記述が全部どれかの意図に載っているか**までは見ていない (安定したアンカーが無い)。CLAUDE.md を通読し、台帳のどの意図にも属さない規約が在れば、意図として足すか、本体の既定の振る舞いで足りるなら記述ごと消す候補にする。CLAUDE.md は常時コンテキストに載るので、消せる行は消す側に倒す。

### 5. 記録する

1. 変更が要るものを **1 件 1 Issue** で起票する。起票前に `gh issue list --search` で重複を見る。本文には 意図の id・根拠にした changelog の行 (版つき)・確かめた docs の URL・提案する手段 を書く。claude-plugins 側の手段 (`plugin-self`) は shinyaoguri/claude-plugins へ起票する
2. 台帳を更新する PR を出す: `reviewed_against` を見直した版と今日の日付へ進め、書き直した `sunset` / `depends_on` / `gap` を反映する。**PR 本文に全意図の判定表** (id・判定・理由 1 文・起票した Issue) を載せる — 「維持」の理由もここに残り、次の見直しが前回の判断を読める
3. 週次の検知が立てた `claude-upstream` の Issue は、PR 本文に `Closes #N` と書いて閉じる
4. 判定が全部「維持」でも PR は出す (`reviewed_against` を進めないと、次の検知が同じ差分をまた並べる)

## やらないこと

- 手段の撤去・置換をこの見直しの中で実行しない (起票まで)
- 台帳に理由の本文を写さない (`why` は 1 文。本文は `rationale` の先)
- 意図を 40 件より増やさない。足す前に、既存の意図へ束ねられないかを考える
