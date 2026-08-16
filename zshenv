# zsh のすべての起動 (対話・非対話・スクリプト) で読まれるファイル。
#
# なぜ zshrc と分けるか: Claude Code の hook・scheduled task・cron から走る zsh は
# 非対話なので zshrc を読まない。秘密の参照 (GYAZO_TOKEN_REF) をあちらに置いていたため、
# 無人セッションでは空になり、値が引けずに黙って止まっていた。
#
# ここに置くのは「人が打つとき以外にも要るもの」だけに絞る。プロンプト・alias・補完・
# oh-my-zsh のような対話シェル向けの設定は zshrc のまま。SSH_AUTH_SOCK も zshrc に残す
# (ここへ移すと、SSH 越しのシェルで forwarding された agent を上書きして壊す)。

# setup リポジトリの実行ファイル (secret-read など) を PATH へ。
# このファイルの実体 (symlink 解決後) が置かれたディレクトリ = リポジトリの根。
_setup_root="${${(%):-%N}:A:h}"
typeset -U path
path=("$_setup_root/bin" $path)
unset _setup_root
export PATH

# Gyazo Upload API のトークンの「参照」だけを置く (値は持たせない)。
# 使う側: secret-read "$GYAZO_TOKEN_REF" — 1Password がロックされていても読めるよう
# Keychain をキャッシュに使う。手順は gyazo-capture スキル、線引きは secret-cache-allowlist
export GYAZO_TOKEN_REF="op://Automation/Gyazo API/credential"
