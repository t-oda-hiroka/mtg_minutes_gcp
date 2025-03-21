import os
from fastapi import FastAPI, File, UploadFile, HTTPException, Body, BackgroundTasks, Depends
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.requests import Request
from pydantic import BaseModel
import tempfile
import shutil
from datetime import datetime
from openai import OpenAI
from dotenv import load_dotenv
import logging
import json
import re
import asyncio
from typing import Optional, Dict, List, Any
import google.auth
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
import uuid
import secrets
from urllib.parse import quote

# ロギングの設定
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 環境変数の読み込み
load_dotenv()

# APIキーのチェック
api_key = os.environ.get("OPENAI_API_KEY")
if not api_key:
    raise ValueError("OPENAI_API_KEYが設定されていません。")

# OpenAI クライアントの初期化
client = OpenAI(api_key=api_key)

# 静的ファイルとテンプレートの設定
app = FastAPI(title="議事録作成アプリ")
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")

# 処理状態を保存する辞書
tasks_status = {}

# OAuth関連の設定
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")
REDIRECT_URI = os.environ.get("REDIRECT_URI", "http://localhost:8000/oauth/callback")

# 認証トークンを保存する辞書（本番環境ではセッションやDBを使用）
oauth_tokens = {}
export_content = {}  # エクスポート中のコンテンツを一時的に保存

# Google API のスコープ
SCOPES = [
    'https://www.googleapis.com/auth/drive.file',
    'https://www.googleapis.com/auth/documents'
]

# リクエストモデルの定義
class EditMinutesRequest(BaseModel):
    minutes: str
    prompt: str

class ExportToDriveRequest(BaseModel):
    content: str
    title: str = "議事録"

class RegenerateMinutesRequest(BaseModel):
    raw_text: str
    meeting_summary: str = ""
    key_terms: str = ""

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

# OAuthの認証リクエストを開始する
@app.get("/oauth/google")
async def start_oauth(request: Request):
    try:
        # クエリパラメータを取得
        title = request.query_params.get("title", "議事録")
        content = request.query_params.get("content", "")
        
        # ステート（CSRF対策）
        state = secrets.token_urlsafe(16)
        
        # コンテンツIDを生成
        content_id = secrets.token_urlsafe(16)
        
        if not content:
            logger.error("エクスポートするコンテンツがありません")
            return templates.TemplateResponse(
                "error.html", 
                {"request": request, "error": "エクスポートするコンテンツがありません。"}
            )
        
        # コンテンツを一時保存
        export_content[content_id] = {
            "content": content,
            "title": title
        }
        
        logger.info(f"コンテンツを一時保存: {content_id} (長さ: {len(content)}文字)")
        
        # GOOGLE_CLIENT_ID が設定されているか確認
        if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
            logger.error("Google OAuth設定が見つかりません")
            return templates.TemplateResponse(
                "error.html", 
                {"request": request, "error": "Google OAuth設定が構成されていません。管理者に連絡してください。"}
            )
        
        # Google認証URLのスコープをURLエンコード
        encoded_scopes = quote(' '.join(SCOPES))
        
        # Google認証URLの構築
        auth_url = (
            "https://accounts.google.com/o/oauth2/v2/auth"
            f"?client_id={GOOGLE_CLIENT_ID}"
            f"&redirect_uri={REDIRECT_URI}"
            f"&response_type=code"
            f"&state={state}:{content_id}"
            f"&scope={encoded_scopes}"
            f"&access_type=offline"
            f"&include_granted_scopes=true"
            f"&prompt=consent"
        )
        
        logger.info(f"Google認証URLにリダイレクト: {auth_url}")
        
        return RedirectResponse(auth_url)
    except Exception as e:
        logger.error(f"OAuth開始処理中にエラー: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        return templates.TemplateResponse(
            "error.html", 
            {"request": request, "error": f"認証処理の開始中にエラーが発生しました: {str(e)}"}
        )

