#!/bin/bash

###########
#Xcodeの確認
###########
echo -e "-----\nXcodeの存在確認"
if type "xcode-select" >/dev/null 2>&1; then
  echo -e "-> ✅ Xcode already exist"
else
  echo -e ">>> Xcode was not exists\n>>> Please install Xcode from AppStore."
  return 2> /dev/null
  exit
fi

###########
# Homebrew
###########
echo -e "-----\nHomebrewの存在確認"
if type "brew" >/dev/null 2>&1; then
  echo -e "-> ✅ brew already exist"
else
  echo -e ">>> Homebrew was not exist"
  bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  exit
fi
