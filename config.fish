# M1チップのMacの場合のHomebrewの設定
set -x PATH /opt/homebrew/bin $PATH
set -x PATH /usr/local/bin $PATH

#エイリアス
alias rmt="trash -F"
alias rm="rm -i"
alias g="git"
alias relogin="exec fish"
alias reload="source ~/.config/fish/config.fish"
alias t="tmuximum"
alias '\;s'="ls"
clear

# anyenv
if test -d $HOME/.anyenv
  #anyenv
  set -x PATH $HOME/.anyenv/bin $PATH
  anyenv init - fish | source
end