# setup

macOS の環境構築 (Ansible) と、Claude Code のグローバル設定の実体を置くリポジトリ。
個人の作業規約はグローバル (`claude/CLAUDE.md` = `~/.claude/CLAUDE.md`) が正本で、
ここには繰り返さない。ここに書くのはこのリポジトリ固有の文脈だけ。

## 構成

- `setup.zsh` — ワンライナーの入口。`-l` でローカルモード (クローン済みの場合)
- `sillicon_mac_setup.zsh` — 前段。Xcode CLT / Homebrew / Ansible / Cask / mas を入れる。
  Cask と App Store は interactive TTY が要るため Ansible の外で先に済ませている
- `playbook_sillicon_mac.yml` — `tasks/*.yml` を tag 付きで import するだけ
- `tasks/*.yml` — 1 ファイル 1 関心。tag 名はファイル名と同じ (`tasks/claude.yml` → `--tags claude`)
- `vars/packages.yml` — インストール対象のパッケージ一覧
- `claude/` — Claude Code のグローバル設定の実体。`tasks/claude.yml` が `~/.claude/` へ symlink する
- `zshrc` — `~/.zshrc` の実体
- `.github/workflows/test.yml` — `claude/tests/` を macOS runner で流す唯一の CI

## コマンド

```bash
# 特定のタスクだけ流す
ansible-playbook playbook_sillicon_mac.yml --tags claude

# claude/ のスクリプトのテスト (PR では .github/workflows/test.yml が同じものを流す)
python3 -m unittest discover -s claude/tests -p '*_test.py' -v

# 1 本だけ流す
python3 claude/tests/runcat_metrics_test.py
```

## 非自明なところ

- 編集するのは常に `claude/` 側。`~/.claude/` は symlink なので、そちらを直接直すと実体を見失う。
  既存ファイルの変更は symlink 越しに即反映されるが、**ファイルを新規追加したときだけ**
  playbook の再実行が要る
- `claude/` にスクリプトを足したら `tasks/claude.yml` の `claude_config_files` にも足す。
  忘れると配布されず、`settings.json` から参照しても動かない
- 1Password 側は手動セットアップが前提。手順の正本は `tasks/git.yml` 冒頭のコメント。
  SSH agent は 1Password アプリと一緒に止まるので「メニューバーに常駐」が要る
- スキルはこのリポでは配らない。自作の汎用スキルは shinyaoguri/claude-plugins (marketplace) の
  プラグインとして配布し、第三者配布スキルも含めて `claude/settings.json` の marketplace 宣言
  (`extraKnownMarketplaces` / `enabledPlugins`) で各マシンへ入れる
- `settings.json` の `permissions.allow` に載せてよいのは**読み取り専用のコマンドだけ**。
  allow は確認プロンプトを消す宣言なので、書き込み系を載せると「聞かれずに実行される」側へ倒れる。
  サブコマンドまで固定して書く (`Bash(git log:*)` は可、`Bash(git:*)` は不可)。
  判定は `claude/tests/settings_test.py` が CI で強制する — 引っかかったら足す前に考え直す。
  プラグイン同梱スクリプトの実行許可はここでなく各スキルの `allowed-tools` frontmatter で宣言する
  (`${CLAUDE_PLUGIN_ROOT}` が展開されるぶん、マシン依存の絶対パスを settings.json に書かずに済む)
- `claude/repo-standards.json` はリポジトリ標準チェックリストの正本。消費者は
  shinyaoguri/claude-plugins の repo-standards プラグイン (`/repo-audit` 等が
  `~/.claude/repo-standards.json` 経由で読む)。項目の増減はテストが守るが、
  check type や builtin 名の変更はプラグイン側スクリプトとの契約が壊れないか確認する
