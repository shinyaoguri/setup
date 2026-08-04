---
name: gyazo-capture
description: "GUI を伴う作業 (画面・見た目・操作手順) を Issue・PR・ドキュメントに記録するとき、スクリーンショットを Gyazo にアップロードして URL を得る。Use when attaching a screenshot to an issue, PR, or doc, or when a visual record of an app, browser, or screen is needed."
---

## 手順

1. **何を撮るか決める。ウィンドウ単位が既定** (全画面は他アプリの内容・通知・個人情報が写り込む)

   ```
   gyazo_list_capturable_windows     # windowId / アプリ名 / タイトルの一覧
   gyazo_capture_and_upload_window   # windowId を指定して撮る
   ```

2. アップロードの完了を待って URL を取得する

   ```
   gyazo_get_captured_image
   ```

3. `![説明](URL)` の形で Issue・PR の本文に貼る。何の画面か・どこを見てほしいかを本文で補う (画像だけでは検索に引っかからない)

## 守ること

- **外部サービスへの送信になる**。撮る前に、画面に秘密情報・個人情報・実データが写っていないか確かめる。判断がつかなければユーザーに確認する
- **`gyazo_get_captured_image` は URL と画像そのものを返す**。画像の読み込みはトークン高コストなので、URL が目的なら**呼び出しは 1 回に留める** (待ちが必要でも連打しない)
- 完了済みのキャプチャが複数あるとまとめて返る。狙った 1 枚だけが欲しいなら、キャプチャ → 取得を 1 セットずつ行う
- **リポジトリに画像をコミットしない** (容量を圧迫する)

## うまくいかないとき

- **`No windows found`** — Gyazo Menu.app と MCP サーバーが両方起動していても返ることがある。macOS の画面収録の許可が MCP サーバーに無いのが原因。システム設定 > プライバシーとセキュリティ > 画面収録 で Gyazo を確認する。**許可の付与は GUI 操作なので代行せず、ユーザーへ依頼する**
- **取得結果が空** — アップロードが未完了。少し待ってもう一度呼ぶ (画像が返るコストがあるので連打しない)
- 動画キャプチャは非対応

## 前提

- macOS は Gyazo v9.9.0 以降 / Windows は v5.8.0 以降。MCP サーバーの登録は setup の `tasks/claude.yml` が行う (バイナリは cask の gyazo が入れる)
- 開発者向けプレビュー版のため仕様変更の可能性があり、公式サポート対象外
