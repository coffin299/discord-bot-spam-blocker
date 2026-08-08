# Discord Spam / Nuke Blocker

Discord サーバー向けのスパム対策・ハニーポット・アンチニューク BOT です。YAML で設定し、許可されていない BOT の投稿削除、罠チャンネル、破壊行為の検知と緊急ロックダウンに特化しています。

## 機能

- 許可リストに基づく BOT 投稿制御（リスト空＝全 BOT 許可）
- 招待リンク・疑わしい URL・NG ワードなどのスパム検知
- ハニーポット（罠チャンネル → KICK 既定、再加入超過で BAN）
- アンチニューク（短時間の大量キック／バン等を検知して隔離）
- 緊急ロックダウン（`nuke_lockdown_enabled: true` のときのみ、`admin_ids` が操作）

## セットアップ

### 1. 依存関係

**Windows（start.bat）:**

```bat
start.bat
```

**Mac/Linux:**

```bash
pip install -r requirements.txt
```

### 2. Discord BOT 作成

1. [Discord Developer Portal](https://discord.com/developers/applications) で Application / Bot を作成
2. Privileged Gateway Intents で以下を有効化:
   - MESSAGE CONTENT INTENT
   - SERVER MEMBERS INTENT
   - MODERATION INTENT（利用可能な場合）
3. OAuth2 で招待。推奨権限:
   - Manage Messages / Kick Members / Ban Members
   - Moderate Members（タイムアウト）
   - Manage Roles / Manage Channels（ロックダウン用）
   - View Audit Log

### 3. 設定と起動

```bash
python main.py
```

初回起動で `config.yaml` が生成されます。`bot_token` と `admin_ids` を編集してください。

## 主な管理コマンド（プレフィックス `!!!`）

管理者権限（Discord）:

- `!!!add_bot` / `!!!remove_bot` / `!!!list_bots`
- `!!!add_channel` / `!!!remove_channel` / `!!!list_channels`
- `!!!add_keyword` / `!!!remove_keyword` / `!!!list_keywords`
- `!!!reload_config` / `!!!status`

`admin_ids` のみ:

- `!!!lockdown` / `!!!unlock_lockdown` / `!!!nuke_status`

## ファイル構成

```
.
├── main.py
├── config.default.yaml
├── config.yaml              # 自動生成（Git 管理外）
├── requirements.txt
├── start.bat
├── spamblocker/
│   ├── spam/
│   ├── honeypot/
│   ├── nuke/
│   └── common/
├── data/                    # ストライク・ロックダウン状態
└── docs/                    # GitHub Pages
```

## セキュリティ

- `config.yaml` にトークンが含まれるためコミットしないでください（`.gitignore` 済み）
- ハニーポット・BAN は誤作動に注意し、まずテストサーバーで確認してください

## ライセンス

MIT（Copyright coffin299）

## ドキュメント

- サイト: https://coffin299.github.io/discord-bot-spam-blocker/
- リポジトリ: https://github.com/coffin299/discord-bot-spam-blocker
