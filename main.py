import os
from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.requests import Request
import tempfile
import shutil
from datetime import datetime
from openai import OpenAI
from dotenv import load_dotenv
import logging

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

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.post("/transcribe/")
async def transcribe_audio(
    file: UploadFile = File(...),
    meeting_summary: str = "",
    key_terms: str = ""
):
    try:
        # ファイル形式のチェック
        allowed_extensions = {'.mp3', '.mp4', '.mpeg', '.mpga', '.m4a', '.wav', '.webm'}  # GPT-4o Transcribeがサポートする形式
        file_ext = os.path.splitext(file.filename)[1].lower()
        if file_ext not in allowed_extensions:
            raise HTTPException(
                status_code=400,
                detail=f"サポートされていないファイル形式です。対応形式: {', '.join(allowed_extensions)}"
            )

        # initial_promptの作成
        initial_prompt = "これは会議の録音です。"
        if meeting_summary:
            initial_prompt += f" {meeting_summary}"
        if key_terms:
            initial_prompt += f" この会議では以下の用語や人物が登場する可能性があります: {key_terms}"

        # 一時ファイルの作成
        with tempfile.NamedTemporaryFile(delete=False, suffix=file_ext) as temp_file:
            try:
                # ファイルの保存
                shutil.copyfileobj(file.file, temp_file)
                temp_path = temp_file.name
                logger.info(f"一時ファイルを作成: {temp_path}")

                # ファイルサイズのチェック
                file_size = os.path.getsize(temp_path)
                logger.info(f"ファイルサイズ: {file_size} bytes")

                # OpenAI APIを使用して音声認識を実行
                with open(temp_path, "rb") as audio_file:
                    logger.info("音声認識を開始")
                    transcript = client.audio.transcriptions.create(
                        model="whisper-1",  # 一時的にWhisperモデルを使用
                        file=audio_file,
                        language="ja",
                        prompt=initial_prompt
                    )
                    logger.info("音声認識が完了")

                # 文字起こしテキストを保存
                raw_text = transcript.text

                # GPT-4を使用して議事録を生成
                logger.info("議事録の生成を開始")
                completion = client.chat.completions.create(
                    model="gpt-4-turbo-preview",
                    messages=[
                        {"role": "system", "content": """あなたは優秀な議事録作成者です。
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
"""},
                        {"role": "user", "content": raw_text}
                    ]
                )
                
                formatted_minutes = completion.choices[0].message.content
                logger.info("議事録の生成が完了")

                return {
                    "raw_text": raw_text,
                    "minutes": formatted_minutes
                }

            except Exception as e:
                logger.error(f"エラーが発生: {str(e)}")
                raise HTTPException(status_code=500, detail=f"処理中にエラーが発生しました: {str(e)}")
            finally:
                # 一時ファイルの削除
                try:
                    os.unlink(temp_path)
                    logger.info("一時ファイルを削除")
                except Exception as e:
                    logger.error(f"一時ファイルの削除中にエラー: {str(e)}")

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