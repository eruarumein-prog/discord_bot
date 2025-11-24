# Render セットアップ詳細手順 - 完全版

## 📋 事前準備

### ⚠️ 重要：フォルダ名について

**フォルダ名は必ず英語のみにしてください。日本語が含まれているとエラーが発生します。**

**現在のフォルダ構成**（変更が必要）:
- ❌ `C:\Users\81809\Desktop\bot_folder\discord_bot` （親フォルダ名を英語に変更する必要がある場合）

**推奨フォルダ構成**（変更後）:
- ✅ `C:\Users\81809\Desktop\discord_bot` （英語のみ、推奨）
- ✅ `C:\Users\81809\Desktop\my_discord_bot` （英語のみ）

**フォルダ名を変更する方法**:

**方法1: ボットフォルダを直接デスクトップに移動（推奨）**
1. `discord_bot`フォルダを右クリック → 「切り取り」
2. `Desktop`フォルダを開く
3. `Desktop`フォルダ内で右クリック → 「貼り付け」
4. これで `C:\Users\81809\Desktop\discord_bot` になります

**方法2: 親フォルダ名を変更**
1. 親フォルダ（例: `bot_folder`）を右クリック → 「名前の変更」
2. 英語名に変更（例: `discord_bot_folder`）
3. これで `C:\Users\81809\Desktop\discord_bot_folder\discord_bot` になります

**方法3: 両方を変更**
1. 親フォルダを削除または名前変更
2. `discord_bot`フォルダを`Desktop`に直接移動

### 必要なファイル
- `main.py` - ボットのメインファイル
- `requirements.txt` - 必要なパッケージ一覧
- `database.py` - データベースファイル
- `cogs/` フォルダ - コグファイル
- `.env` - 環境変数（GitHubにはアップロードしない）

---

## 📝 ステップ1: GitHubにコードをアップロード

### 1-1. GitHub Desktopを使う場合

1. **GitHub Desktopをダウンロード**
   - https://desktop.github.com にアクセス
   - 「Download for Windows」をクリック
   - インストール

2. **GitHub Desktopを起動**
   - 「Sign in to GitHub.com」をクリック
   - GitHubアカウントでログイン

3. **リポジトリを追加**
   - 「File」→「Add Local Repository」をクリック
   - 「Choose...」をクリック
   - ボットのフォルダを選択（例: `C:\Users\81809\Desktop\discord_bot`）
   - ⚠️ **重要**: フォルダ名は必ず英語のみにしてください（日本語が含まれているとエラーになります）
   - 「Add repository」をクリック

4. **GitHubにアップロード**
   - 左下の「Publish repository」をクリック
   - **Repository name**: `discord-bot`（何でもOK）
   - **Description**: （空欄でもOK）
   - 「Keep this code private」のチェックを外す（公開リポジトリにする）
   - 「Publish repository」をクリック

### 1-2. ブラウザで直接アップロードする場合

1. **GitHubにログイン**
   - https://github.com にアクセス
   - ログイン

2. **新しいリポジトリを作成**
   - 右上の「+」→「New repository」をクリック
   - **Repository name**: `discord-bot`
   - **Description**: （空欄でもOK）
   - 「Public」を選択
   - 「Add a README file」のチェックを**外す**
   - 「Create repository」をクリック

3. **ファイルをアップロード**
   - 「uploading an existing file」をクリック
   - ボットのフォルダ内の**必要なファイルのみ**をドラッグ&ドロップ：
     - `main.py`
     - `requirements.txt`
     - `database.py`
     - `cogs/` フォルダ（中身も含む、`.bak`ファイルは除く）
     - `.gitignore`（作成する、後述）
   - ⚠️ **注意**: 
     - フォルダ名が英語のみであることを確認してください
     - `.bak`ファイル、`.bat`ファイル、`data/`フォルダ、`logs/`フォルダはアップロードしないでください
   - 「Commit changes」をクリック

### 1-3. .gitignoreファイルを作成（重要）

`.env`ファイルをGitHubにアップロードしないようにするため：

1. ボットのフォルダ（英語名のフォルダ）に `.gitignore` ファイルを作成
2. 以下の内容を記入：
   ```
   .env
   __pycache__/
   *.pyc
   *.db
   logs/
   .env.local
   ```
3. ⚠️ **確認**: フォルダ名が英語のみであることを確認してください

---

## 📝 ステップ2: Renderでアカウント作成

1. **Renderにアクセス**
   - https://render.com にアクセス

2. **アカウント作成**
   - 「Get Started for Free」をクリック
   - 「Sign up with GitHub」をクリック
   - GitHubアカウントでログイン
   - 認証を許可

---

## 📝 ステップ3: プロジェクトを作成

1. **ダッシュボードを開く**
   - ログイン後、ダッシュボードが表示されます

2. **新しいWeb Serviceを作成**
   - 「New」ボタンをクリック
   - 「Web Service」を選択

3. **GitHubリポジトリを連携**
   - 「Connect account」をクリック（初回のみ）
   - GitHubアカウントを選択
   - 「Authorize render」をクリック
   - 作成したリポジトリ（`discord-bot`）を選択
   - 「Connect」をクリック

---

## 📝 ステップ4: 設定を入力

