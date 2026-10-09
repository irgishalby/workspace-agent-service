import os
import base64
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

NOTION_TOKEN = os.getenv("NOTION_TOKEN")
BOOKS_PAGE_ID = os.getenv("BOOKS_PAGE_ID")
TASKS_DATABASE_ID = os.getenv("TASKS_DATABASE_ID") or os.getenv("TASKS_PAGE_ID")  # Fallback for old name
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ALLOWED_USER_ID = int(os.getenv("ALLOWED_USER_ID", "0"))

if not all([TELEGRAM_TOKEN, GROQ_API_KEY, NOTION_TOKEN]):
    raise ValueError("Missing critical API keys in environment variables!")

if not ALLOWED_USER_ID:
    raise RuntimeError("Missing env var: ALLOWED_USER_ID")

if not os.path.exists("credentials.json") and os.getenv("GOOGLE_CREDENTIALS_BASE64"):
    creds_bytes = base64.b64decode(os.getenv("GOOGLE_CREDENTIALS_BASE64"))
    with open("credentials.json", "wb") as f:
        f.write(creds_bytes)

# Validate all required variables
required_vars = [
    ("NOTION_TOKEN", NOTION_TOKEN),
    ("BOOKS_PAGE_ID", BOOKS_PAGE_ID),
    ("TASKS_DATABASE_ID", TASKS_DATABASE_ID),
    ("GROQ_API_KEY", GROQ_API_KEY),
    ("TELEGRAM_TOKEN", TELEGRAM_TOKEN),
]

missing = [name for name, val in required_vars if not val]
if missing:
    raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")