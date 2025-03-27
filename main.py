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
import traceback
from typing import Optional, Dict, List, Any
import google.auth
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
import uuid
import secrets
from urllib.parse import quote
from bs4 import BeautifulSoup

# ロギングの設定
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 環境変数の読み込み
load_dotenv()

# デバッグモード設定（APIを使わずにテスト可能）
DEBUG_MODE = os.environ.get("DEBUG_MODE", "false").lower() == "true"
logger.info(f"デバッグモード: {DEBUG_MODE}")

# APIキーのチェック（デバッグモードでない場合のみ）
api_key = os.environ.get("OPENAI_API_KEY")
if not DEBUG_MODE and not api_key:
    raise ValueError("OPENAI_API_KEYが設定されていません。")

# OpenAI クライアントの初期化（デバッグモードでない場合のみ）
client = None
if not DEBUG_MODE:
    client = OpenAI(api_key=api_key)
else:
    logger.info("デバッグモード: OpenAI APIクライアントは初期化されません")

# 静的ファイルとテンプレートの設定
app = FastAPI(title="議事録作成アプリ")
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")

# 処理状態を保存する辞書
tasks_status = {}

# OAuth関連の設定
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "")
REDIRECT_URI = os.environ.get("REDIRECT_URI", "http://localhost:8000/oauth/callback")

# サービスアカウントキーファイルからAPI情報を読み込む
GOOGLE_SERVICE_ACCOUNT_FILE = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "mtg-minutes-drive-api-key.json")

# 認証情報ファイルから追加情報を読み込む
if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET or not GOOGLE_API_KEY:
    try:
        if os.path.exists(GOOGLE_SERVICE_ACCOUNT_FILE):
            logger.info(f"サービスアカウントキーファイル {GOOGLE_SERVICE_ACCOUNT_FILE} から認証情報を読み込みます")
            import json
            with open(GOOGLE_SERVICE_ACCOUNT_FILE, 'r') as f:
                credentials_data = json.load(f)
                
                # プロジェクト情報を取得（APIキー生成に関連）
                project_id = credentials_data.get('project_id')
                logger.info(f"プロジェクトID: {project_id}")
                
                # APIキーがない場合、サービスアカウントファイルから取得を試みる
                if not GOOGLE_API_KEY:
                    # 通常はAPIキーはJSON内の'api_key'か'key'に格納されていることが多い
                    GOOGLE_API_KEY = credentials_data.get('api_key') or credentials_data.get('key')
                    
                    # サービスアカウントファイルにAPIキーがない場合、他のフィールドを確認
                    if not GOOGLE_API_KEY and project_id:
                        # プロジェクトIDがあれば、そのプロジェクトの他の認証情報ファイルを探す
                        alternative_files = [
                            f"{project_id}-oauth.json",
                            f"{project_id}-api-key.json",
                            "oauth-credentials.json",
                            "credentials.json",
                            ".oauth-credentials.json"
                        ]
                        
                        for alt_file in alternative_files:
                            if os.path.exists(alt_file):
                                logger.info(f"代替認証情報ファイル {alt_file} を確認中...")
                                try:
                                    with open(alt_file, 'r') as alt_f:
                                        alt_data = json.load(alt_f)
                                        if not GOOGLE_CLIENT_ID:
                                            GOOGLE_CLIENT_ID = alt_data.get('client_id') or alt_data.get('web', {}).get('client_id')
                                        if not GOOGLE_CLIENT_SECRET:
                                            GOOGLE_CLIENT_SECRET = alt_data.get('client_secret') or alt_data.get('web', {}).get('client_secret')
                                        if not GOOGLE_API_KEY:
                                            GOOGLE_API_KEY = alt_data.get('api_key') or alt_data.get('key')
                                        
                                        if GOOGLE_API_KEY:
                                            logger.info(f"代替ファイル {alt_file} からAPIキーを読み込みました")
                                            break
                                except Exception as alt_error:
                                    logger.warning(f"代替ファイル {alt_file} の読み込みエラー: {str(alt_error)}")
                
                # OAuth情報を確認
                if "web" in credentials_data:
                    if not GOOGLE_CLIENT_ID:
                        GOOGLE_CLIENT_ID = credentials_data.get("web", {}).get("client_id")
                    if not GOOGLE_CLIENT_SECRET:
                        GOOGLE_CLIENT_SECRET = credentials_data.get("web", {}).get("client_secret")
                
                logger.info(f"認証情報ファイルから読み込み完了: "
                           f"クライアントID={bool(GOOGLE_CLIENT_ID)}, "
                           f"APIキー有無={bool(GOOGLE_API_KEY)}")
                
                # APIキーが取得できなかった場合の警告
                if not GOOGLE_API_KEY:
                    logger.warning("Google Drive Pickerの使用にはAPIキーが必要です。フォルダピッカーは利用できません。")
                
    except Exception as e:
        logger.error(f"認証情報ファイルの読み込み中にエラー: {str(e)}")
        logger.error(traceback.format_exc())

# 認証トークンを保存する辞書（本番環境ではセッションやDBを使用）
oauth_tokens = {}
export_content = {}  # エクスポート中のコンテンツを一時的に保存

# Google API のスコープ
SCOPES = [
    'https://www.googleapis.com/auth/drive',  # フルアクセス権限（共有フォルダなど全てのフォルダにアクセス可能）
    'https://www.googleapis.com/auth/documents'
]

# リクエストモデルの定義
class EditMinutesRequest(BaseModel):
    minutes: str
    prompt: str

class ExportToDriveRequest(BaseModel):
    content: str
    title: str = "議事録"
    folder_id: str = ""  # 空の場合はルートに保存

class ExportToNotionRequest(BaseModel):
    title: str = "議事録"
    content: str
    token: str
    database_id: str

class RegenerateMinutesRequest(BaseModel):
    raw_text: str
    meeting_summary: str = ""
    key_terms: str = ""

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/google-picker-info")
async def get_google_picker_info():
    """
    Google PickerのAPIキーと設定を返す
    """
    if not GOOGLE_CLIENT_ID or not GOOGLE_API_KEY:
        logger.error(f"Google Picker初期化エラー - クライアントID有無: {bool(GOOGLE_CLIENT_ID)}, APIキー有無: {bool(GOOGLE_API_KEY)}")
        return JSONResponse(
            status_code=500,
            content={"error": "Google OAuth設定が構成されていません"}
        )
    
    # リダイレクトURIをログに出力（デバッグ用）
    logger.info(f"Google Picker設定 - リダイレクトURI: {REDIRECT_URI}")
    logger.info(f"Google Picker設定 - クライアントID: {GOOGLE_CLIENT_ID[:10]}...（先頭10文字のみ表示）")
    logger.info(f"Google Picker設定 - APIキー: {GOOGLE_API_KEY[:5]}...（先頭5文字のみ表示）")
    
    # 設定値を返す
    return {
        "clientId": GOOGLE_CLIENT_ID,
        "apiKey": GOOGLE_API_KEY,
        "scope": " ".join(SCOPES),
        "redirectUri": REDIRECT_URI  # Pickerのリダイレクト確認用
    }

