from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parents[1]
load_dotenv(ROOT_DIR / ".env")

UPLOADS_DIR = ROOT_DIR / "uploads"
OUTPUTS_DIR = ROOT_DIR / "outputs"
CREDENTIALS_DIR = ROOT_DIR / "credentials"
SCHEDULE_FILE = OUTPUTS_DIR / "scheduled_posts.json"

SUPPORTED_AUDIO = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}
SUPPORTED_IMAGES = {".png", ".jpg", ".jpeg", ".webp"}

# Groq API: usada para gerar título, descrição, tags, hashtags, palavras-chave e comentário fixado.
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
GROQ_TEXT_MODEL = os.getenv("GROQ_TEXT_MODEL", "llama-3.3-70b-versatile").strip()
GROQ_TEMPERATURE = float(os.getenv("GROQ_TEMPERATURE", "0.85"))
GROQ_MAX_TOKENS = int(os.getenv("GROQ_MAX_TOKENS", "2500"))

YOUTUBE_CLIENT_SECRETS = Path(
    os.getenv("YOUTUBE_CLIENT_SECRETS", str(CREDENTIALS_DIR / "client_secret.json"))
)
if not YOUTUBE_CLIENT_SECRETS.is_absolute():
    YOUTUBE_CLIENT_SECRETS = ROOT_DIR / YOUTUBE_CLIENT_SECRETS

YOUTUBE_TOKEN_FILE = Path(
    os.getenv("YOUTUBE_TOKEN_FILE", str(CREDENTIALS_DIR / "youtube_token.json"))
)
if not YOUTUBE_TOKEN_FILE.is_absolute():
    YOUTUBE_TOKEN_FILE = ROOT_DIR / YOUTUBE_TOKEN_FILE

VIDEO_WIDTH = int(os.getenv("VIDEO_WIDTH", "1920"))
VIDEO_HEIGHT = int(os.getenv("VIDEO_HEIGHT", "1080"))
VIDEO_FPS = int(os.getenv("VIDEO_FPS", "30"))
VIDEO_CRF = int(os.getenv("VIDEO_CRF", "18"))

SHORTS_VIDEO_WIDTH = int(os.getenv("SHORTS_VIDEO_WIDTH", "1080"))
SHORTS_VIDEO_HEIGHT = int(os.getenv("SHORTS_VIDEO_HEIGHT", "1920"))

# YouTube classifica Shorts por duração e proporção. O limite oficial é 3 minutos,
# mas deixamos 179s por padrão para evitar que arredondamentos de encoder/metadata
# gerem um arquivo com 180.01s e o YouTube não reconheça como Shorts.
SHORTS_MAX_DURATION_SECONDS = float(os.getenv("SHORTS_MAX_DURATION_SECONDS", "179"))

# TikTok Content Posting API: use um User Access Token OAuth, não apenas o Client Key do app.
# O app procura o token nesta ordem: override na tela, .env e arquivos JSON em credentials/.
TIKTOK_ACCESS_TOKEN = os.getenv("TIKTOK_ACCESS_TOKEN", "").strip()
TIKTOK_REFRESH_TOKEN = os.getenv("TIKTOK_REFRESH_TOKEN", "").strip()
TIKTOK_CLIENT_KEY = (os.getenv("TIKTOK_CLIENT_KEY") or os.getenv("TIKTOK_CLIENT_ID") or "").strip()
TIKTOK_CLIENT_SECRET = os.getenv("TIKTOK_CLIENT_SECRET", "").strip()
TIKTOK_TOKEN_FILE = Path(os.getenv("TIKTOK_TOKEN_FILE", str(CREDENTIALS_DIR / ".tiktok_token.json")))
if not TIKTOK_TOKEN_FILE.is_absolute():
    TIKTOK_TOKEN_FILE = ROOT_DIR / TIKTOK_TOKEN_FILE
TIKTOK_DEFAULT_PRIVACY_LEVEL = os.getenv("TIKTOK_DEFAULT_PRIVACY_LEVEL", "SELF_ONLY").strip() or "SELF_ONLY"

# Fuso usado nos campos de agendamento do app.
DEFAULT_TIMEZONE = os.getenv("DEFAULT_TIMEZONE", "America/Sao_Paulo").strip() or "America/Sao_Paulo"

# Segurança do app Streamlit. Configure pelo .env local ou pelo Secrets do Streamlit Cloud.
APP_AUTH_ENABLED = os.getenv("APP_AUTH_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
APP_USERNAME = os.getenv("APP_USERNAME", "admin").strip() or "admin"
APP_PASSWORD = os.getenv("APP_PASSWORD", "").strip()
APP_ALLOWED_IPS = [ip.strip() for ip in os.getenv("APP_ALLOWED_IPS", "").split(",") if ip.strip()]
APP_IP_STRICT = os.getenv("APP_IP_STRICT", "true").strip().lower() in {"1", "true", "yes", "on"}

# Fila local de agendamento TikTok via Direct Post.
TIKTOK_SCHEDULER_ENABLED = os.getenv("TIKTOK_SCHEDULER_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
TIKTOK_QUEUE_POLL_SECONDS = max(15, int(os.getenv("TIKTOK_QUEUE_POLL_SECONDS", "60")))
