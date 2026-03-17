"""
Сохранение участников розыгрыша и выдача порядкового номера.
Оптимизации под нагрузку: WAL, таймаут, кэш админов.
"""
import time
import aiosqlite
from pathlib import Path
from typing import Optional

from config import DB_PATH, ADMIN_IDS as CONFIG_ADMIN_IDS, DB_TIMEOUT

# Кэш списка админов (редко меняется) — меньше обращений к БД при каждом сообщении
_ADMIN_IDS_CACHE = None
_ADMIN_IDS_CACHE_TIME = 0
_ADMIN_USERNAMES_CACHE = None
_ADMIN_USERNAMES_CACHE_TIME = 0
ADMIN_CACHE_TTL = 60  # секунд


def _connect():
    return aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT)

INIT_SQL = """
CREATE TABLE IF NOT EXISTS participants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    participant_number INTEGER NOT NULL UNIQUE,
    telegram_id INTEGER NOT NULL UNIQUE,
    username TEXT,
    name TEXT NOT NULL,
    age TEXT NOT NULL,
    occupation TEXT NOT NULL,
    goal TEXT NOT NULL,
    phone TEXT NOT NULL,
    created_at TEXT NOT NULL,
    left_at TEXT NULL,
    source TEXT NULL
);
CREATE INDEX IF NOT EXISTS idx_participants_telegram_id ON participants(telegram_id);
"""

INIT_VISITS_SQL = """
CREATE TABLE IF NOT EXISTS visits (
    telegram_id INTEGER PRIMARY KEY,
    source TEXT NULL,
    first_seen_at TEXT NOT NULL
);
"""

INIT_ADMINS_SQL = """
CREATE TABLE IF NOT EXISTS admins (
    telegram_id INTEGER NULL UNIQUE,
    username TEXT NULL UNIQUE
);
"""


async def init_db() -> None:
    global _ADMIN_IDS_CACHE, _ADMIN_IDS_CACHE_TIME, _ADMIN_USERNAMES_CACHE, _ADMIN_USERNAMES_CACHE_TIME
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with _connect() as db:
        await db.executescript(INIT_SQL)
        await db.executescript(INIT_VISITS_SQL)
        await db.executescript(INIT_ADMINS_SQL)
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA busy_timeout=15000")
        await db.commit()
    for col in ("left_at", "source"):
        try:
            async with _connect() as db:
                await db.execute(f"ALTER TABLE participants ADD COLUMN {col} TEXT NULL")
                await db.commit()
        except aiosqlite.OperationalError:
            pass
    try:
        async with _connect() as db:
            await db.executescript(INIT_ADMINS_SQL)
            await db.commit()
    except aiosqlite.OperationalError:
        pass
    _ADMIN_IDS_CACHE = _ADMIN_USERNAMES_CACHE = None
    _ADMIN_IDS_CACHE_TIME = _ADMIN_USERNAMES_CACHE_TIME = 0


# --- Дополнительные админы (добавляются через бота) ---

async def get_all_admin_ids() -> list:
    """ID всех админов: из config + из таблицы admins. С кэшем на 60 сек."""
    global _ADMIN_IDS_CACHE, _ADMIN_IDS_CACHE_TIME
    if _ADMIN_IDS_CACHE is not None and (time.time() - _ADMIN_IDS_CACHE_TIME) < ADMIN_CACHE_TTL:
        return _ADMIN_IDS_CACHE
    async with _connect() as db:
        cursor = await db.execute(
            "SELECT telegram_id FROM admins WHERE telegram_id IS NOT NULL"
        )
        rows = await cursor.fetchall()
    _ADMIN_IDS_CACHE = list(CONFIG_ADMIN_IDS) + [r[0] for r in rows]
    _ADMIN_IDS_CACHE_TIME = time.time()
    return _ADMIN_IDS_CACHE