# OAuthのコールバック処理
@app.get("/oauth/callback")
async def oauth_callback(request: Request):
    try:
        # コード取得
        code = request.query_params.get("code")
        state_param = request.query_params.get("state", "")
        
        if not code:
            logger.error("認証コードが取得できませんでした")
            return templates.TemplateResponse(
                "error.html", 
                {"request": request, "error": "認証コードが取得できませんでした"}
            )
        
        # stateからcontent_idを抽出
        state_parts = state_param.split(":")
        if len(state_parts) != 2:
            logger.error(f"不正なstate値: {state_param}")
            return templates.TemplateResponse(
                "error.html", 
                {"request": request, "error": "不正なstate値です"}
            )
        
        state, content_id = state_parts
        
        # コンテンツの確認
        if content_id not in export_content:
            logger.error(f"コンテンツIDが見つかりません: {content_id}")
            return templates.TemplateResponse(
                "error.html", 
                {"request": request, "error": "エクスポートするコンテンツが見つかりません"}
            )
        
        # 認証トークンの取得
        token_url = "https://oauth2.googleapis.com/token"
        token_data = {
            "code": code,
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uri": REDIRECT_URI,
            "grant_type": "authorization_code"
        }
        
        logger.info("Googleトークンを取得中...")
        
        # リクエスト送信
        import httpx
        async with httpx.AsyncClient() as client:
            token_response = await client.post(token_url, data=token_data)
            
            if token_response.status_code != 200:
                error_msg = f"トークン取得エラー: HTTP {token_response.status_code}"
                logger.error(f"{error_msg} - {token_response.text}")
                return templates.TemplateResponse(
                    "error.html", 
                    {"request": request, "error": error_msg}
                )
            
            token_json = token_response.json()
        
        # アクセストークンの確認
        if "access_token" not in token_json:
            logger.error("アクセストークンが含まれていません")
            return templates.TemplateResponse(
                "error.html", 
                {"request": request, "error": "アクセストークンの取得に失敗しました"}
            )
        
        # 認証情報を保存
        oauth_tokens[content_id] = {
            "access_token": token_json.get("access_token"),
            "refresh_token": token_json.get("refresh_token"),
            "expires_in": token_json.get("expires_in")
        }
        
        logger.info(f"Googleトークン取得成功: {content_id}")
        
        # 保存したコンテンツを取得
        content_data = export_content.get(content_id, {})
        
        # Google Driveにエクスポート
        try:
            logger.info("Google Driveにエクスポート中...")
            document_url = await export_to_google_drive(
                content_data.get("content", ""), 
                content_data.get("title", "議事録"),
                oauth_tokens[content_id]
            )
            
            logger.info(f"エクスポート成功: {document_url}")
            
            # 使用後はクリーンアップ
            del export_content[content_id]
            
            # 成功画面を表示
            return templates.TemplateResponse(
                "export_success.html", 
                {
                    "request": request, 
                    "document_url": document_url,
                    "title": content_data.get("title", "議事録")
                }
            )
        except Exception as drive_error:
            logger.error(f"Google Driveエクスポート中のエラー: {str(drive_error)}")
            return templates.TemplateResponse(
                "error.html", 
                {"request": request, "error": f"Google Driveへのエクスポート中にエラーが発生しました: {str(drive_error)}"}
            )
        
    except Exception as e:
        logger.error(f"OAuth処理中にエラー: {str(e)}")
        return templates.TemplateResponse(
            "error.html", 
            {"request": request, "error": f"認証処理中にエラーが発生しました: {str(e)}"}
        )

