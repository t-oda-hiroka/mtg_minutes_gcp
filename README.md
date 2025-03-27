# 議事録作成アプリケーション

音声ファイルから自動で議事録を作成するWebアプリケーションです。OpenAIのWhisper APIを使用して音声認識を行い、GPT-4を使用して議事録を生成します。

## 機能

- 音声ファイルのアップロード（対応形式: MP3, WAV, M4A, MP4, MPEG, MPGA, WEBM）
- 音声認識による文字起こし
- AIによる議事録の自動生成
- 会議の概要や重要な用語の事前入力機能

## 必要な環境

- Python 3.11以上
- FastAPI
- OpenAI API Key
- Google Cloud Platform アカウント（デプロイ時）

## セットアップ

1. 必要なパッケージのインストール:
```bash
pip install -r requirements.txt
```

2. 環境変数の設定:
`.env`ファイルをプロジェクトのルートディレクトリに作成し、以下の内容を設定します：
```
OPENAI_API_KEY=your-api-key-here
```

## ローカルでの実行

```bash
uvicorn main:app --reload
```

アプリケーションは http://127.0.0.1:8000 で起動します。

## Google Cloud Platformの初期設定

1. Google Cloudアカウントの作成
   - [Google Cloud Console](https://console.cloud.google.com/)にアクセス
   - Googleアカウントでログイン
   - 国と利用規約に同意
   - 支払い情報を設定（クレジットカード必須）

2. プロジェクトの作成
   - コンソール上部の「プロジェクトの選択」をクリック
   - 「新しいプロジェクト」をクリック
   - プロジェクト名を入力（例：`mtg-minutes`）
   - 「作成」をクリック

3. 必要なAPIの有効化
   - 左側メニューから「APIとサービス」→「ライブラリ」を選択
   - 以下のAPIを検索して有効化：
     - Cloud Run API
     - Cloud Build API
     - Secret Manager API
     - Container Registry API

4. Google Cloud SDKのインストール
   - [公式ページ](https://cloud.google.com/sdk/docs/install)からSDKをダウンロード
   - インストーラーを実行
   - ターミナルで初期化を実行：
   ```bash
   gcloud init
   ```
   - プロンプトに従ってGoogleアカウントにログイン
   - 作成したプロジェクトを選択

5. デフォルトのリージョンを設定：
```bash
gcloud config set compute/region asia-northeast2
```

## Google Drive API 認証情報の設定

1. Google Cloud Consoleで認証情報を作成
   - 左側メニューから「APIとサービス」→「認証情報」を選択
   - 「認証情報を作成」→「OAuth クライアント ID」を選択
   - アプリケーションの種類を「ウェブアプリケーション」に設定
   - 名前を入力（例：`MTG Minutes App`）
   - 「承認済みのリダイレクトURI」に以下を追加
     - ローカル環境: `http://localhost:8000/oauth/callback`
     - **注意**: デプロイ環境のURLは、デプロイ後に生成される実際のURLを使用します（後述）
   - 「作成」をクリックし、クライアントIDとシークレットを保存

2. OAuth同意画面の設定
   - 左側メニューから「APIとサービス」→「OAuth同意画面」を選択
   - ユーザータイプを「外部」に設定
   - アプリケーション名、サポートメール、デベロッパーの連絡先情報を入力
   - スコープに「.../auth/documents」と「.../auth/drive.file」を追加
   - 「テストユーザー」セクションで、アプリへのアクセスを許可するユーザーのメールアドレスを追加

3. サービスアカウントの作成（バックエンド処理用）
   - 左側メニューから「IAM と管理」→「サービスアカウント」を選択
   - 「サービスアカウントを作成」をクリック
   - 名前と説明を入力（例：`mtg-minutes-drive-api`）
   - 役割を「編集者」に設定
   - 「キーの作成」→「JSON」を選択し、キーファイルをダウンロード
   - ダウンロードしたJSONファイルをプロジェクトルートディレクトリに配置し、`.env`ファイルで参照

## Google Cloud Runへのデプロイ

### 初回デプロイ手順（ステップバイステップ）

1. プロジェクトIDの設定:
```bash
# プロジェクトIDの確認
gcloud projects list

# プロジェクトの設定
gcloud config set project your-project-id
```

2. 必要なシークレットの作成:
```bash
# Secret Managerの有効化
gcloud services enable secretmanager.googleapis.com

# APIキーをシークレットとして保存
echo -n "your-openai-api-key" | gcloud secrets create openai-api-key --data-file=-

# Google OAuth2認証情報をシークレットとして保存
echo -n "your-google-client-id" | gcloud secrets create google-client-id --data-file=-
echo -n "your-google-client-secret" | gcloud secrets create google-client-secret --data-file=-

# サービスアカウントキーファイルをシークレットとして保存
gcloud secrets create google-service-account-key --data-file=your-service-account-file.json

# Cloud RunからSecret Managerにアクセスするためのサービスアカウント権限を設定
PROJECT_ID=$(gcloud config get-value project)
PROJECT_NUMBER=$(gcloud projects describe $PROJECT_ID --format='value(projectNumber)')
gcloud projects add-iam-policy-binding $PROJECT_ID \
    --member=serviceAccount:$PROJECT_NUMBER-compute@developer.gserviceaccount.com \
    --role=roles/secretmanager.secretAccessor
```

3. Dockerイメージのビルドとデプロイ:
```bash
# Container Registryの有効化
gcloud services enable containerregistry.googleapis.com

# イメージのビルド
gcloud builds submit --tag gcr.io/your-project-id/mtg-minutes

# Cloud Runへの初回デプロイ（シンプルな構成から始める）
gcloud run deploy mtg-minutes-service \
  --image gcr.io/your-project-id/mtg-minutes \
  --platform managed \
  --region asia-northeast2 \
  --allow-unauthenticated \
  --set-secrets=OPENAI_API_KEY=openai-api-key:latest,GOOGLE_CLIENT_ID=google-client-id:latest,GOOGLE_CLIENT_SECRET=google-client-secret:latest
```

4. サービスURLの取得とリダイレクトURIの設定:
```bash
# デプロイしたサービスのURLを取得
SERVICE_URL=$(gcloud run services describe mtg-minutes-service --region asia-northeast2 --format="value(status.url)")
echo $SERVICE_URL

# リダイレクトURIを環境変数として設定
gcloud run services update mtg-minutes-service \
  --region asia-northeast2 \
  --update-env-vars=REDIRECT_URI=${SERVICE_URL}/oauth/callback

# 必要に応じてサービスアカウントJSONの設定
gcloud run services update mtg-minutes-service \
  --region asia-northeast2 \
  --update-secrets=SERVICE_ACCOUNT_JSON=google-service-account-key:latest
```

5. **重要**: Google Cloud ConsoleでOAuth設定を更新
   - Google Cloud Consoleの「APIとサービス」→「認証情報」に移動
   - 使用しているOAuthクライアントIDを編集
   - **「承認済みのリダイレクトURI」に以下を追加**:
     ```
     https://mtg-minutes-service-[your-project-id].an.run.app/oauth/callback
     ```
     （実際のURLは上記の `SERVICE_URL` で取得したものを使用してください）
   - この設定はアプリのOAuth認証に**絶対に必要**です。忘れずに行ってください。

### 2回目以降のデプロイ手順

1. コードを変更した場合（アプリケーション機能の更新）:
```bash
# 新しいDockerイメージをビルド
gcloud builds submit --tag gcr.io/your-project-id/mtg-minutes

# 既存のCloud Runサービスを更新
gcloud run deploy mtg-minutes-service \
  --image gcr.io/your-project-id/mtg-minutes \
  --platform managed \
  --region asia-northeast2
```

2. シークレットのみを更新する場合:
```bash
# 例：OpenAI APIキーを更新
echo -n "new-api-key" | gcloud secrets versions add openai-api-key --data-file=-

# サービスを再起動（シークレットの変更を反映）
gcloud run services update mtg-minutes-service \
  --region asia-northeast2
```

3. 環境変数を更新する場合:
```bash
# 例：リダイレクトURIを更新
gcloud run services update mtg-minutes-service \
  --region asia-northeast2 \
  --update-env-vars=REDIRECT_URI=https://your-new-url.an.run.app/oauth/callback
```

## APIキーの更新方法

### ローカル環境

1. `.env`ファイルを開きます
2. `OPENAI_API_KEY`の値を新しいAPIキーに更新します
3. アプリケーションを再起動します

### Cloud Run環境

1. Secret Managerの値を更新:
```bash
echo -n "new-api-key" | gcloud secrets versions add openai-api-key --data-file=-
```

2. Cloud Runサービスを更新:
```bash
# シークレットの更新を反映
gcloud run services update mtg-minutes-service \
  --region asia-northeast2
```

## トラブルシューティング

### Cloud Runデプロイ時のエラー

1. コンテナの起動エラー:
```
ERROR: Revision 'mtg-minutes-xxxxx' is not ready and cannot serve traffic.
```
- ログを確認：
```bash
gcloud logging read "resource.type=cloud_run_revision AND resource.labels.service_name=mtg-minutes-service" --limit=50
```
- 一般的な解決方法：
  - シークレットが正しく設定されているか確認
  - 必要なAPIが有効化されているか確認
  - サービスアカウントの権限が正しく設定されているか確認
  - 環境変数が正しく設定されているか確認

2. ビルドエラー:
```
ERROR: (gcloud.builds.submit) build failed
```
- Cloud Buildのログを確認：
```bash
gcloud builds list
gcloud builds log [BUILD_ID]
```

3. OAuth認証エラー:
- エラーメッセージ: `You can't sign in to this app because it doesn't comply with Google's OAuth 2.0 policy`
- 解決策:
  - Google Cloud ConsoleのAPIとサービス→認証情報で、リダイレクトURIが**正確に**設定されていることを確認してください
  - リダイレクトURIは完全に一致する必要があります（末尾のスラッシュ有無も含めて）
  - 設定例: `https://mtg-minutes-service-your-project-id.an.run.app/oauth/callback`
  - アプリケーションの環境変数 `REDIRECT_URI` と Google Cloud ConsoleのリダイレクトURI設定が一致していることを確認

4. SSL証明書エラー:
- エラーメッセージ: `この接続ではプライバシーが保護されません` または `NET::ERR_CERT_COMMON_NAME_INVALID`
- 解決策:
  - デプロイ後のCloud RunのURLを正確に使用しているか確認
  - 開発目的であれば、ブラウザの「詳細設定」→「<サイト>にアクセスする（安全ではありません）」で一時的に進むことができます

5. デプロイをやり直す場合:
```bash
# 既存のリソースを削除
gcloud run services delete mtg-minutes-service --region asia-northeast2 --quiet

# 手順に従って再デプロイ
# （上記の初回デプロイ手順を参照）
```

## 注意事項

- OpenAI APIの利用には課金が発生します
- 音声ファイルのサイズ制限にご注意ください
- APIキーは必ず環境変数として設定し、ソースコードには直接記載しないでください
- Cloud Runにデプロイする際は、適切なアクセス制御を設定してください
- Google Cloudの無料枠を超えると課金が発生します
- OAuth認証が正しく機能するためには、Google Cloud ConsoleのリダイレクトURI設定とアプリケーションの環境変数設定が完全に一致している必要があります 