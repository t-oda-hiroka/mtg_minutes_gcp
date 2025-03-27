# Codebase Guidelines for Claude

## Project Overview
This is a FastAPI application that uses OpenAI's Whisper and GPT models to create meeting minutes from audio files, with Google Drive/Docs integration.

## Development Commands
- **Run application locally**: `uvicorn main:app --reload`
- **Build Docker image**: `docker build -t mtg_minutes_app .`
- **Run Docker container**: `docker run -p 8080:8080 mtg_minutes_app`
- **Deploy to GCP**: `gcloud builds submit --tag gcr.io/your-project-id/mtg-minutes`

## Code Style Guidelines
- **Imports**: Group standard library imports first, then third-party packages, then local modules
- **Formatting**: Use 4 spaces for indentation
- **Typing**: Use type hints for function parameters and return values (from `typing` module)
- **Naming**: 
  - Use snake_case for variables and functions
  - Use PascalCase for classes and Pydantic models
- **Error Handling**: Use try/except blocks with specific exception types and proper logging
- **Logging**: Use the logging module with appropriate levels (info, error, etc.)
- **Environment Variables**: Load from .env file using python-dotenv, check for required variables

## API Guidelines
- Use Pydantic models for request/response validation
- Implement proper error handling with appropriate HTTP status codes
- Use dependency injection when appropriate
- Document API endpoints with docstrings

## 言語設定
- ユーザーとのやりとりは日本語で行う
- コメントやログメッセージも日本語で記述する
- エラーメッセージも日本語で表示する