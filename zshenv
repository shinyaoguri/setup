# zsh のすべての起動 (対話・非対話・スクリプト) で読まれるファイル。
#
# なぜ zshrc と分けるか: Claude Code の hook・scheduled task・cron から走る zsh は
# 非対話なので zshrc を読まない。秘密の参照 (GYAZO_TOKEN_REF) をあちらに置いていたため、
# 無人セッションでは空になり、値が引けずに黙って止まっていた。
#
# ここに置くのは「人が打つとき以外にも要るもの」だけに絞る。プロンプト・alias・補完・
# oh-my-zsh のような対話シェル向けの設定は zshrc のまま。

# setup リポジトリの実行ファイル (secret-read など) を PATH へ。
# このファイルの実体 (symlink 解決後) が置かれたディレクトリ = リポジトリの根。
_setup_root="${${(%):-%N}:A:h}"
typeset -U path
path=("$_setup_root/bin" $path)

# Homebrew も**ここ**で通す。zshrc (対話シェル専用) にしか無いと、hook・cron・launchd
# から走る zsh に届かない。secret-read の refresh_if_stale は `command -v op` で抜けるので、
# op が引けない = キャッシュの自動ローテートが一度も走らない状態になっていた
# (CLAUDE.md の「24 時間ごとに取り直される」が無人セッションで成立していなかった。#170)。
# sbin も通す — sbin にしか入らない formula がある。
# `brew shellenv` を eval しないのは、非対話シェルすべてでプロセスを 1 つ余計に起動する
# ことになるため。ここで要るのは PATH だけ。
path=("/opt/homebrew/bin" "/opt/homebrew/sbin" $path)
export PATH

# Gyazo Upload API のトークンの「参照」だけを置く (値は持たせない)。
# 使う側: secret-read "$GYAZO_TOKEN_REF" — 1Password がロックされていても読めるよう
# Keychain をキャッシュに使う。手順は gyazo-capture スキル、線引きは secret-cache-allowlist
#
# 参照は `op://` ではなく secret-read の**役割名**で渡す (issue #289)。どの項目を充てるかは
# 各自の 1Password のタグ (secret-read/gyazo-token) で決まるので、保管庫や項目の名前を
# 公開リポジトリに書かずに済み、他の人の環境でもそのまま意味を持つ。
#
# **参照の literal はここ 1 つ。** 下の MOKUME_GYAZO_TOKEN_CMD もこの変数を読む形にして
# あり、参照を書き換えるときに直す場所が 2 つに割れないようにしている。
export GYAZO_TOKEN_REF="gyazo-token"

# Gyazo のトークンも、mokume へは**読むコマンド**として渡す (受け取る口が
# MOKUME_GYAZO_TOKEN_CMD で、`bash -c` / `eval` で実行される)。上の GYAZO_TOKEN_REF は
# 参照の形なので、そのままでは mokume の口に嵌まらない — スキルと `make example-shots` は
# コマンドしか受け取らない。
#
# **無いと、失効したトークンと同じ 401 になる。** 空の access_token にも Gyazo は
# `You are not authorized.` を返すので、未設定は「トークンが死んだ」に見える (#159 で
# 実際に誤診し、トークンを作り直させた)。
#
# コマンドは絶対パスで書く。上で PATH へ足した分は、Claude デスクトップアプリのセッションでは
# 残らないことがある — アプリは起動した時点の PATH を持ち続け、Bash ツールのシェル
# スナップショットがこのファイルの後でそれを書き戻す (セットアップ前に起動していたアプリで
# 実際に踏んだ。#154)。書き戻されるのは PATH だけで変数は残るので、裸の secret-read だと
# 「変数はあるのにコマンドが無い」になる。参照を直書きしない理由は上の GYAZO_TOKEN_REF と同じ。
export MOKUME_GYAZO_TOKEN_CMD="${(q)_setup_root}/bin/secret-read \"\$GYAZO_TOKEN_REF\""
unset _setup_root

# SSH agent は Secretive (Secure Enclave)。ssh-keygen -Y sign は SSH_AUTH_SOCK から
# agent を引くので、コミット署名にもこの変数が要る。zshrc に置くと非対話シェルが
# 読まないため、無人セッションでだけ署名が落ちる (GYAZO_TOKEN_REF と同じ轍)。
#
# ただし SSH 越しにこのマシンへ入っているときは触らない。forwarding された agent の
# ソケットを指しているので、上書きすると相手の鍵が使えなくなる。
if [[ -z "$SSH_CONNECTION" ]]; then
  export SSH_AUTH_SOCK=~/Library/Containers/com.maxgoedjen.Secretive.SecretAgent/Data/socket.ssh
fi
