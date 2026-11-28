# ログインシェルでだけ読まれるファイル。読まれる順は
#   ~/.zshenv → /etc/zprofile → ここ → (対話なら) ~/.zshrc
#
# ここに置くのは 1 つだけ: **/etc/zprofile の path_helper が並べ替えた PATH を戻す**こと。
#
# macOS の /etc/zprofile は path_helper を呼び、それまでの PATH を「システムのパス
# (/usr/bin など) を先頭、残りを後ろ」に組み直す。Homebrew の PATH は zshenv が通して
# いるが (非対話シェルへ届かせるため。#170)、ログインシェルではその後で後ろへ回され、
# `git` のようにシステムにも同名がある道具は Apple のものが先に引かれていた。
# Terminal.app が開くのはログインシェルなので、人が打つ端末と無人セッションで同じ名前が
# 別の実行ファイルを指すことになる (issue #197)。
#
# zshenv 側は消さない。非ログインのシェル (hook・cron・`zsh -c`) はここを読まない。
# path は zshenv が `typeset -U` にしているので、入れ直しても重複せず先頭へ移るだけ。
# Homebrew 公式が `brew shellenv` の置き場として案内しているのもこのファイルである。
typeset -U path
path=("/opt/homebrew/bin" "/opt/homebrew/sbin" $path)
export PATH
