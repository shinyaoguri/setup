#!/bin/zsh
#######################################
# MIT License
# Copyright (c) 2022 Shinya Oguri
# https://github.com/shinyaoguri/setup
#######################################

######## 
# Xcode
########
echo -e "-----\nCheck Xcode"
if type "xcode-select" >/dev/null 2>&1; then
  echo -e "✅ Xcode already exist"
else
  echo -e "🙅 Xcode was not exist\n>>> Please install Xcode from AppStore."
  open "https://apps.apple.com/jp/app/xcode/id497799835"
  return 2> /dev/null
  exit
fi

###########
# Homebrew
###########
echo -e "-----\nCheck Homebrew"
if [ -f ~/.zshrc ]; then
  if [ "`echo $PATH | grep '/opt/homebrew/bin'`" ]; then
    echo '✅ Homebrew PATH already exist'
  else
    echo '🙅 Homebrew PATH was not exist\n...update .zshrc'
    echo 'export PATH=/opt/homebrew/bin:/usr/local/bin:$PATH' >> ~/.zshrc
    source ~/.zshrc
  fi
else
  echo '🙅 .zshrc was not exist\n...update .zshrc'
  echo 'export PATH=/opt/homebrew/bin:/usr/local/bin:$PATH' >> ~/.zshrc
  source ~/.zshrc
fi

if type "brew" >/dev/null 2>&1; then
  echo -e "✅ brew already exist"
else
  echo -e "🙅 Homebrew was not exist\nPlease install Homebrew"
  open "https://brew.sh"
  exit
fi

##########
# Ansible
##########
echo -e "-----\nCheck Ansible"
if type "ansible" >/dev/null 2>&1; then
  echo -e "✅ Ansible already exist"
else
  echo -e "🙅 ansible was not installed"
  brew install ansible
fi


echo -e "-----\nAnsible Deploy"
cd ~
curl -O -sfSL https://raw.githubusercontent.com/shinyaoguri/setup/main/ansible_arm64_mac.yml

if [ -f ~/ansible_arm64_mac.yml ]; then
  ansible-galaxy collection install community.general
  ansible-playbook ansible_arm64_mac.yml --ask-become-pass
  rm ansible_arm64_mac.yml
else
  echo -e "🙅 ansible-playbook was not downloaded"
fi
