# LTC P2P Discord Bot — セットアップガイド（Windows 11）

本ガイドでは、Windows 11（VPS / 実機）上でボットを動かすための全手順を説明します。

---

## 1. 前提条件

- **Python 3.10 以上** がインストール済み
- **Git** がインストール済み（任意）
- **管理者権限** でPowerShellを開けること

---

## 2. PostgreSQL のセットアップ

### 方法A: Docker を使う（推奨）

#### 2-1. Docker Desktop のインストール

1. https://www.docker.com/products/docker-desktop/ からインストーラーをダウンロード
2. インストール後、再起動
3. PowerShellで確認:
   ```powershell
   docker --version
   ```

#### 2-2. PostgreSQL コンテナの起動

```powershell
docker run -d `
  --name ltcbot-postgres `
  -e POSTGRES_USER=botuser `
  -e POSTGRES_PASSWORD=password `
  -e POSTGRES_DB=botdb `
  -p 5432:5432 `
  --restart unless-stopped `
  postgres:16
```

動作確認:
```powershell
docker ps
# ltcbot-postgres が Up で表示されればOK
```

#### コンテナの管理コマンド
```powershell
docker stop ltcbot-postgres     # 停止
docker start ltcbot-postgres    # 起動
docker logs ltcbot-postgres     # ログ確認
```

### 方法B: PostgreSQL を直接インストール

1. https://www.postgresql.org/download/windows/ からインストーラーをダウンロード
2. インストール中にユーザー名(`botuser`)、パスワード(`password`)、ポート(`5432`)を設定
3. pgAdmin または psql で `botdb` データベースを作成:
   ```sql
   CREATE DATABASE botdb;
   ```

---

## 3. Electrum-LTC のセットアップ

### 3-1. Electrum-LTC のダウンロード

1. https://electrum-ltc.org/ から Windows 版をダウンロード・インストール
2. **初回起動で新しいウォレットを作成（またはシードから復元）**
3. ウォレットファイルの場所を確認（通常: `%APPDATA%\Electrum-LTC\wallets\default_wallet`）

### 3-2. RPC設定

Electrum-LTC を閉じた状態で、PowerShell から以下を実行:

```powershell
# Electrum-LTC のパスを通す（インストール先に合わせて変更）
# 通常: "C:\Program Files (x86)\Electrum-LTC\electrum-ltc.exe"

# RPC設定
electrum-ltc setconfig rpcuser rpc_user
electrum-ltc setconfig rpcpassword rpc_password
electrum-ltc setconfig rpcport 7777
```

> **重要:** ここで設定する `rpcuser`, `rpcpassword`, `rpcport` は `.env` ファイルの
> `ELECTRUM_RPC_USER`, `ELECTRUM_RPC_PASSWORD`, `ELECTRUM_RPC_PORT` と完全に一致させてください。

### 3-3. デーモンモードで起動

> ⚠️ Windows では `-d` フラグ（バックグラウンド化）は使えません。
> デーモンはフォアグラウンドで実行し、**そのターミナルは開いたままにしてください**。

**ターミナル①**（Electrum-LTC デーモン用、開いたままにする）:
```powershell
electrum-ltc daemon
```

**ターミナル②**（別のPowerShellを開いて実行）:
```powershell
# ウォレットをロード（デフォルトウォレットの場合）
electrum-ltc daemon load_wallet

# 接続確認
electrum-ltc getinfo
```

正常であれば、サーバー接続情報やブロック高が表示されます。

### 3-4. RPC動作テスト

```powershell
# アドレス一覧を取得してRPCが機能してるか確認
electrum-ltc listaddresses
```

JSON形式でアドレスが返ってくれば成功です。

### ⚠️ デーモンの自動起動設定（VPS向け）

VPS再起動時にもデーモンが起動するように、タスクスケジューラに登録することを推奨します:

1. 「タスクスケジューラ」を開く
2. 「基本タスクの作成」→ 名前: `Electrum-LTC Daemon`
3. トリガー: 「ログオン時」
4. 操作: 「プログラムの開始」
   - プログラム: `electrum-ltc.exe` のフルパス
   - 引数: `daemon`（※ `-d` は不要）
5. 保存後、同様にもう1つ作成:
   - 引数: `daemon load_wallet`
   - トリガーに「遅延: 10秒」を設定

---

## 4. Python 環境のセットアップ

```powershell
# プロジェクトディレクトリに移動
cd C:\Users\さくたろう\Desktop\lbot

