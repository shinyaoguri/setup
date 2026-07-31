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
- 汎用スキルは `claude/skills/<name>/` に置くと playbook が `~/.claude/skills/` へ個別 symlink する。
  第三者配布スキルはコピーせず、`claude/settings.json` の marketplace 宣言で入れて上流更新に追従する
