# 全プロジェクト共通スキル

全プロジェクトで再利用する汎用 Claude Code スキルの置き場。実体はここに置き、
`tasks/claude.yml` が各スキルを `~/.claude/skills/<name>` へ個別シンボリックリンクして
全プロジェクトへ供給する。

## 置き場の切り分け

- **汎用スキル** (複数プロジェクトで使う手順・ノウハウ) → ここ `claude/skills/`
- **プロジェクト固有スキル** (そのアプリの構造・起動手順・ドメインに依存) →
  各リポジトリの `.claude/skills/` (コードと一緒にバージョン管理・レビューする)

判断基準は「2 つ以上のリポジトリで使い回すか」。使い回すならここ、そのプロジェクト
限定ならリポジトリ側。迷ったらリポジトリ側に置き、2 つ目の利用先が出た時点でここへ昇格。

## 追加方法

`claude/skills/<name>/SKILL.md` を作って置くだけ。playbook を再実行
(`ansible-playbook playbook_sillicon_mac.yml --tags claude`) すれば
`~/.claude/skills/<name>` に symlink される (playbook 側の変更は不要)。

## 外部配布スキルは別ルート

第三者が配布するスキル (例: Cloudflare の skills) は、ここへコピーせず plugin
marketplace 経由で入れる (`settings.json` の `extraKnownMarketplaces` /
`enabledPlugins` で宣言)。上流の更新に追従できるため。