# 仮想環境を作成（初回のみ）
python -m venv venv

# 仮想環境を有効化
.\venv\Scripts\Activate.ps1

# 依存パッケージをインストール
pip install -r requirements.txt
```

---

## 5. `.env` ファイルの設定

プロジェクトルートの `.env` ファイルを編集:

```env
# Discord設定
DISCORD_BOT_TOKEN="あなたのボットトークン"
GUILD_ID="あなたのサーバーID"
ADMIN_ROLE_ID="管理者ロールID"
TICKET_CATEGORY_ID="チケット用カテゴリID"

# データベース設定 (PostgreSQL)
DATABASE_URL="postgresql+asyncpg://botuser:password@localhost:5432/botdb"

# Electrum-LTC デーモン RPC設定
LTC_MASTER_XKEY="あなたのマスター秘密鍵"
ELECTRUM_RPC_USER="rpc_user"
ELECTRUM_RPC_PASSWORD="rpc_password"
ELECTRUM_RPC_HOST="127.0.0.1"
ELECTRUM_RPC_PORT="7777"

# 価格取得API
MARKET_PRICE_API="https://api.kraken.com/0/public/Ticker?pair=LTCJPY"
```

> ⚠️ `DISCORD_BOT_TOKEN` と `LTC_MASTER_XKEY` は**絶対に外部に漏らさないでください**。

---

## 6. データベースの初期化

ボットの初回起動時に自動的にテーブルが作成されます。
既存のテーブルをリセットしたい場合:

```powershell
# Docker経由でpsqlに入る
docker exec -it ltcbot-postgres psql -U botuser -d botdb

# テーブルをすべて削除
DROP TABLE IF EXISTS system_config, transactions, orders, ads, users CASCADE;

# 終了
\q
```

---

## 7. ボットの起動

```powershell
cd C:\Users\さくたろう\Desktop\lbot
.\venv\Scripts\Activate.ps1
py main.py
```

正常起動時の出力例:
```
Database initialized.
Loaded extension cogs.admin
Loaded extension cogs.dashboard
Loaded extension cogs.market
Loaded extension cogs.ticket
Synced commands to guild XXXXXXXXXX.
Logged in as BotName (ID: XXXXXXXXXX)
Bot is ready and operational.
```

---

## 8. Discord 上での初期設定

1. **ダッシュボード設置:** 管理者が任意のチャンネルで `/setup_dashboard` を実行
2. **マーケット設置:** P2P取引用のチャンネルで `/setup_market` を実行
3. **手数料確認:** `/admin_fee` で累計手数料を確認

---

## 9. 起動チェックリスト

ボットを起動する前に、以下が全て揃っているか確認してください:

| 項目 | 確認方法 |
|------|----------|
| Docker / PostgreSQL が起動中 | `docker ps` で `ltcbot-postgres` が Up |
| Electrum-LTC デーモンが起動中 | `electrum-ltc getinfo` でレスポンスあり |
| `.env` が正しく設定済み | 各値がサービスの設定と一致 |
| Python 仮想環境が有効 | プロンプトに `(venv)` が表示 |
| テーブルが存在する | ボット起動時に `Database initialized.` と表示 |

---

## 10. トラブルシューティング

| 症状 | 原因と対策 |
|------|-----------|
| `relation "users" does not exist` | `main.py` でテーブル自動作成が走っていない。ボットを再起動。それでもダメなら手動でテーブルを DROP して再起動 |
| `RPC Connection Error` + `text/html` | Electrum-LTC をデーモンモード(`daemon -d`)で起動していない、またはウォレットがロードされていない |
| `入金アドレスの生成に失敗しました` | Electrum-LTC デーモンが起動していないか、RPC設定(`rpcuser`/`rpcpassword`)が `.env` と一致していない |
| `アプリケーションが応答しませんでした` | Discordの3秒タイムアウト。通常は `defer()` で対応済み。DB接続が遅い場合はネットワークを確認 |
| `CommandNotFound: '依頼メニュー'` | 他のボットまたは古いバージョンの残骸。無視してOK |
