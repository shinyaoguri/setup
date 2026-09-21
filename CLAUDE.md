# setup

macOS の環境構築 (Ansible) と、Claude Code のグローバル設定の実体を置くリポジトリ。
個人の作業規約はグローバル (`claude/CLAUDE.md` = `~/.claude/CLAUDE.md`) が正本で、
ここには繰り返さない。ここに書くのはこのリポジトリ固有の文脈だけ。

## 構成

- `setup.zsh` — ワンライナーの入口。`-l` でローカルモード (クローン済みの場合)
- `sillicon_mac_setup.zsh` — 前段。Xcode CLT / Homebrew / Ansible / Cask / mas を入れる。
  Cask と App Store は interactive TTY が要るため Ansible の外で先に済ませている。
  optional の選択 (fzf) もここ。`--with-optional` / `--no-optional` で選択を省ける
- `playbook_sillicon_mac.yml` — `tasks/*.yml` を tag 付きで import するだけ
- `tasks/*.yml` — 1 ファイル 1 関心。tag 名はファイル名と同じ (`tasks/claude.yml` → `--tags claude`)
- `vars/packages.yml` — インストール対象のパッケージ一覧。`*_required` は無条件に入れる土台
  (無いと playbook か鍵まわりが成立しないもの)、`*_optional` は `sillicon_mac_setup.zsh` が
  fzf で選ばせるもの。required へ足すのは「無いと壊れる」ことを言えるときだけ
- `claude/` — Claude Code のグローバル設定の実体。`tasks/claude.yml` が `~/.claude/` へ symlink する
- `zshrc` / `zshenv` / `zprofile` — `~/.zshrc` などの実体 (`tasks/zshrc.yml` が symlink する)。
  対話シェル向けは zshrc、非対話シェル (hook・cron) にも要るものは zshenv、zprofile は
  ログインシェルで path_helper が並べ替えた PATH を戻すだけ
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
- SSH の鍵まわりは Secretive (Secure Enclave) 側の手動セットアップが前提。手順の正本は
  `tasks/ssh.yml` 冒頭のコメント。鍵の生成は GUI 操作なので ansible では自動化できない。
  **鍵タイプは ECDSA P-256、"Require Authentication" は外す** — どちらを外しても
  無人セッションが承認待ちで止まる側に倒れる。1Password は秘密の保管庫としてのみ使う
  (`bin/secret-read`)