@app.get("/drive-folders/{token_id}")
async def get_folder_list(token_id: str):
    """
    Googleドライブのフォルダ一覧を取得する
    """
    try:
        if token_id not in oauth_tokens:
            return JSONResponse(
                status_code=401,
                content={"error": "無効な認証トークンです。再度認証してください。"}
            )
            
        # トークン情報を取得
        token_info = oauth_tokens[token_id]
        
        # フォルダ一覧を取得
        folders_data = await get_drive_folders(token_info)
        
        if "error" in folders_data:
            return JSONResponse(
                status_code=500,
                content={"error": f"フォルダ一覧の取得中にエラーが発生しました: {folders_data['error']}"}
            )
            
        # フォルダリストを整形
        folder_list = []
        
        # マイドライブのフォルダを追加
        for folder in folders_data.get("my_folders", []):
            folder_list.append({
                "id": folder.get("id"),
                "name": folder.get("name"),
                "type": "folder",
                "parentId": folder.get("parents", [None])[0]
            })
            
        # 共有ドライブを追加
        for drive in folders_data.get("shared_drives", []):
            folder_list.append({
                "id": drive.get("id"),
                "name": f"共有ドライブ: {drive.get('name')}",
                "type": "shared_drive",
                "parentId": None
            })
        
        # 階層構造を構築
        folder_hierarchy = build_folder_hierarchy(folder_list)
        
        # デバッグ: 階層構造の確認
        logger.info(f"フォルダ階層構造: {len(folder_hierarchy)} 個のルートフォルダ")
        for i, folder in enumerate(folder_hierarchy[:3]):  # 最初の3つだけログ出力
            children_count = len(folder.get("children", []))
            logger.info(f"  ルートフォルダ {i+1}: {folder.get('name')} (ID: {folder.get('id')}, 子フォルダ: {children_count}個)")
            
        return {
            "folders": folder_list,
            "hierarchy": folder_hierarchy
        }
        
    except Exception as e:
        logger.error(f"フォルダ一覧取得API処理中にエラー: {str(e)}")
        logger.error(traceback.format_exc())
        return JSONResponse(
            status_code=500,
            content={"error": f"フォルダ一覧の取得中にエラーが発生しました: {str(e)}"}
        )

def build_folder_hierarchy(folder_list):
    """
    フォルダリストから階層構造を構築する補助関数
    """
    # IDをキーとしたマップを作成
    folder_map = {folder["id"]: folder for folder in folder_list}
    
    # ルートフォルダと階層構造
    root_folders = []
    
    for folder in folder_list:
        parent_id = folder.get("parentId")
        
        # ルートフォルダまたは共有ドライブの場合
        if not parent_id or folder.get("type") == "shared_drive":
            folder_copy = folder.copy()
            folder_copy["children"] = []
            root_folders.append(folder_copy)
        else:
            # 親フォルダがマップに存在する場合
            if parent_id in folder_map:
                # 親フォルダに「children」キーがなければ追加
                if "children" not in folder_map[parent_id]:
                    folder_map[parent_id]["children"] = []
                
                # 子フォルダのコピーを作成して追加
                folder_copy = folder.copy()
                folder_copy["children"] = []
                folder_map[parent_id]["children"].append(folder_copy)
    
    return root_folders

