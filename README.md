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

## Google Cloud Runへのデプロイ

1. プロジェクトIDの設定:
```bash
# プロジェクトIDの確認
gcloud projects list

# プロジェクトの設定
gcloud config set project your-project-id
```

2. OpenAI APIキーの設定:
```bash
# Secret Managerの有効化
gcloud services enable secretmanager.googleapis.com

# APIキーをシークレットとして保存
echo -n "your-api-key" | gcloud secrets create openai-api-key --data-file=-

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

# Cloud Runへのデプロイ
gcloud run deploy mtg-minutes \
  --image gcr.io/your-project-id/mtg-minutes \
  --platform managed \
  --region asia-northeast2 \
  --no-allow-unauthenticated \
  --set-secrets=OPENAI_API_KEY=openai-api-key:latest
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

2. Cloud Runサービスを再デプロイ:
```bash
gcloud run deploy mtg-minutes \
  --image gcr.io/your-project-id/mtg-minutes \
  --platform managed \
  --region asia-northeast2 \
  --no-allow-unauthenticated \
  --set-secrets=OPENAI_API_KEY=openai-api-key:latest
```

## トラブルシューティング

### Cloud Runデプロイ時のエラー

1. コンテナの起動エラー
```
ERROR: Revision 'mtg-minutes-xxxxx' is not ready and cannot serve traffic.
```
- ログを確認：
```bash
gcloud logging read "resource.type=cloud_run_revision AND resource.labels.service_name=mtg-minutes" --limit=50
```
- 一般的な解決方法：
  - APIキーが正しく設定されているか確認
  - 必要なAPIが有効化されているか確認
  - サービスアカウントの権限が正しく設定されているか確認

2. ビルドエラー
```
ERROR: (gcloud.builds.submit) build failed
```
- Cloud Buildのログを確認：
```bash
gcloud builds list
gcloud builds log [BUILD_ID]
```

## 注意事項

- OpenAI APIの利用には課金が発生します
- 音声ファイルのサイズ制限にご注意ください
- APIキーは必ず環境変数として設定し、ソースコードには直接記載しないでください
- Cloud Runにデプロイする際は、適切なアクセス制御を設定してください
- Google Cloudの無料枠を超えると課金が発生します 