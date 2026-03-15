import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise ValueError("Укажите BOT_TOKEN в .env")

# Админ: только из кода (без переменной окружения). Добавь сюда свой ID и других админов.
ADMIN_IDS = [7392364029]

# Username бота без @ (для реферальных ссылок). По умолчанию risetestq_bot.
BOT_USERNAME = (os.getenv("BOT_USERNAME", "risetestq_bot") or "risetestq_bot").strip().lstrip("@")

DB_PATH = Path(__file__).resolve().parent / "participants.db"
# Файл чек-листа (бонус после регистрации). Положи checklist.txt в папку проекта или укажи свой путь.
CHECKLIST_PATH = Path(__file__).resolve().parent / "checklist.txt"