- **Claude デスクトップアプリは起動した時点の PATH を持ち続ける。** Bash ツールのシェルスナップショットが
  zshenv の後でそれを書き戻すので、zshenv が export した変数は届くのに PATH の追加だけが消える
  (セットアップ前から開いていたアプリで踏んだ。#154)。**変数で渡すコマンドは絶対パスで書く**
  (`MOKUME_APP_PRIVATE_KEY_CMD` がその形)。playbook はアプリが zshenv より古いと再起動を促す。
  新しいマシンで手作業が残る手順は README の「新しいマシンで」に集めてある
- スキルはこのリポでは配らない。自作の汎用スキルは shinyaoguri/claude-plugins (marketplace) の
  プラグインとして配布し、第三者配布スキルも含めて `claude/settings.json` の marketplace 宣言
  (`extraKnownMarketplaces` / `enabledPlugins`) で各マシンへ入れる
- `settings.json` の `permissions.allow` に載せてよいのは**読み取り専用のコマンドだけ**。
  allow は確認プロンプトを消す宣言なので、書き込み系を載せると「聞かれずに実行される」側へ倒れる。
  サブコマンドまで固定して書く (`Bash(git log:*)` は可、`Bash(git:*)` は不可)。
  判定は `claude/tests/settings_test.py` が CI で強制する — 引っかかったら足す前に考え直す。
  プラグイン同梱スクリプトの実行許可はここでなく各スキルの `allowed-tools` frontmatter で宣言する
  (`${CLAUDE_PLUGIN_ROOT}` が展開されるぶん、マシン依存の絶対パスを settings.json に書かずに済む)
- `permissions.additionalDirectories` は「毎回同じ承認を押している場所」だけを足す。ここは
  コマンドでなく**範囲**の宣言で、読み取りが無確認になるだけ (編集の可否は permission mode に
  従うので default モードでは確認が残る)。worktree セッションはリポ本体も設定の実体も範囲外に
  なるため、`~/Repos` / `~/.setup` / `~/.claude` を常設している。パスは `~/` 始まりで書く
  (展開は効く。絶対パスだとユーザー名がマシンに依存する)。範囲の広すぎと書き方は
  `claude/tests/settings_test.py` が CI で見る
- 書き込み系のコマンドを確認なしで通したいときは allow ではなく PreToolUse フック側で判定する。
  allow は文字列の前方一致でしかなく「安全な場合だけ」を表現できないが、フックはリポジトリの
  状態を見て可逆と確認できたときだけ `allow` を返せる (`claude/git-safety-guard.sh` の
  ブランチ掃除と切り替えがその形。素通し = 無出力では permissions へ判定が戻り、結局確認プロンプトが出る)。
  **状態を見ても可逆にならないものは、可逆にしてから通す** — 作業ツリーの取り消しは
  「捨てられるものがそこに在ること」自体が不可逆の理由なので、状態を見ている限り永久に
  確認へ落ちる。同じフックが object DB へ退避し `refs/claude/discarded/*` に固定してから
  allow を返す (setup#142)
- **判定軸は「不可逆か」ではなく「退避を作れたか」** (setup#148)。2 週間の実測で `ask` は
  178 回・人が止めたのは 0 回で、確認が判断ではなく反射になっていた。上の「止まる 5 つ」が
  挙げるのは*どこにも残っていないものを壊すとき*なので、退避を作った後は止める理由が無い。
  いま退避を作るのは 3 つ — 作業ツリーの未コミット変更 / `git branch -D` の前のブランチ
  先端 / `git reset --hard` の前の HEAD。**退避を作れたら、コマンド全体が読めれば `allow`、
  読めなければ素通し** (判定は分類器へ戻る)。「判定不能は安全側」は**退避を作れなかった
  ときの規律**であって、作れたものには当てない
- 退避は 30 日残る。**`git discarded` で一覧・復元する** — 一覧できなければ「多少のリスクを
  許容する」が「気付けないリスク」になる。復元は種類で違う (作業ツリーは
  `git checkout <ref> -- <path>`・ブランチは `git branch <名前> <ref>`・HEAD は
  `git reset --hard <ref>`)
- **退避があっても通さないものが 2 つある。** `git clean -f` は追跡外ファイルを object DB へ
  入れられず `-x` では無視対象まで対象に入って費用が非有界になるので `ask` のまま。
  `git checkout <rev> -- <settings.json>` は `autoMode.hard_deny` の自己権限拡大に当たるので、
  退避を作ったうえで `ask` にする (対象は git にパスを展開させて見るので `-- .` でも拾う)
- `claude/repo-standards.json` はリポジトリ標準チェックリストの正本。消費者は
  shinyaoguri/claude-plugins の repo-standards プラグイン (`/repo-audit` 等が
  `~/.claude/repo-standards.json` 経由で読む)。項目の増減はテストが守るが、
  check type や builtin 名の変更はプラグイン側スクリプトとの契約が壊れないか確認する
- **シグナルの送り先は名前ではなく親子関係で持ち主を決める** (`claude/signal-guard.py`・setup#150)。
  Bash ツールのシェルもフックもそのセッションの `claude` プロセスの子なので、対象の一番近い
  `claude` の祖先が自分と違えば別セッションのもので deny になる。`pkill` / `killall` /
  `kill $P` のように送り先が静的に決まらない形も deny で、PID を出すコマンドを先に打って
  数字で送り直させる。**`kill -0` も例外にしていない** — 効いているかを他セッションへ
  本物のシグナルを打たずに確かめるためで、確かめるときは `kill -0 <他セッションの PID>` を使う
- **親子の鎖が切れた孤児は、出所のパスで持ち主を見直す** (setup#153)。スケッチは起動した
  シェルが終わると launchd へ再親付けされて `ppid=1` になるので、親子関係だけを見ていると
  自分で立てたものが時間の経過だけで「判定できない」側へ落ちる (実測で ask の過半がこれだった)。
  引数か cwd にこのセッションの scratchpad (`session_id`) が在れば `allow`、作業ディレクトリが
  在れば**同じ所を開いているセッションが他に居ないときだけ** `allow` — worktree は一意ではなく、
  同じ所を複数のセッションが開くことが実在するため。deny の範囲は setup#150 のままで、
  別セッションのものは変わらず止まる