# OAuthの認証リクエストを開始する
@app.get("/oauth/google")
async def start_oauth(request: Request):
    try:
        # クエリパラメータを取得
        title = request.query_params.get("title", "議事録")
        content = request.query_params.get("content", "")
        folder_id = request.query_params.get("folder_id", "")
        folders_only = request.query_params.get("folders_only", "false").lower() == "true"
        
        # ステート（CSRF対策）
        state = secrets.token_urlsafe(16)
        
        # コンテンツIDを生成
        content_id = secrets.token_urlsafe(16)
        
        # フォルダ一覧取得のみのリクエストの場合は特別処理
        if folders_only:
            logger.info("フォルダ一覧取得モードでの認証")
            # ダミーコンテンツを使用
            content = "folder_list_only"
            
        elif not content:
            logger.error("エクスポートするコンテンツがありません")
            return templates.TemplateResponse(
                "error.html", 
                {"request": request, "error": "エクスポートするコンテンツがありません。"}
            )
        
        # コンテンツを一時保存
        export_content[content_id] = {
            "content": content,
            "title": title,
            "folder_id": folder_id,
            "folders_only": folders_only
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
        
        # フォルダ一覧取得モードの場合
        if content_data.get("folders_only", False):
            logger.info("フォルダ一覧取得モードの認証完了")
            
            # 成功ページを返す
            html_content = f"""
            <!DOCTYPE html>
            <html>
            <head>
                <title>フォルダ選択完了</title>
                <style>
                    body {{ font-family: sans-serif; text-align: center; margin-top: 50px; }}
                    .message {{ max-width: 500px; margin: 0 auto; }}
                    .success {{ color: #28a745; }}
                </style>
            </head>
            <body>
                <div class="message">
                    <h2>認証成功</h2>
                    <p class="success">認証が完了しました。このウィンドウは閉じて構いません。</p>
                    <p>元のページに戻り、「フォルダを再読み込み」ボタンをクリックしてください。</p>
                    <p><small>トークンID: {content_id}</small></p>
                    <script>
                        // ローカルストレージにトークンIDを保存
                        localStorage.setItem('lastContentId', '{content_id}');
                        
                        // 5秒後に自動的にウィンドウを閉じる
                        setTimeout(function() {{
                            window.close();
                        }}, 5000);
                    </script>
                </div>
            </body>
            </html>
            """
            
            return HTMLResponse(content=html_content)
        
        # 通常のGoogle Driveエクスポート
        else:
            try:
                logger.info("Google Driveにエクスポート中...")
                # フォルダIDがあれば使用
                folder_id = content_data.get("folder_id", "")
                if folder_id:
                    logger.info(f"フォルダIDが指定されました: {folder_id}")
                
                document_url = await export_to_google_drive(
                    content_data.get("content", ""), 
                    content_data.get("title", "議事録"),
                    oauth_tokens[content_id],
                    folder_id=folder_id if folder_id else None
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

# 使用可能なフォルダ一覧を取得
async def get_drive_folders(token_info):
    try:
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
        
        # マイドライブのフォルダを取得
        query = "mimeType='application/vnd.google-apps.folder' and trashed=false"
        results = drive_service.files().list(
            q=query,
            fields="files(id, name, parents)",
            orderBy="name",
            pageSize=100
        ).execute()
        
        folders = results.get('files', [])
        
        # 共有ドライブも取得
        shared_drives = []
        try:
            # 共有ドライブの一覧を取得
            drives_result = drive_service.drives().list(pageSize=50).execute()
            shared_drives_basic = drives_result.get('drives', [])
            
            # 共有ドライブごとに詳細情報（フォルダ一覧）を取得
            for drive in shared_drives_basic:
                try:
                    # 各共有ドライブのフォルダを取得（shared_drives配列に追加）
                    shared_drives.append(drive)
                    
                    # 共有ドライブ内のフォルダも取得 (25件まで)
                    drive_id = drive.get('id')
                    if drive_id:
                        # 共有ドライブ内のフォルダを検索
                        drive_folders_query = "mimeType='application/vnd.google-apps.folder' and trashed=false"
                        drive_folders = drive_service.files().list(
                            q=drive_folders_query,
                            driveId=drive_id,
                            corpora="drive",
                            supportsAllDrives=True,
                            supportsTeamDrives=True,  # 後方互換性のため
                            fields="files(id, name, parents, driveId)",
                            pageSize=25
                        ).execute().get('files', [])
                        
                        # 共有ドライブ内のフォルダを追加（親フォルダIDを設定）
                        for folder in drive_folders:
                            folder['driveId'] = drive_id  # ドライブIDを追加
                            folders.append(folder)
                            
                        logger.info(f"共有ドライブ '{drive.get('name')}' から {len(drive_folders)} 個のフォルダを取得")
                except Exception as drive_error:
                    logger.warning(f"共有ドライブ '{drive.get('name')}' のフォルダ取得中にエラー: {str(drive_error)}")
                    # エラーは無視して続行
        except Exception as e:
            logger.warning(f"共有ドライブの取得中にエラー: {str(e)}")
        
        # 結果を組み合わせる
        return {
            "my_folders": folders,
            "shared_drives": shared_drives
        }
    except Exception as e:
        logger.error(f"フォルダ一覧取得エラー: {str(e)}")
        logger.error(traceback.format_exc())
        return {"error": str(e)}

# Google Driveへのエクスポート処理（OAuth認証使用）
async def export_to_google_drive(content, title, token_info, folder_id=None):
    try:
        logger.info(f"Google Driveエクスポート開始: {title}")
        
        # HTMLの処理
        is_html = "<" in content and ">" in content
        
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
        
        # 新しいGoogleドキュメントを作成するためのメタデータ
        doc_metadata = {
            'name': title,
            'mimeType': 'application/vnd.google-apps.document'
        }
        
        # フォルダIDが指定されている場合は、そのフォルダに保存
        if folder_id and folder_id.strip():
            try:
                # フォルダが存在するか確認
                logger.info(f"指定されたフォルダを確認中: {folder_id}")
                folder_check = drive_service.files().get(
                    fileId=folder_id, 
                    fields="id,name,mimeType,capabilities,driveId", 
                    supportsAllDrives=True
                ).execute()
                
                # レスポンスの詳細をログ記録
                logger.info(f"フォルダ情報: {folder_check}")
                
                # フォルダのタイプかどうか確認
                if folder_check.get('mimeType') != 'application/vnd.google-apps.folder':
                    logger.warning(f"指定されたID '{folder_id}' はフォルダではありません: {folder_check.get('mimeType')}")
                    raise ValueError(f"指定されたID '{folder_id}' はフォルダではありません")
                
                # フォルダへの書き込み権限を確認
                capabilities = folder_check.get('capabilities', {})
                if not capabilities.get('canAddChildren', False):
                    logger.warning(f"指定されたフォルダ '{folder_id}' への書き込み権限がありません")
                    raise ValueError(f"指定されたフォルダ '{folder_id}' への書き込み権限がありません")
                
                # 親フォルダを指定
                doc_metadata['parents'] = [folder_id]
                
                # 共有ドライブのIDを取得
                drive_id = folder_check.get('driveId')
                if drive_id:
                    logger.info(f"共有ドライブを検出: ドライブID {drive_id}")
                    # 保存時にドライブIDを指定する必要がある
                    doc_metadata['driveId'] = drive_id
                    
                    # 共有ドライブかどうかをさらに確認
                    try:
                        # 共有ドライブ情報を直接取得
                        drive_info = drive_service.drives().get(driveId=drive_id).execute()
                        logger.info(f"共有ドライブ情報: {drive_info.get('name')} (ID: {drive_id})")
                        
                        # 共有ドライブの権限を確認
                        drive_capabilities = drive_info.get('capabilities', {})
                        can_create_files = drive_capabilities.get('canCreateFiles', False)
                        logger.info(f"共有ドライブへのファイル作成権限: {can_create_files}")
                        
                        if not can_create_files:
                            logger.warning(f"共有ドライブ '{drive_info.get('name')}' へのファイル作成権限がありません")
                            raise ValueError(f"共有ドライブ '{drive_info.get('name')}' へのファイル作成権限がありません")
                    except Exception as drive_error:
                        logger.warning(f"共有ドライブ情報の取得中にエラー: {str(drive_error)}")
                        # エラーは無視し、通常の処理を続行
                
                logger.info(f"保存先フォルダ: {folder_check.get('name')} ({folder_id})")
                
                # APIのスコープが十分か確認するため、ユーザー情報を取得
                about = drive_service.about().get(fields="user").execute()
                user_email = about.get("user", {}).get("emailAddress", "不明")
                logger.info(f"認証ユーザー: {user_email}")
                
            except Exception as folder_error:
                logger.error(f"フォルダ確認エラー: {str(folder_error)} - ルートに保存します")
                logger.error(traceback.format_exc())
                # フォルダが存在しないか問題がある場合は、ルートフォルダに保存
        
        try:
            logger.info("Google Docsドキュメントを作成中...")
            logger.info(f"リクエスト内容: {doc_metadata}")
            
            # 共有ドライブの場合、必要なパラメータを追加
            drive_id = doc_metadata.pop('driveId', None)  # メタデータから取り出す
            
            create_params = {
                'body': doc_metadata,
                'supportsAllDrives': True
            }
            
            # 共有ドライブへのアクセスを許可するパラメータ
            if drive_id:
                logger.info(f"共有ドライブにファイルを作成します: driveId={drive_id}")
                # 共有ドライブフラグが必要（古いAPIとの互換性のため）
                create_params['supportsTeamDrives'] = True
            
            logger.info(f"ドキュメント作成パラメータ: {create_params}")
            try:
                doc = drive_service.files().create(**create_params).execute()
            except Exception as create_error:
                logger.error(f"ドキュメント作成中の詳細エラー: {str(create_error)}")
                # 共有ドライブの場合は別の方法も試す
                if drive_id:
                    logger.info("別の方法で共有ドライブにファイルを作成します")
                    # 共有ドライブのルートを親として指定
                    doc_metadata['parents'] = [drive_id]
                    
                    # 新しいパラメータでリクエストを作成（problematicなパラメータを削除）
                    alt_params = {
                        'body': doc_metadata,
                        'supportsAllDrives': True,
                        'supportsTeamDrives': True
                    }
                    logger.info(f"代替パラメータ: {alt_params}")
                    
                    # 再試行
                    doc = drive_service.files().create(**alt_params).execute()
            
            document_id = doc.get('id')
            
            # レスポンス全体をログ記録
            logger.info(f"ドキュメント作成レスポンス: {doc}")
            
        except Exception as create_error:
            logger.error(f"ドキュメント作成エラー: {str(create_error)}")
            logger.error(traceback.format_exc())
            
            # 共有ドライブ関連のエラーメッセージを詳細化
            error_msg = str(create_error)
            if "teamDriveId" in error_msg or "driveId" in error_msg:
                error_msg = f"共有ドライブへのアクセスエラー: {error_msg}。共有ドライブへの十分な権限があるか確認してください。"
            elif "permission" in error_msg.lower() or "access" in error_msg.lower():
                error_msg = f"権限エラー: {error_msg}。フォルダへの書き込み権限があるか確認してください。"
            
            raise ValueError(f"ドキュメント作成中にエラーが発生しました: {error_msg}")
        
        if not document_id:
            raise ValueError("ドキュメントIDの取得に失敗しました")
        
        logger.info(f"ドキュメント作成成功: {document_id}")
        
        # HTMLからプレーンテキストに変換
        if is_html:
            content_text = html_to_plain_text(content)
        else:
            content_text = content
        
        # テキストとして挿入
        insert_text_request = {
            'insertText': {
                'location': {'index': 1},
                'text': content_text
            }
        }
        
        logger.info("ドキュメント内容を挿入中...")
        docs_service.documents().batchUpdate(
            documentId=document_id,
            body={'requests': [insert_text_request]}
        ).execute()
        
        logger.info("テキスト挿入成功")
        
        # HTMLの場合はスタイルとリスト構造を適用
        if is_html:
            try:
                logger.info("リッチテキストスタイルを適用中...")
                # スタイル情報の抽出と適用
                style_requests = extract_styles_from_html(content, content_text)
                list_requests = extract_lists_from_html(content, content_text)
                
                # スタイルとリストのリクエストを実行
                if style_requests or list_requests:
                    all_requests = style_requests + list_requests
                    logger.info(f"スタイル適用リクエスト数: {len(all_requests)}")
                    
                    # バッチ処理でリクエストを送信
                    docs_service.documents().batchUpdate(
                        documentId=document_id,
                        body={'requests': all_requests}
                    ).execute()
                    logger.info("スタイル適用成功")
                else:
                    logger.info("適用するスタイルが見つかりませんでした")
            except Exception as style_error:
                logger.error(f"スタイル適用中にエラー: {str(style_error)}")
                logger.error(traceback.format_exc())
                # スタイル適用が失敗してもプロセスは続行
        
        logger.info("ドキュメント更新成功")
        
        # ドキュメントへのリンクを生成
        document_url = f"https://docs.google.com/document/d/{document_id}/edit"
        
        logger.info(f"エクスポート完了: {document_url}")
        
        return document_url
        
    except Exception as e:
        logger.error(f"Google Driveへのエクスポート中にエラー: {str(e)}")
        # スタックトレースもログに記録（デバッグ用）
        logger.error(traceback.format_exc())
        raise

def extract_styles_from_html(html_content, plain_text):
    """
    HTMLからスタイル情報を抽出し、Google Docs APIの形式に変換する
    (plain_textは既にドキュメントに挿入されたテキスト)
    """
    from bs4 import BeautifulSoup
    import re
    
    requests = []
    
    try:
        soup = BeautifulSoup(html_content, 'html.parser')
        
        # 見出しの処理
        for i in range(1, 7):
            heading_tag = f'h{i}'
            headings = soup.find_all(heading_tag)
            
            for heading in headings:
                heading_text = heading.get_text().strip()
                if heading_text and heading_text in plain_text:
                    # テキスト位置を検索
                    start_index = plain_text.find(heading_text)
                    if start_index != -1:
                        end_index = start_index + len(heading_text)
                        
                        # 見出しスタイルの適用
                        heading_request = {
                            'updateParagraphStyle': {
                                'range': {
                                    'startIndex': start_index + 1,  # Google Docsは1から始まるインデックス
                                    'endIndex': end_index + 1
                                },
                                'paragraphStyle': {
                                    'namedStyleType': f'HEADING_{i}'
                                },
                                'fields': 'namedStyleType'
                            }
                        }
                        requests.append(heading_request)
        
        # 太字の処理
        for bold in soup.find_all(['b', 'strong']):
            bold_text = bold.get_text().strip()
            if bold_text and bold_text in plain_text:
                start_index = plain_text.find(bold_text)
                if start_index != -1:
                    end_index = start_index + len(bold_text)
                    
                    bold_request = {
                        'updateTextStyle': {
                            'range': {
                                'startIndex': start_index + 1,
                                'endIndex': end_index + 1
                            },
                            'textStyle': {
                                'bold': True
                            },
                            'fields': 'bold'
                        }
                    }
                    requests.append(bold_request)
        
        # 斜体の処理
        for italic in soup.find_all(['i', 'em']):
            italic_text = italic.get_text().strip()
            if italic_text and italic_text in plain_text:
                start_index = plain_text.find(italic_text)
                if start_index != -1:
                    end_index = start_index + len(italic_text)
                    
                    italic_request = {
                        'updateTextStyle': {
                            'range': {
                                'startIndex': start_index + 1,
                                'endIndex': end_index + 1
                            },
                            'textStyle': {
                                'italic': True
                            },
                            'fields': 'italic'
                        }
                    }
                    requests.append(italic_request)
        
        # 下線の処理
        for underline in soup.find_all('u'):
            underline_text = underline.get_text().strip()
            if underline_text and underline_text in plain_text:
                start_index = plain_text.find(underline_text)
                if start_index != -1:
                    end_index = start_index + len(underline_text)
                    
                    underline_request = {
                        'updateTextStyle': {
                            'range': {
                                'startIndex': start_index + 1,
                                'endIndex': end_index + 1
                            },
                            'textStyle': {
                                'underline': True
                            },
                            'fields': 'underline'
                        }
                    }
                    requests.append(underline_request)
                    
        return requests
    except Exception as e:
        logger.error(f"スタイル抽出中にエラー: {str(e)}")
        return []

def extract_lists_from_html(html_content, plain_text):
    """
    HTMLからリスト要素を抽出し、Google Docs APIの形式に変換する
    （bulletPresetを使わない方法）
    """
    from bs4 import BeautifulSoup
    
    requests = []
    
    try:
        soup = BeautifulSoup(html_content, 'html.parser')
        
        # 番号付きリスト（ol）の処理
        ordered_lists = soup.find_all('ol')
        for ol_index, ol in enumerate(ordered_lists):
            # リストIDの生成は不要（bulletPresetを使用）
            # 個別のリストアイテムごとにcreateParagraphBulletsを使用
            
            # リストアイテムを処理
            list_items = ol.find_all('li')
            for li in list_items:
                li_text = li.get_text().strip()
                if li_text and li_text in plain_text:
                    start_index = plain_text.find(li_text)
                    if start_index != -1:
                        end_index = start_index + len(li_text)
                        
                        # リストを適用（updateParagraphBulletを使用）
                        apply_list_request = {
                            'createParagraphBullets': {
                                'range': {
                                    'startIndex': start_index + 1,  # Google Docsは1から始まる
                                    'endIndex': end_index + 1
                                },
                                'bulletPreset': 'NUMBERED_DECIMAL_NESTED',
                                'indentFirstLine': {
                                    'magnitude': 18,
                                    'unit': 'PT'
                                },
                                'indentStart': {
                                    'magnitude': 36,
                                    'unit': 'PT'
                                }
                            }
                        }
                        requests.append(apply_list_request)
        
        # 箇条書きリスト（ul）の処理
        unordered_lists = soup.find_all('ul')
        for ul_index, ul in enumerate(unordered_lists):
            # リストIDの生成は不要（bulletPresetを使用）
            # 個別のリストアイテムごとにcreateParagraphBulletsを使用
            
            # リストアイテムを処理
            list_items = ul.find_all('li')
            for li in list_items:
                li_text = li.get_text().strip()
                if li_text and li_text in plain_text:
                    start_index = plain_text.find(li_text)
                    if start_index != -1:
                        end_index = start_index + len(li_text)
                        
                        # 段落スタイルを適用（createParagraphBulletsを使用）
                        apply_list_request = {
                            'createParagraphBullets': {
                                'range': {
                                    'startIndex': start_index + 1,
                                    'endIndex': end_index + 1
                                },
                                'bulletPreset': 'BULLET_DISC_CIRCLE_SQUARE',
                                'indentFirstLine': {
                                    'magnitude': 18,
                                    'unit': 'PT'
                                },
                                'indentStart': {
                                    'magnitude': 36,
                                    'unit': 'PT'
                                }
                            }
                        }
                        requests.append(apply_list_request)
        
        return requests
    except Exception as e:
        logger.error(f"リスト抽出中にエラー: {str(e)}")
        logger.error(traceback.format_exc())
        return []

# HTML形式の議事録からプレーンテキストを抽出する
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
async def process_audio_task(task_id: str, file_path: str, meeting_summary: str, key_terms: str, model: str = "whisper-1"):
    try:
        file_ext = os.path.splitext(file_path)[1].lower()
        
        # 処理状態の初期設定 (ステップ1: ファイルアップロード・処理)
        status = {
            "step": 1,
            "progress": 5,
            "message": "音声ファイルを準備中...",
            "completed": False
        }
        update_task_status(task_id, status)
        await asyncio.sleep(0.1)
        
        # initial_promptの作成
        initial_prompt = "これは会議の録音です。"
        if meeting_summary:
            initial_prompt += f" {meeting_summary}"
        if key_terms:
            initial_prompt += f" この会議では以下の用語や人物が登場する可能性があります: {key_terms}"
        
        # 処理初期化の完了
        status = {
            "step": 1,
            "progress": 15,
            "message": "音声処理を初期化中...",
            "completed": False
        }
        update_task_status(task_id, status)
        await asyncio.sleep(0.1)
        
        # ファイルサイズのチェック
        file_size = os.path.getsize(file_path)
        logger.info(f"一時ファイルを準備: {file_path} [ファイルサイズ: {file_size} バイト]")
        
        # ファイルのMIMEタイプとメタデータ取得
        file_mime = "audio/unknown"
        file_type = "Unknown audio format"
        
        try:
            # python-magicのインポートを試みる
            import magic
            try:
                file_mime = magic.from_file(file_path, mime=True)
                file_type = magic.from_file(file_path)
                logger.info(f"ファイル形式: {file_mime}, {file_type}")
            except Exception as magic_error:
                logger.warning(f"ファイル形式取得エラー: {str(magic_error)}")
                # magic.from_fileでエラーが発生した場合
                # ファイル拡張子からMIMEタイプを推測
                if file_ext == '.mp3':
                    file_mime = 'audio/mpeg'
                    file_type = 'MP3 audio'
                elif file_ext == '.wav':
                    file_mime = 'audio/wav'
                    file_type = 'WAV audio'
                elif file_ext in ['.m4a', '.mp4']:
                    file_mime = 'audio/mp4'
                    file_type = 'MP4 audio'
        except ImportError:
            # python-magicがインストールされていない場合
            logger.warning("python-magicライブラリがインストールされていません")
            # ファイル拡張子からMIMEタイプを推測
            if file_ext == '.mp3':
                file_mime = 'audio/mpeg'
                file_type = 'MP3 audio'
            elif file_ext == '.wav':
                file_mime = 'audio/wav'
                file_type = 'WAV audio'
            elif file_ext in ['.m4a', '.mp4']:
                file_mime = 'audio/mp4'
                file_type = 'MP4 audio'
        
        # ファイル情報ステータス更新
        status = {
            "step": 1,
            "progress": 25,
            "message": f"ファイル情報: {file_mime}, {file_size/1024:.1f} KB",
            "completed": False
        }
        update_task_status(task_id, status)
        await asyncio.sleep(0.1)
        
        # 音声処理前の準備
        status = {
            "step": 1,
            "progress": 40,
            "message": "音声プロセッサを準備中...",
            "completed": False
        }
        update_task_status(task_id, status)
        await asyncio.sleep(0.1)
        
        # 音声分析中
        status = {
            "step": 1,
            "progress": 60,
            "message": "音声ファイルを分析中...",
            "completed": False
        }
        update_task_status(task_id, status)
        await asyncio.sleep(0.1)
        
        # ファイル処理完了
        status = {
            "step": 1,
            "progress": 80,
            "message": "音声ファイル処理完了",
            "completed": False
        }
        update_task_status(task_id, status)
        await asyncio.sleep(0.1)
        
        # ステップ2: 音声認識の開始
        status = {
            "step": 2,
            "progress": 5,
            "message": f"音声認識モデル（{model}）を準備中...",
            "completed": False
        }
        update_task_status(task_id, status)
        await asyncio.sleep(0.1)
        
        # 音声認識開始
        status = {
            "step": 2,
            "progress": 10,
            "message": "音声認識を開始...",
            "completed": False
        }
        update_task_status(task_id, status)
        await asyncio.sleep(0.1)
        
        # OpenAI APIを使用して音声認識を実行
        logger.info(f"音声認識を開始 [モデル: {model}]")
        status = {
            "step": 2,
            "progress": 15,
            "message": f"{model}を使用して音声を認識中...",
            "completed": False
        }
        update_task_status(task_id, status)
        
        # 文字起こしの実行
        raw_text = ""
        
        if DEBUG_MODE:
            # デバッグモード: サンプルテキストを返す
            logger.info(f"デバッグモード: 文字起こしAPIを使わずにサンプルテキストを使用（選択モデル: {model}）")
            
            # 音声認識の進捗をシミュレート
            progress_steps = [20, 30, 45, 60, 75, 90]
            progress_messages = [
                "音声データを解析中...",
                "音声パターンを認識中...",
                "言語モデルでテキスト変換中...",
                "音声認識実行中...",
                "文脈を解析中...",
                "認識結果を確定中..."
            ]
            
            for progress, message in zip(progress_steps, progress_messages):
                status = {
                    "step": 2,
                    "progress": progress,
                    "message": message,
                    "completed": False
                }
                update_task_status(task_id, status)
                await asyncio.sleep(0.2)
            
            # サンプルテキスト（デバッグ用）
            raw_text = """
            会議を始めます。本日の議題は第3四半期の売上報告と来年度の予算計画についてです。
            まず、佐藤さんから売上の報告をお願いします。
            
            はい、佐藤です。第3四半期の売上は前年比8%増の2億3000万円となりました。
            特に新規顧客からの売上が25%増加し、全体をけん引しています。
            
            ありがとうございます。続いて、田中さんから予算計画の説明をお願いします。
            
            田中です。来年度の予算については、マーケティング費を15%増の5000万円、
            研究開発費を20%増の8000万円で計画しています。
            
            質問がありますが、研究開発の内訳はどうなっていますか？
            
            新製品開発に6000万円、既存製品の改良に2000万円を予定しています。
            
            わかりました。それでは、次回は来月15日に開催します。それまでに各部門から
            詳細な計画書を提出してください。以上で会議を終了します。
            """
        else:
            # 通常モード: 実際にOpenAI APIを使用
            # 同期的なAPIを非同期的に実行
            loop = asyncio.get_event_loop()
            
            def run_whisper():
                # 音声認識開始通知
                # 注: この部分はメインスレッドから非同期に実行されるためステータス更新ができない
                # そのため、後でポーリングでステータスを更新する
                logger.info(f"音声認識モデル: {model} - API呼び出し開始")
                
                # 同期的にファイルを開いて処理
                with open(file_path, "rb") as audio_file:
                    # Whisperモデルと他のGPTモデルでAPIの呼び出し方が異なる
                    if model == "whisper-1":
                        # Whisper APIを使用
                        return client.audio.transcriptions.create(
                            model=model,
                            file=audio_file,
                            language="ja",
                            prompt=initial_prompt
                        )
                    else:
                        # GPT-4o Transcribe系モデルを使用
                        return client.audio.speech.transcriptions.create(
                            model=model,
                            file=audio_file,
                            language="ja",
                            prompt=initial_prompt
                        )
            
            # API呼び出し中の進捗状況を更新するタスク
            async def update_progress_during_api_call():
                progress_values = [20, 30, 45, 60, 75, 90]
                messages = [
                    "音声データを送信中...",
                    "音声を解析中...",
                    "音声認識処理中...",
                    "テキスト変換中...",
                    "認識結果を整形中...",
                    "最終処理実行中..."
                ]
                
                for i, (progress, message) in enumerate(zip(progress_values, messages)):
                    status = {
                        "step": 2,
                        "progress": progress,
                        "message": message,
                        "completed": False
                    }
                    update_task_status(task_id, status)
                    await asyncio.sleep(1.5)  # 1.5秒ごとに更新
            
            # 進捗更新タスクと実際のAPI呼び出しを並行して実行
            progress_task = asyncio.create_task(update_progress_during_api_call())
            
            # 実際のAPI呼び出し
            try:
                transcript = await loop.run_in_executor(None, run_whisper)
                
                # API呼び出し完了のステータス更新
                status = {
                    "step": 2,
                    "progress": 49,
                    "message": "音声認識完了、結果を処理中...",
                    "completed": False
                }
                update_task_status(task_id, status)
                
                # 文字起こし完了
                raw_text = transcript.text
                
                # 進捗更新タスクを停止（APIが先に完了した場合）
                if not progress_task.done():
                    progress_task.cancel()
                    
            except Exception as e:
                # エラーが発生した場合も進捗更新タスクを停止
                if not progress_task.done():
                    progress_task.cancel()
                raise e
        logger.info(f"音声認識が完了 [文字数: {len(raw_text)}]")
        status = {
            "step": 2,
            "progress": 95,
            "message": f"音声認識が完了しました ({len(raw_text)}文字)",
            "completed": False
        }
        update_task_status(task_id, status)
        await asyncio.sleep(0.3)
        
        # 音声認識完了
        status = {
            "step": 2,
            "progress": 100,
            "message": "音声からテキストへの変換完了",
            "completed": False
        }
        update_task_status(task_id, status)
        await asyncio.sleep(0.5)
        
        # 議事録生成ステップの開始
        status = {
            "step": 3,
            "progress": 5,
            "message": "議事録生成の準備中...",
            "completed": False
        }
        update_task_status(task_id, status)
        await asyncio.sleep(0.1)
        
        # 議事録生成中間ステップ（プロンプト準備）
        status = {
            "step": 3,
            "progress": 15,
            "message": "議事録フォーマットの準備中...",
            "completed": False
        }
        update_task_status(task_id, status)
        await asyncio.sleep(0.1)
        
        # 議事録生成開始
        status = {
            "step": 3,
            "progress": 20,
            "message": "議事録の生成中...",
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
        
        # 議事録生成
        formatted_minutes = ""
        
        if DEBUG_MODE:
            # デバッグモード: サンプルの議事録を生成
            logger.info("デバッグモード: GPT-4 APIを使わずにサンプル議事録を使用")
            
            # 議事録生成の進捗をシミュレート
            progress_steps = [25, 35, 45, 55, 65, 75, 85, 95]
            progress_messages = [
                "議事録構造を作成中...",
                "会議内容を分析中...",
                "主要なトピックを抽出中...",
                "参加者情報を整理中...",
                "決定事項を抽出中...",
                "議事録を整形中...",
                "表現を調整中...",
                "最終調整中..."
            ]
            
            for progress, message in zip(progress_steps, progress_messages):
                status = {
                    "step": 3,
                    "progress": progress,
                    "message": message,
                    "completed": False
                }
                update_task_status(task_id, status)
                await asyncio.sleep(0.2)
            
            # サンプル議事録（デバッグ用）
            formatted_minutes = f"""# 議事録

## 開催情報
- 日時：{datetime.now().strftime('%Y年%m月%d日 %H:%M')}
- 議題：第3四半期の売上報告と来年度の予算計画

## 参加者
- 司会者
- 佐藤さん（売上報告担当）
- 田中さん（予算計画担当）
- その他会議参加者

## 主な議題と決定事項
- 第3四半期の売上は前年比8%増の2億3000万円
- 新規顧客からの売上が25%増加
- 来年度の予算計画：マーケティング費15%増、研究開発費20%増
- 次回会議は来月15日に開催

## 詳細な議事内容
会議では、まず第3四半期の売上報告が行われました。佐藤さんからの報告によると、売上は前年比8%増の2億3000万円となりました。特に新規顧客からの売上が25%増加し、全体をけん引しています。

続いて、田中さんから来年度の予算計画について説明がありました。マーケティング費を15%増の5000万円、研究開発費を20%増の8000万円で計画しているとのことです。研究開発費の内訳は、新製品開発に6000万円、既存製品の改良に2000万円を予定しています。

## 次回のアクション項目
- 各部門から詳細な計画書の提出

## 次回予定
- 日時：来月15日
- 議題：未定
"""
            
            # デバッグモードでも提供された会議の概要や用語を追加
            if meeting_summary or key_terms:
                formatted_minutes += "\n## 備考\n"
                if meeting_summary:
                    formatted_minutes += f"会議の概要: {meeting_summary}\n"
                if key_terms:
                    formatted_minutes += f"キーワード: {key_terms}\n"
        
        else:
            # 通常モード: 実際にGPT-4 APIを使用
            # GPT-4の呼び出しも非同期的に実行
            def run_gpt():
                logger.info("GPT-4による議事録生成を開始")
                return client.chat.completions.create(
                    model="gpt-4-turbo-preview",
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": raw_text}
                    ]
                )
            
            # GPT-4の呼び出し中に進捗ステータスを更新するタスク
            async def update_gpt_progress():
                progress_steps = [25, 35, 45, 55, 65, 75, 85, 95]
                progress_messages = [
                    "議事録構造を作成中...",
                    "会議内容を分析中...",
                    "主要なトピックを抽出中...",
                    "参加者情報を整理中...",
                    "決定事項を抽出中...",
                    "議事録を整形中...",
                    "表現を調整中...",
                    "最終調整中..."
                ]
                
                for progress, message in zip(progress_steps, progress_messages):
                    status = {
                        "step": 3,
                        "progress": progress,
                        "message": message,
                        "completed": False
                    }
                    update_task_status(task_id, status)
                    await asyncio.sleep(2.0)  # LLM呼び出しは時間がかかるため長めの間隔で更新
            
            # 進捗更新タスクとGPT-4呼び出しを並行して実行
            gpt_progress_task = asyncio.create_task(update_gpt_progress())
            
            try:
                # 実際のGPT-4呼び出し
                completion = await loop.run_in_executor(None, run_gpt)
                formatted_minutes = completion.choices[0].message.content
                
                # 進捗更新タスクを停止（APIが先に完了した場合）
                if not gpt_progress_task.done():
                    gpt_progress_task.cancel()
                    
            except Exception as e:
                # エラーが発生した場合も進捗更新タスクを停止
                if not gpt_progress_task.done():
                    gpt_progress_task.cancel()
                raise e
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
    key_terms: str = "",
    model: str = "whisper-1"
):
    try:
        logger.info(f"音声ファイルアップロード: {file.filename}, サイズ: {file.size if hasattr(file, 'size') else '不明'}, モデル: {model}")
        
        # ファイル形式のチェック
        allowed_extensions = {'.mp3', '.mp4', '.mpeg', '.mpga', '.m4a', '.wav', '.webm'}  # サポートする形式
        
        # ファイル名から拡張子を取得
        original_filename = file.filename or ""
        file_ext = os.path.splitext(original_filename)[1].lower()
        
        # 拡張子チェック
        if not file_ext:
            logger.warning(f"ファイル拡張子が見つかりません: {original_filename}")
            file_ext = ".unknown"  # デフォルト拡張子
        
        if file_ext not in allowed_extensions:
            error_msg = f"サポートされていないファイル形式です。対応形式: {', '.join(allowed_extensions)}"
            logger.error(error_msg)
            raise HTTPException(status_code=400, detail=error_msg)

        # タスクIDを生成
        task_id = str(uuid.uuid4())
        logger.info(f"タスクID生成: {task_id}")
        
        # 初期状態を設定
        initial_status = {
            "step": 1,
            "progress": 0,
            "message": "処理を開始しています...",
            "completed": False
        }
        update_task_status(task_id, initial_status)
        
        try:
            # 一時ファイルの作成
            with tempfile.NamedTemporaryFile(delete=False, suffix=file_ext) as temp_file:
                try:
                    # ファイルデータを取得
                    file_data = await file.read()
                    file_size = len(file_data)
                    logger.info(f"ファイルデータ読み込み: {file_size} バイト")
                    
                    # ファイルの保存
                    temp_file.write(file_data)
                    temp_path = temp_file.name
                    logger.info(f"一時ファイル保存: {temp_path}")
                    
                    # ファイルが正しく書き込まれたか確認
                    temp_file.flush()
                    actual_size = os.path.getsize(temp_path)
                    logger.info(f"一時ファイルサイズ確認: {actual_size} バイト")
                    
                    if actual_size == 0:
                        raise ValueError("ファイルサイズが0バイトです")
                    
                    if actual_size != file_size:
                        logger.warning(f"ファイルサイズが一致しません: 期待={file_size}, 実際={actual_size}")
                    
                    # 非同期処理を実行するためのラッパー関数
                    def process_audio_wrapper():
                        try:
                            asyncio.run(process_audio_task(task_id, temp_path, meeting_summary, key_terms, model))
                        except Exception as wrapper_error:
                            logger.error(f"音声処理ラッパーでエラー: {str(wrapper_error)}")
                            import traceback
                            logger.error(traceback.format_exc())
                            # エラー状態を設定
                            error_status = {
                                "error": True,
                                "message": f"処理中にエラーが発生しました: {str(wrapper_error)}",
                                "completed": True
                            }
                            update_task_status(task_id, error_status)
                    
                    # バックグラウンドで処理を実行
                    background_tasks.add_task(process_audio_wrapper)
                    logger.info(f"バックグラウンドタスク開始: {task_id}")
                    
                    # タスクIDを返す
                    return {"task_id": task_id}
                    
                except Exception as file_error:
                    logger.error(f"ファイル処理中にエラー: {str(file_error)}")
                    import traceback
                    logger.error(traceback.format_exc())
                    raise ValueError(f"ファイル処理エラー: {str(file_error)}")
        except Exception as temp_file_error:
            logger.error(f"一時ファイル作成中にエラー: {str(temp_file_error)}")
            import traceback
            logger.error(traceback.format_exc())
            raise ValueError(f"一時ファイルエラー: {str(temp_file_error)}")

    except HTTPException as http_ex:
        # HTTPExceptionはそのまま再送
        logger.error(f"HTTP例外: {http_ex.detail}")
        raise
    except Exception as e:
        logger.error(f"予期せぬエラーが発生: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
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
        
        # 議事録修正処理
        edited_text = ""
        
        if DEBUG_MODE:
            # デバッグモード: 簡易的な修正を行う
            logger.info("デバッグモード: GPT-4 APIを使わずに議事録を修正")
            
            # 簡易的な修正（キーワードの追加など）
            edited_text = text_minutes
            if "追加" in request.prompt:
                # 修正指示に「追加」が含まれている場合は内容を追加
                edited_text += f"\n\n## 編集内容\n{request.prompt}による編集が行われました。"
            elif "削除" in request.prompt:
                # 「削除」が含まれている場合
                edited_text = "# 編集済み議事録\n\n" + edited_text.replace("議事録", "議事録（編集済み）")
            else:
                # その他の修正
                edited_text = "# 修正済み議事録\n\n" + edited_text + f"\n\n修正内容: {request.prompt}"
                
        else:
            # 通常モード: 実際にGPT-4 APIを使用
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
        
        # 議事録生成処理
        formatted_minutes = ""
        
        if DEBUG_MODE:
            # デバッグモード: サンプルの議事録を生成
            logger.info("デバッグモード: GPT-4 APIを使わずに議事録を再生成")
            
            # 簡易的なサンプル議事録（編集指示に基づく）
            formatted_minutes = f"""# 再生成された議事録

## 開催情報
- 日時：{datetime.now().strftime('%Y年%m月%d日 %H:%M')}
- 議題：議事録の再生成

## 参加者
- システム管理者
- ユーザー

## 主な議題と決定事項
- 議事録の再生成が行われました
- 元のテキスト（{len(request.raw_text)}文字）を基に生成

## 詳細な議事内容
{request.raw_text[:200]}...（省略）

## 次回のアクション項目
- 特になし

## 備考
"""
            # 会議概要と用語を追加
            if request.meeting_summary:
                formatted_minutes += f"会議の概要: {request.meeting_summary}\n"
            if request.key_terms:
                formatted_minutes += f"キーワード: {request.key_terms}\n"
                
        else:
            # 通常モード: 実際にGPT-4 APIを使用
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

@app.post("/export-to-notion/")
async def export_to_notion(request: ExportToNotionRequest):
    """
    議事録をNotionにエクスポートする
    """
    try:
        logger.info(f"Notionエクスポート開始: {request.title}")
        
        # 必須パラメータの検証
        if not request.token:
            return JSONResponse(
                status_code=400,
                content={"error": "Notion APIトークンが必要です"}
            )
        
        if not request.database_id:
            return JSONResponse(
                status_code=400,
                content={"error": "NotionデータベースIDが必要です"}
            )
        
        from notion_client import Client
        
        # Notion APIクライアントを初期化
        notion = Client(auth=request.token)
        
        try:
            # データベースが存在することを確認
            database = notion.databases.retrieve(database_id=request.database_id)
            logger.info(f"データベース確認: {database.get('title', [{}])[0].get('plain_text', '不明なデータベース')}")
        except Exception as e:
            logger.error(f"Notionデータベース取得エラー: {str(e)}")
            return JSONResponse(
                status_code=400,
                content={"error": f"データベースの取得に失敗しました: {str(e)}"}
            )
        
        # Notionページのプロパティを準備
        properties = {
            "title": {
                "title": [
                    {
                        "text": {
                            "content": request.title
                        }
                    }
                ]
            },
            "作成日": {
                "date": {
                    "start": datetime.now().isoformat()
                }
            }
        }
        
        # コンテンツのブロックを作成
        blocks = []
        
        # マークダウンコンテンツを処理
        md_lines = request.content.split('\n')
        
        current_list_items = []
        current_list_type = None
        
        for line in md_lines:
            line = line.strip()
            
            # 空行の場合はリストを終了
            if not line and current_list_items:
                if current_list_type == "bulleted":
                    blocks.append({
                        "object": "block",
                        "type": "bulleted_list_item",
                        "bulleted_list_item": {
                            "rich_text": [{"type": "text", "text": {"content": current_list_items[0]}}]
                        }
                    })
                elif current_list_type == "numbered":
                    blocks.append({
                        "object": "block",
                        "type": "numbered_list_item",
                        "numbered_list_item": {
                            "rich_text": [{"type": "text", "text": {"content": current_list_items[0]}}]
                        }
                    })
                
                current_list_items = []
                current_list_type = None
                continue
                
            # 見出し1
            if line.startswith('# '):
                blocks.append({
                    "object": "block",
                    "type": "heading_1",
                    "heading_1": {
                        "rich_text": [{"type": "text", "text": {"content": line[2:]}}]
                    }
                })
            
            # 見出し2
            elif line.startswith('## '):
                blocks.append({
                    "object": "block",
                    "type": "heading_2",
                    "heading_2": {
                        "rich_text": [{"type": "text", "text": {"content": line[3:]}}]
                    }
                })
            
            # 見出し3
            elif line.startswith('### '):
                blocks.append({
                    "object": "block",
                    "type": "heading_3",
                    "heading_3": {
                        "rich_text": [{"type": "text", "text": {"content": line[4:]}}]
                    }
                })
            
            # 箇条書き
            elif line.startswith('- '):
                blocks.append({
                    "object": "block",
                    "type": "bulleted_list_item",
                    "bulleted_list_item": {
                        "rich_text": [{"type": "text", "text": {"content": line[2:]}}]
                    }
                })
            
            # 空行でない通常のテキスト
            elif line:
                blocks.append({
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {
                        "rich_text": [{"type": "text", "text": {"content": line}}]
                    }
                })
        
        # Notionページを作成
        logger.info(f"Notionページ作成中: {request.title}")
        response = notion.pages.create(
            parent={"database_id": request.database_id},
            properties=properties,
            children=blocks
        )
        
        # 作成されたページのURL
        page_id = response["id"]
        page_url = f"https://notion.so/{page_id.replace('-', '')}"
        
        logger.info(f"Notionページ作成成功: {page_url}")
        
        return {
            "success": True,
            "page_id": page_id,
            "page_url": page_url,
            "title": request.title
        }
        
    except Exception as e:
        logger.error(f"Notionエクスポート中にエラー: {str(e)}")
        logger.error(traceback.format_exc())
        
        return JSONResponse(
            status_code=500,
            content={"error": f"Notionエクスポート中にエラーが発生しました: {str(e)}"}
        )

@app.get("/export-info/")
async def get_export_info():
    """
    エクスポートに関する情報を取得する
    """
    try:
        export_options = []
        
        # Google Drive設定の確認
        if GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET:
            export_options.append({
                "type": "google_drive",
                "name": "Google Drive",
                "auth_configured": True,
                "auth_type": "oauth",
                "export_type": "ユーザーのGoogleドライブにエクスポート",
                "message": "Googleアカウントへの認証が必要です"
            })
        
        # サービスアカウント設定の確認
        service_account_file = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE")
        if service_account_file:
            try:
                with open(service_account_file, 'r') as f:
                    service_account_info = json.load(f)
                    
                export_options.append({
                    "type": "google_drive_service",
                    "name": "Google Drive (Service Account)",
                    "auth_configured": True,
                    "auth_type": "service_account",
                    "service_account": service_account_info.get("client_email", "不明"),
                    "project_id": service_account_info.get("project_id", "不明"),
                    "export_type": "サービスアカウント（共有ドキュメント）"
                })
            except Exception as e:
                logger.error(f"サービスアカウント情報の取得中にエラー: {str(e)}")
        
        # Notion APIオプションを追加
        export_options.append({
            "type": "notion",
            "name": "Notion",
            "auth_configured": "user_provided",
            "auth_type": "api_token",
            "export_type": "NotionデータベースにエクスポートCE",
            "message": "Notion API TokenとデータベースIDが必要です"
        })
        
        if not export_options:
            return {
                "success": False,
                "error": "エクスポート設定が見つかりません",
                "auth_configured": False,
                "export_options": []
            }
        
        return {
            "success": True,
            "export_options": export_options
        }
            
    except Exception as e:
        logger.error(f"エクスポート情報の取得中にエラー: {str(e)}")
        return {"success": False, "error": str(e), "auth_configured": False} 