# Google Driveへのエクスポート処理（OAuth認証使用）
async def export_to_google_drive(content, title, token_info):
    try:
        logger.info(f"Google Driveエクスポート開始: {title}")
        
        # HTMLのサニタイズ処理（コンテンツがHTMLの場合）
        content_text = content
        if "<" in content and ">" in content:
            content_text = html_to_plain_text(content)
        
        # アクセストークンの確認
        if "access_token" not in token_info:
            raise ValueError("アクセストークンがありません")
        
        # 認証情報の作成
        credentials = Credentials(
            token=token_info.get("access_token"),
            refresh_token=token_info.get("refresh_token"),
            client_id=GOOGLE_CLIENT_ID,
            client_secret=GOOGLE_CLIENT_SECRET,
            token_uri="https://oauth2.googleapis.com/token"
        )
        
        # Google Drive APIのサービスを構築
        drive_service = build('drive', 'v3', credentials=credentials)
        
        # Google Docs APIのサービスを構築
        docs_service = build('docs', 'v1', credentials=credentials)
        
        # 新しいGoogleドキュメントを作成
        doc_metadata = {
            'name': title,
            'mimeType': 'application/vnd.google-apps.document'
        }
        
        logger.info("Google Docsドキュメントを作成中...")
        doc = drive_service.files().create(body=doc_metadata).execute()
        document_id = doc.get('id')
        
        if not document_id:
            raise ValueError("ドキュメントIDの取得に失敗しました")
        
        logger.info(f"ドキュメント作成成功: {document_id}")
        
        # ドキュメントの内容を更新
        requests = [
            {
                'insertText': {
                    'location': {
                        'index': 1
                    },
                    'text': content_text
                }
            }
        ]
        
        logger.info("ドキュメント内容を更新中...")
        docs_service.documents().batchUpdate(
            documentId=document_id,
            body={'requests': requests}
        ).execute()
        
        logger.info("ドキュメント更新成功")
        
        # ドキュメントへのリンクを生成
        document_url = f"https://docs.google.com/document/d/{document_id}/edit"
        
        logger.info(f"エクスポート完了: {document_url}")
        
        return document_url
        
    except Exception as e:
        logger.error(f"Google Driveへのエクスポート中にエラー: {str(e)}")
        # スタックトレースもログに記録（デバッグ用）
        import traceback
        logger.error(traceback.format_exc())
        raise

# タスク状態を取得するAPIエンドポイント
@app.get("/task_status/{task_id}")
async def get_task_status(task_id: str):
    if task_id not in tasks_status:
        raise HTTPException(status_code=404, detail="タスクが見つかりません")
    
    # 完了している場合はデータを含める
    status_data = tasks_status.get(task_id, {})
    
    # 完了したタスクはキャッシュから削除（クライアントが取得した後）
    if status_data.get("completed", False) and "result" in status_data:
        # 結果をコピーして返す
        result = status_data.copy()
        # 一定の猶予を持って削除（クライアントが再度リクエストする可能性があるため）
        # 実際の運用では非同期タスクで一定時間後に削除する処理を追加
        return result
    
    return status_data

# タスク状態を更新する関数
def update_task_status(task_id: str, status: Dict[str, Any]):
    tasks_status[task_id] = status
    logger.info(f"タスク状態更新: {task_id} - {status}")

