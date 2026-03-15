import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise ValueError("Укажите BOT_TOKEN в .env")

ADMIN_IDS_STR = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = [int(x.strip()) for x in ADMIN_IDS_STR.split(",") if x.strip().isdigit()]

# Username бота без @ (для реферальных ссылок). По умолчанию risetestq_bot.
BOT_USERNAME = (os.getenv("BOT_USERNAME", "risetestq_bot") or "risetestq_bot").strip().lstrip("@")

DB_PATH = Path(__file__).resolve().parent / "participants.db"
# Файл чек-листа (бонус после регистрации). Положи checklist.txt в папку проекта или укажи свой путь.
CHECKLIST_PATH = Path(__file__).resolve().parent / "checklist.txt"
