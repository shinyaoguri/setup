# macOS Setup

ワンライナーでmacOSの環境構築を自動化

## 使い方

```bash
zsh -c "$(curl -fsSL https://raw.githubusercontent.com/shinyaoguri/setup/main/setup.sh)"
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
./arm64_mac_setup_dynamic.sh
```

## 操作方法

- `↑/↓` - 移動
- `Space` - 選択
- `Enter` - カテゴリ展開
- `c` - 決定
- `q` - 終了

## ライセンス

MIT