# 音声処理のバックグラウンドタスク
async def process_audio_task(task_id: str, file_path: str, meeting_summary: str, key_terms: str):
    try:
        file_ext = os.path.splitext(file_path)[1].lower()
        
        # 処理状態の初期設定
        status = {
            "step": 1,
            "progress": 10,
            "message": "音声ファイルを準備中...",
            "completed": False
        }
        update_task_status(task_id, status)
        
        # initial_promptの作成
        initial_prompt = "これは会議の録音です。"
        if meeting_summary:
            initial_prompt += f" {meeting_summary}"
        if key_terms:
            initial_prompt += f" この会議では以下の用語や人物が登場する可能性があります: {key_terms}"
        
        # ファイルサイズのチェック
        file_size = os.path.getsize(file_path)
        logger.info(f"一時ファイルを準備: {file_path} [進捗: 15%]")
        status = {
            "step": 1,
            "progress": 15,
            "message": f"ファイルサイズ: {file_size} バイト",
            "completed": False
        }
        update_task_status(task_id, status)
        await asyncio.sleep(0.5)  # 少し待機して状態更新を確実に
        
        status = {
            "step": 2,
            "progress": 20,
            "message": "音声認識を実行中...",
            "completed": False
        }
        update_task_status(task_id, status)
        await asyncio.sleep(0.5)
        
        # OpenAI APIを使用して音声認識を実行
        logger.info("音声認識を開始 [進捗: 25%]")
        status = {
            "step": 2,
            "progress": 25,
            "message": "Whisper APIを使用して音声を認識中...",
            "completed": False
        }
        update_task_status(task_id, status)
        
        # 同期的なAPIを非同期的に実行
        loop = asyncio.get_event_loop()
        # 文字起こしの実行
        transcript = None
        def run_whisper():
            # 同期的にファイルを開いて処理
            with open(file_path, "rb") as audio_file:
                return client.audio.transcriptions.create(
                    model="whisper-1",
                    file=audio_file,
                    language="ja",
                    prompt=initial_prompt
                )
                
        # 非同期的に実行
        transcript = await loop.run_in_executor(None, run_whisper)
        
        # 文字起こし完了
        raw_text = transcript.text
        logger.info(f"音声認識が完了 [進捗: 50%]")
        status = {
            "step": 2,
            "progress": 50,
            "message": f"音声認識が完了しました ({len(raw_text)}文字)",
            "completed": False
        }
        update_task_status(task_id, status)
        await asyncio.sleep(0.5)
        
        # 議事録生成ステップ
        status = {
            "step": 3,
            "progress": 60,
            "message": "議事録を生成中...",
            "completed": False
        }
        update_task_status(task_id, status)
        
        # 議事録生成のためのシステムプロンプトを強化
        system_prompt = """あなたは優秀な議事録作成者です。
与えられたテキストから以下の形式で議事録を作成してください：

# 議事録

## 開催情報
- 日時：[日時を記載]
- 議題：[議題を特定して記載]

## 参加者
[参加者が言及されている場合は記載]

## 主な議題と決定事項
[重要な議題と決定事項を箇条書きで記載]

## 詳細な議事内容
[議事の詳細を段落分けして記載]

## 次回のアクション項目
[次回までのタスクや宿題が言及されている場合は記載]

## 次回予定
[次回の予定が言及されている場合は記載]
"""

        # 会議の概要と主要用語/人物名の情報を追加
        if meeting_summary:
            system_prompt += f"\n\n会議の概要: {meeting_summary}"
        
        if key_terms:
            system_prompt += f"\n\n出現する可能性のある主要用語/人物: {key_terms}\n以上の用語や人物名が文中に出てきた場合は、正確に記録してください。"
        
        logger.info("議事録フォーマット処理中 [進捗: 75%]")
        status = {
            "step": 3,
            "progress": 75,
            "message": "GPT-4を使用して議事録を作成中...",
            "completed": False
        }
        update_task_status(task_id, status)
        
        # GPT-4の呼び出しも非同期的に実行
        def run_gpt():
            return client.chat.completions.create(
                model="gpt-4-turbo-preview",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": raw_text}
                ]
            )
        completion = await loop.run_in_executor(None, run_gpt)
        
        formatted_minutes = completion.choices[0].message.content
        logger.info("議事録の生成が完了 [進捗: 100%]")
        
        # 処理完了ステータス
        status = {
            "step": 4,
            "progress": 100,
            "message": "処理が完了しました",
            "completed": True,
            "result": {
                "raw_text": raw_text,
                "minutes": formatted_minutes
            }
        }
        update_task_status(task_id, status)
        
        # 一時ファイルの削除
        try:
            os.unlink(file_path)
            logger.info("一時ファイルを削除")
        except Exception as e:
            logger.error(f"一時ファイルの削除中にエラー: {str(e)}")
            
    except Exception as e:
        logger.error(f"音声処理中にエラー発生: {str(e)}")
        error_status = {
            "error": True,
            "message": f"エラーが発生しました: {str(e)}",
            "completed": True
        }
        update_task_status(task_id, error_status)