async def get_admin_usernames() -> list:
    """Username'ы админов из таблицы (без @, lowercase). С кэшем на 60 сек."""
    global _ADMIN_USERNAMES_CACHE, _ADMIN_USERNAMES_CACHE_TIME
    if _ADMIN_USERNAMES_CACHE is not None and (time.time() - _ADMIN_USERNAMES_CACHE_TIME) < ADMIN_CACHE_TTL:
        return _ADMIN_USERNAMES_CACHE
    async with _connect() as db:
        cursor = await db.execute(
            "SELECT username FROM admins WHERE username IS NOT NULL"
        )
        rows = await cursor.fetchall()
    _ADMIN_USERNAMES_CACHE = [(r[0] or "").strip().lower() for r in rows if (r[0] or "").strip()]
    _ADMIN_USERNAMES_CACHE_TIME = time.time()
    return _ADMIN_USERNAMES_CACHE


def _invalidate_admin_cache() -> None:
    global _ADMIN_IDS_CACHE, _ADMIN_IDS_CACHE_TIME, _ADMIN_USERNAMES_CACHE, _ADMIN_USERNAMES_CACHE_TIME
    _ADMIN_IDS_CACHE = _ADMIN_USERNAMES_CACHE = None
    _ADMIN_IDS_CACHE_TIME = _ADMIN_USERNAMES_CACHE_TIME = 0


async def add_admin_by_id(telegram_id: int) -> bool:
    """Добавить админа по ID. Возвращает True если добавлен, False если уже есть."""
    try:
        async with _connect() as db:
            await db.execute(
                "INSERT INTO admins (telegram_id, username) VALUES (?, NULL)",
                (telegram_id,),
            )
            await db.commit()
        _invalidate_admin_cache()
        return True
    except aiosqlite.IntegrityError:
        return False


async def add_admin_by_username(username: str) -> bool:
    """Добавить админа по username (без @). Возвращает True если добавлен."""
    username = (username or "").strip().lstrip("@").lower()
    if not username:
        return False
    try:
        async with _connect() as db:
            await db.execute(
                "INSERT INTO admins (telegram_id, username) VALUES (NULL, ?)",
                (username,),
            )
            await db.commit()
        _invalidate_admin_cache()
        return True
    except aiosqlite.IntegrityError:
        return False


async def get_participant_by_telegram_id(telegram_id: int) -> Optional[dict]:
    """Участник по telegram_id (в т.ч. вышедший). В словаре есть left_at — если не None, участник вышел."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT participant_number, name, phone, created_at, left_at, source FROM participants WHERE telegram_id = ?",
            (telegram_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        d = dict(row)
        if "left_at" not in d:
            d["left_at"] = None
        if "source" not in d:
            d["source"] = None
        return d


async def get_next_participant_number() -> int:
    """Следующий порядковый номер участника (1, 2, 3, ...)."""
    async with _connect() as db:
        cursor = await db.execute(
            "SELECT COALESCE(MAX(participant_number), 0) + 1 FROM participants"
        )
        row = await cursor.fetchone()
        return row[0] if row else 1


async def save_participant(
    telegram_id: int,
    username: Optional[str],
    name: str,
    age: str,
    occupation: str,
    goal: str,
    phone: str,
    source: Optional[str] = None,
) -> int:
    """
    Сохраняет участника и возвращает его номер (participant_number).
    source — с какой ссылки зашёл (например ac1, ac2 из t.me/bot?start=ac1).
    """
    from datetime import datetime

    existing = await get_participant_by_telegram_id(telegram_id)
    if existing and not existing.get("left_at"):
        return existing["participant_number"]

    num = await get_next_participant_number()
    created_at = datetime.utcnow().isoformat() + "Z"
    src = (source or "").strip()[:100] or None
    async with _connect() as db:
        await db.execute(
            """
            INSERT INTO participants
            (participant_number, telegram_id, username, name, age, occupation, goal, phone, created_at, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (num, telegram_id, username or "", name, age, occupation, goal, phone, created_at, src),
        )
        await db.commit()
    return num