### 基本設定

以下の項目を入力：

| 項目 | 入力内容 | 説明 |
|------|---------|------|
| **Name** | `discord-bot` | 何でもOK、識別用 |
| **Region** | `Singapore` | 日本に近い地域を選択 |
| **Branch** | `main` | そのまま（GitHubのブランチ名） |
| **Root Directory** | （空欄） | そのまま |
| **Runtime** | `Python 3` | 自動検出される |
| **Build Command** | `pip install -r requirements.txt` | そのまま入力 |
| **Start Command** | `python main.py` | そのまま入力 |

### 環境変数の設定

1. **「Advanced」をクリック**
2. **「Add Environment Variable」をクリック**
3. 以下を追加：

   | Key | Value |
   |-----|-------|
   | `DISCORD_TOKEN` | あなたのDiscordボットトークン |

4. **「Add」をクリック**

### プランの選択

1. **「Create Web Service」の前に、プランを確認**
2. **「Starter」プラン（月$7）を選択**
   - スリープしない
   - 24時間稼働
3. **「Create Web Service」をクリック**

---

## 📝 ステップ5: デプロイの確認

1. **デプロイが開始されます**
   - 数分かかります
   - ログが表示されます

2. **ログを確認**
   - 「Logs」タブでログを確認
   - エラーがないか確認

3. **成功の確認**
   - 「Live」と表示されれば成功
   - Discordでボットがオンラインになっているか確認

---

## 🔧 よくあるエラーと解決方法

### エラー1: ModuleNotFoundError

**原因**: `requirements.txt`にパッケージが不足

**解決方法**:
1. ローカルの`requirements.txt`を確認
2. 不足しているパッケージを追加
3. GitHubにプッシュ
4. Renderが自動的に再デプロイ

**現在のrequirements.txtの内容**:
```
discord.py>=2.3.0
python-dotenv>=1.0.0
```

### エラー2: Token not found

**原因**: 環境変数`DISCORD_TOKEN`が設定されていない

**解決方法**:
1. Renderの「Environment」タブを確認
2. `DISCORD_TOKEN`が正しく設定されているか確認
3. トークンが正しいか確認

### エラー3: database file not found

**原因**: データベースファイルが存在しない

**解決方法**:
- 初回起動時に自動的に作成されるので問題なし
- エラーが出る場合は、`database.py`のパスを確認

---

## 📁 アップロードするファイル一覧

### 必須ファイル（実際に存在するファイルのみ）

**ルートフォルダ**:
- ✅ `main.py` - ボットのメインファイル
- ✅ `requirements.txt` - 必要なパッケージ
- ✅ `database.py` - データベース管理

**cogs/フォルダ**（すべての`.py`ファイル、実際のフォルダ構成順）:
- ✅ `cogs/__init__.py`
- ✅ `cogs/embeddisplay.py`
- ✅ `cogs/rolemanager.py`
- ✅ `cogs/serverdm.py`
- ✅ `cogs/ticketmanager.py`
- ✅ `cogs/vcmanager.py`

**注意**: 
- `cogs/`フォルダ内のすべての`.py`ファイルをアップロードしてください
- `.bak`ファイル（バックアップ）はアップロードしないでください

### アップロードしないファイル
- ❌ `.env` - 環境変数（GitHubにアップロードしない）
- ❌ `*.db` - データベースファイル（自動生成される）
- ❌ `*.bak` - バックアップファイル（`vcmanager.py.bak`、`database.py.bak`など）
- ❌ `*.bat` - Windows用バッチファイル（`Start-Bot.bat`、`Stop-Bot.bat`など）
- ❌ `__pycache__/` - Pythonキャッシュ
- ❌ `logs/` - ログファイル
- ❌ `data/` - データフォルダ（自動生成される）
- ❌ `Render詳細手順.md` - 説明書（アップロード不要）

---

## 🔄 コードを更新する方法

1. **ローカルでコードを編集**
   - ファイルを編集

2. **GitHub Desktopでアップロード**
   - 左下の「Commit to main」をクリック
   - コミットメッセージを入力（例: "機能追加"）
   - 「Commit to main」をクリック
   - 「Push origin」をクリック

3. **Renderが自動的に再デプロイ**
   - 数分かかります
   - 「Logs」タブで確認

---

## 💰 料金について

- **Starterプラン**: 月$7（約1,050円）
  - スリープなし
  - 24時間稼働
  - 512MB RAM
  - 0.1 CPU

- **Freeプラン**: 無料
  - スリープあり（15分でスリープ）
  - 24時間稼働不可

**24時間稼働が必須ならStarterプランが必要です**

---

## ✅ チェックリスト

セットアップ前に確認：

- [ ] GitHubアカウントを作成
- [ ] ボットのコードをGitHubにアップロード
- [ ] `.gitignore`ファイルを作成（`.env`を除外）
- [ ] Renderアカウントを作成
- [ ] GitHubリポジトリをRenderに連携
- [ ] 環境変数`DISCORD_TOKEN`を設定
- [ ] Starterプラン（月$7）を選択
- [ ] デプロイが成功したか確認
- [ ] Discordでボットがオンラインになっているか確認

---

## 🎉 完了！

これでDiscordボットが24時間稼働します！