@app.post("/transcribe/")
async def transcribe_audio(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    meeting_summary: str = "",
    key_terms: str = ""
):
    try:
        # ファイル形式のチェック
        allowed_extensions = {'.mp3', '.mp4', '.mpeg', '.mpga', '.m4a', '.wav', '.webm'}  # サポートする形式
        file_ext = os.path.splitext(file.filename)[1].lower()
        if file_ext not in allowed_extensions:
            raise HTTPException(
                status_code=400,
                detail=f"サポートされていないファイル形式です。対応形式: {', '.join(allowed_extensions)}"
            )

        # タスクIDを生成
        task_id = str(uuid.uuid4())
        
        # 初期状態を設定
        initial_status = {
            "step": 1,
            "progress": 0,
            "message": "処理を開始しています...",
            "completed": False
        }
        update_task_status(task_id, initial_status)
        
        # 一時ファイルの作成
        with tempfile.NamedTemporaryFile(delete=False, suffix=file_ext) as temp_file:
            # ファイルの保存
            shutil.copyfileobj(file.file, temp_file)
            temp_path = temp_file.name
            
            # 非同期処理を実行するためのラッパー関数
            def process_audio_wrapper():
                asyncio.run(process_audio_task(task_id, temp_path, meeting_summary, key_terms))
            
            # バックグラウンドで処理を実行
            background_tasks.add_task(process_audio_wrapper)
            
            # タスクIDを返す
            return {"task_id": task_id}

    except Exception as e:
        logger.error(f"予期せぬエラーが発生: {str(e)}")
        raise HTTPException(status_code=500, detail=f"予期せぬエラーが発生しました: {str(e)}")

def format_minutes(text: str) -> str:
    """
    音声認識結果を議事録フォーマットに変換する
    """
    try:
        # 基本的なフォーマット
        formatted_text = "# 議事録\n\n"
        formatted_text += f"## 日時\n{datetime.now().strftime('%Y年%m月%d日 %H:%M')}\n\n"
        formatted_text += "## 議事内容\n\n"
        
        # 文章を整形
        sentences = text.replace("。", "。\n").split("\n")
        for sentence in sentences:
            if sentence.strip():
                formatted_text += f"- {sentence.strip()}\n"
        
        return formatted_text
    except Exception as e:
        logger.error(f"フォーマット中にエラーが発生: {str(e)}")
        raise 

@app.post("/edit-minutes/")
async def edit_minutes(request: EditMinutesRequest):
    try:
        logger.info("議事録修正を開始")
        
        # HTML形式の議事録をテキストに戻す簡易的な処理
        # 実際のプロジェクトではより堅牢なHTMLパーサーを使用することを推奨
        text_minutes = request.minutes.replace('<h1>', '# ').replace('</h1>', '\n\n')
        text_minutes = text_minutes.replace('<h2>', '## ').replace('</h2>', '\n\n')
        text_minutes = text_minutes.replace('<li>', '- ').replace('</li>', '\n')
        text_minutes = text_minutes.replace('<p>', '').replace('</p>', '\n\n')
        
        # GPT-4を使用して議事録を修正
        completion = client.chat.completions.create(
            model="gpt-4-turbo-preview",
            messages=[
                {"role": "system", "content": """あなたは優秀な議事録編集者です。
与えられた議事録を、ユーザーの指示に従って修正してください。
修正後の議事録全体を返してください。元の構造やフォーマットを維持しつつ、内容を改善してください。"""},
                {"role": "user", "content": f"以下の議事録を修正してください:\n\n{text_minutes}\n\n修正指示: {request.prompt}"}
            ]
        )
        
        edited_text = completion.choices[0].message.content
        
        # Markdown形式の修正済み議事録をHTMLに変換
        edited_html = edited_text.replace('# ', '<h1>').replace('\n\n', '</h1>')
        edited_html = edited_html.replace('## ', '<h2>').replace('\n\n', '</h2>')
        edited_html = edited_html.replace('- ', '<li>').replace('\n', '</li>')
        
        logger.info("議事録修正が完了")
        
        return {
            "edited_minutes": edited_html
        }
        
    except Exception as e:
        logger.error(f"議事録修正中にエラーが発生: {str(e)}")
        raise HTTPException(status_code=500, detail=f"議事録修正中にエラーが発生しました: {str(e)}") 