async def get_stats() -> dict:
    """Статистика: всего активных, за сегодня, по целям (участники, не вышедшие)."""
    from datetime import datetime
    today = datetime.utcnow().strftime("%Y-%m-%d")
    async with _connect() as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM participants WHERE left_at IS NULL"
        )
        total = (await cursor.fetchone())[0]
        cursor = await db.execute(
            "SELECT COUNT(*) FROM participants WHERE left_at IS NULL AND created_at LIKE ?",
            (today + "%",),
        )
        today_count = (await cursor.fetchone())[0]
        cursor = await db.execute(
            "SELECT goal, COUNT(*) as cnt FROM participants WHERE left_at IS NULL GROUP BY goal ORDER BY cnt DESC"
        )
        by_goal = [{"goal": row[0], "count": row[1]} for row in await cursor.fetchall()]
        # По источникам (ссылка): ac1, ac2 и т.д.
        cursor = await db.execute(
            "SELECT COALESCE(source, '—') as src, COUNT(*) as cnt FROM participants WHERE left_at IS NULL GROUP BY source ORDER BY cnt DESC"
        )
        by_source = [{"source": row[0], "count": row[1]} for row in await cursor.fetchall()]
    return {"total": total, "today": today_count, "by_goal": by_goal, "by_source": by_source}


async def leave_contest(telegram_id: int) -> bool:
    """Отметить участника как вышедшего. Возвращает True, если выход выполнен, False если уже вышел или не найден."""
    from datetime import datetime
    left_at = datetime.utcnow().isoformat() + "Z"
    async with _connect() as db:
        cursor = await db.execute(
            "UPDATE participants SET left_at = ? WHERE telegram_id = ? AND left_at IS NULL",
            (left_at, telegram_id),
        )
        await db.commit()
        return cursor.rowcount > 0


async def get_participants_by_numbers(numbers: list) -> list[dict]:
    """Участники по списку номеров (participant_number). Возвращает полные данные."""
    if not numbers:
        return []
    placeholders = ",".join("?" * len(numbers))
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT participant_number, telegram_id, username, name, age, occupation, goal, phone, created_at, left_at, source
            FROM participants
            WHERE participant_number IN ({})
            ORDER BY participant_number
            """.format(placeholders),
            numbers,
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_all_participants() -> list[dict]:
    """Все участники для выгрузки в Excel."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT participant_number, telegram_id, username, name, age, occupation, goal, phone, created_at, left_at, source
            FROM participants
            ORDER BY participant_number
            """
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


# --- Визиты без регистрации (зашли и вышли) ---

async def add_visit(telegram_id: int, source: Optional[str] = None) -> None:
    """Учёт визита: зашёл в бота (по ссылке или просто /start). Только первый визит сохраняем."""
    from datetime import datetime
    first_seen = datetime.utcnow().isoformat() + "Z"
    src = (source or "").strip()[:100] or None
    async with _connect() as db:
        await db.execute(
            "INSERT OR IGNORE INTO visits (telegram_id, source, first_seen_at) VALUES (?, ?, ?)",
            (telegram_id, src, first_seen),
        )
        await db.commit()


async def get_visitors_not_registered() -> list[dict]:
    """Визиты тех, кто зашёл в бота, но так и не зарегистрировался (нет в participants)."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT v.telegram_id, v.source, v.first_seen_at
            FROM visits v
            LEFT JOIN participants p ON p.telegram_id = v.telegram_id
            WHERE p.telegram_id IS NULL
            ORDER BY v.first_seen_at
            """
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def wipe_database() -> None:
    """Полная очистка базы: участники, визиты. Все данные удаляются безвозвратно."""
    async with _connect() as db:
        await db.execute("DELETE FROM participants")
        await db.execute("DELETE FROM visits")
        await db.execute("DELETE FROM sqlite_sequence WHERE name IN ('participants', 'visits')")
        await db.commit()


async def get_visitor_stats() -> dict:
    """Статистика по визитам без регистрации: всего и по источникам."""
    async with _connect() as db:
        cursor = await db.execute(
            """
            SELECT COUNT(*) FROM visits v
            LEFT JOIN participants p ON p.telegram_id = v.telegram_id
            WHERE p.telegram_id IS NULL
            """
        )
        total = (await cursor.fetchone())[0]
        cursor = await db.execute(
            """
            SELECT COALESCE(v.source, '—') as src, COUNT(*) as cnt
            FROM visits v
            LEFT JOIN participants p ON p.telegram_id = v.telegram_id
            WHERE p.telegram_id IS NULL
            GROUP BY v.source
            ORDER BY cnt DESC
            """
        )
        by_source = [{"source": row[0], "count": row[1]} for row in await cursor.fetchall()]
    return {"total": total, "by_source": by_source}
