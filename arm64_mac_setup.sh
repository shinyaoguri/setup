#!/bin/zsh

######## 
# Xcode
########
echo -e "-----\nCheck Xcode"
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
  open "https://brew.sh/index_ja"
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

cd ~
zsh -c "$(curl -O https://raw.githubusercontent.com/shinyaoguri/setup/main/ansible_arm64_mac.yml)"
ansible-playbook -i hosts ansible_arm64_mac.yml --check
rm ansible_arm64_mac.yml