@app.post("/export-to-drive/")
async def export_to_drive(request: ExportToDriveRequest):
    """
    議事録をGoogle DriveにGoogleドキュメントとしてエクスポートする
    """
    try:
        logger.info(f"議事録のエクスポートを開始: {request.title}")
        
        # OAuth認証情報の有無を確認
        if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
            return {
                "success": False, 
                "error": "Google OAuth設定が構成されていません", 
                "oauth_required": True
            }
        
        # エクスポート方法の設定
        return {
            "success": True,
            "oauth_required": True,
            "message": "Google認証が必要です",
            "auth_url": f"/oauth/google?title={request.title}&content={request.content}"
        }
            
    except Exception as e:
        logger.error(f"議事録のエクスポート中にエラー: {str(e)}")
        raise HTTPException(status_code=500, detail=f"議事録のエクスポート中にエラー: {str(e)}")

def html_to_plain_text(html_content: str) -> str:
    """
    HTML形式の議事録からプレーンテキストを抽出する
    """
    # HTMLタグを削除
    text = re.sub(r'<[^>]+>', '\n', html_content)
    
    # 複数の改行を1つに
    text = re.sub(r'\n+', '\n', text)
    
    # 前後の空白を削除
    text = text.strip()
    
    return text 

@app.post("/regenerate-minutes/")
async def regenerate_minutes(request: RegenerateMinutesRequest):
    """
    編集された文字起こし内容から議事録を再生成する
    """
    try:
        logger.info("編集された文字起こしから議事録の再生成を開始")
        
        # システムプロンプトを作成
        system_prompt = """あなたは優秀な議事録作成者です。
与えられたテキストから以下の形式で議事録を作成してください：

# 議事録

## 開催情報
- 日時：[日時を記載]
- 議題：[議題を特定して記載]

## 参加者
[参加者が言及されている場合は記載]

## 主な議題と決定事項
[重要な議題と決定事項を箇条書きで記載]

## 詳細な議事内容
[議事の詳細を段落分けして記載]

## 次回のアクション項目
[次回までのタスクや宿題が言及されている場合は記載]

## 次回予定
[次回の予定が言及されている場合は記載]
"""

        # 会議の概要と主要用語/人物名の情報を追加
        if request.meeting_summary:
            system_prompt += f"\n\n会議の概要: {request.meeting_summary}"
        
        if request.key_terms:
            system_prompt += f"\n\n出現する可能性のある主要用語/人物: {request.key_terms}\n以上の用語や人物名が文中に出てきた場合は、正確に記録してください。"
        
        # GPT-4を使用して議事録を生成
        completion = client.chat.completions.create(
            model="gpt-4-turbo-preview",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": request.raw_text}
            ]
        )
        
        formatted_minutes = completion.choices[0].message.content
        
        logger.info("編集された文字起こしからの議事録生成が完了")
        
        return {
            "raw_text": request.raw_text,
            "minutes": formatted_minutes
        }
        
    except Exception as e:
        logger.error(f"議事録の再生成中にエラー: {str(e)}")
        raise HTTPException(status_code=500, detail=f"議事録の再生成中にエラーが発生しました: {str(e)}") 

@app.get("/export-info/")
async def get_export_info():
    """
    エクスポートに関する情報を取得する
    """
    try:
        # OAuth設定の確認
        if GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET:
            return {
                "success": True,
                "auth_configured": True,
                "auth_type": "oauth",
                "export_type": "ユーザーのGoogleドライブにエクスポート",
                "message": "Googleアカウントへの認証が必要です"
            }
            
        # サービスアカウントの確認
        service_account_file = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE")
        if service_account_file:
            try:
                with open(service_account_file, 'r') as f:
                    service_account_info = json.load(f)
                    
                return {
                    "success": True,
                    "auth_configured": True,
                    "auth_type": "service_account",
                    "service_account": service_account_info.get("client_email", "不明"),
                    "project_id": service_account_info.get("project_id", "不明"),
                    "export_type": "サービスアカウント（共有ドキュメント）"
                }
            except Exception as e:
                logger.error(f"サービスアカウント情報の取得中にエラー: {str(e)}")
                pass
        
        # どちらも設定されていない場合
        return {
            "success": False,
            "error": "Google Drive連携が設定されていません",
            "auth_configured": False
        }
            
    except Exception as e:
        logger.error(f"エクスポート情報の取得中にエラー: {str(e)}")
        return {"success": False, "error": str(e), "auth_configured": False} 