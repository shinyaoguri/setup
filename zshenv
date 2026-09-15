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
export PATH

# Gyazo Upload API のトークンの「参照」だけを置く (値は持たせない)。
# 使う側: secret-read "$GYAZO_TOKEN_REF" — 1Password がロックされていても読めるよう
# Keychain をキャッシュに使う。手順は gyazo-capture スキル、線引きは secret-cache-allowlist
export GYAZO_TOKEN_REF="op://Automation/Gyazo API/credential"

# mokume のエージェントが push と PR 作成に使う GitHub App の秘密鍵を、**読むコマンド**
# として置く (値は持たせない)。mokume の scripts/gh-app-token.sh が eval して PEM を得る。
# 参照だけでなくコマンドの形なのは、あちらが受け取る口が MOKUME_APP_PRIVATE_KEY_CMD
# だから (mokume の AGENTS.md「エージェントの identity」)。
#
# **こちらが持つのは、あちらが持てないからである。** mokume は「秘密鍵の中身も在処も
# リポジトリに書かない」を規約にしていて、鍵の渡し方だけが手で揃える設定として外に
# 残る。その置き場がここになる。
#
# zshrc ではなくここに置くのは GYAZO_TOKEN_REF と同じ理由 — 無人セッション (hook・
# scheduled task) が非対話で zshrc を読まず、空のまま「鍵が無い」で止まるため。
# 承認が要る PR は App identity でしか作れないので (mokume の ADR-0007)、ここが空だと
# エージェントは PR を作れない。
#
# コマンドは絶対パスで書く。上で PATH へ足した分は、Claude デスクトップアプリのセッションでは
# 残らないことがある — アプリは起動した時点の PATH を持ち続け、Bash ツールのシェル
# スナップショットがこのファイルの後でそれを書き戻す (セットアップ前に起動していたアプリで
# 実際に踏んだ。#154)。書き戻されるのは PATH だけで変数は残るので、裸の secret-read だと
# 「変数はあるのにコマンドが無い」になり、エージェントは 1Password の承認待ちへ落ちる。
export MOKUME_APP_PRIVATE_KEY_CMD="${(q)_setup_root}/bin/secret-read \"op://Automation/mokume-agent/mokume-agent.2026-08-26.private-key.pem\""
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
