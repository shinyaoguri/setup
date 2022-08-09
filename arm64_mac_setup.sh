#!/bin/zsh

###########
#Xcodeの確認
###########
echo -e "-----\nXcodeの存在確認"
if type "xcode-select" >/dev/null 2>&1; then
  echo -e "✅ Xcode already exist"
else
  echo -e "🙅 Xcode was not exist\n>>> Please install Xcode from AppStore."
  return 2> /dev/null
  exit
fi

###########
# Homebrew
###########
echo -e "-----\nHomebrewの存在確認"
if [ -f ~/.zshrc ]; then
  echo '✅ .zshrc already exist'
else
  echo '🙅 .zshrc was not exist'
  echo 'export PATH=/opt/homebrew/bin:/usr/local/bin:$PATH' > ~/.zshrc
  source ~/.zshrc
fi

if type "brew" >/dev/null 2>&1; then
  echo -e "✅ brew already exist"
else
  echo -e "🙅 Homebrew was not exist\n Please install Homebrew"
  open "https://brew.sh/index_ja"
fi

##########
# Ansible
##########
echo -e "-----\nAnsibleの存在確認"
if type "ansible" >/dev/null 2>&1; then
  echo -e "✅ Ansible already exist"
else
  brew install ansible
fi

cd ~
zsh -c "$(curl -O https://raw.githubusercontent.com/shinyaoguri/setup/main/ansible_arm64_mac.yml)"
ansible-playbook -i hosts ansible_arm64_mac.yml --check
