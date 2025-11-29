# macOS Setup

ワンライナーでmacOSの環境構築を自動化

## 使い方

```bash
zsh -c "$(curl -fsSL https://raw.githubusercontent.com/shinyaoguri/setup/main/setup.zsh)"
```

以上。

## 何ができるか

対話的に選択してインストール：
- Homebrew パッケージ
- デスクトップアプリ
- App Store アプリ
- 開発環境（nodenv, rbenv等）
- macOS設定

## ローカル実行

```bash
git clone https://github.com/shinyaoguri/setup.git
cd setup
zsh setup.zsh -l
```
