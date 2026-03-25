"""
Сохранение участников розыгрыша и выдача порядкового номера.
Оптимизации под нагрузку: WAL, таймаут, кэш админов.
"""
import asyncio
import time
import aiosqlite
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from config import DB_PATH, ADMIN_IDS as CONFIG_ADMIN_IDS, DB_TIMEOUT

# Кэш списка админов (редко меняется) — меньше обращений к БД при каждом сообщении
_ADMIN_IDS_CACHE = None
_ADMIN_IDS_CACHE_TIME = 0
_ADMIN_USERNAMES_CACHE = None
_ADMIN_USERNAMES_CACHE_TIME = 0
ADMIN_CACHE_TTL = 60  # секунд


@asynccontextmanager
async def _connect():
    """Одно соединение на блок `async with`; PRAGMA busy_timeout на каждое открытие (иначе «database is locked»)."""
    async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
        await db.execute(f"PRAGMA busy_timeout={int(DB_TIMEOUT * 1000)}")
        yield db


def canonical_event_iso(iso_str: Optional[str]) -> Optional[str]:
    """Единый формат UTC ISO для сравнения даты эфира (избегаем рассинхрона snapshot / settings)."""
    if not iso_str or not str(iso_str).strip():
        return None
    s = str(iso_str).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()

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

INIT_CONTEST_SQL = """
CREATE TABLE IF NOT EXISTS contest_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    event_at TEXT,
    contest_link TEXT,
    first_touch_text TEXT,
    postponement_notice TEXT
);
INSERT OR IGNORE INTO contest_settings (id, event_at, contest_link, first_touch_text, postponement_notice)
VALUES (1, NULL, '', '', '');
CREATE TABLE IF NOT EXISTS contest_reminders (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    event_at_snapshot TEXT,
    sent_2h INTEGER NOT NULL DEFAULT 0,
    sent_15m INTEGER NOT NULL DEFAULT 0,
    sent_5m INTEGER NOT NULL DEFAULT 0,
    last_daily_msk TEXT NULL
);
INSERT OR IGNORE INTO contest_reminders (id, event_at_snapshot, sent_2h, sent_15m, sent_5m)
VALUES (1, NULL, 0, 0, 0);
"""

INIT_BOT_SERVICE_SQL = """
CREATE TABLE IF NOT EXISTS bot_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    maintenance_enabled INTEGER NOT NULL DEFAULT 0,
    maintenance_message TEXT NOT NULL DEFAULT ''
);
INSERT OR IGNORE INTO bot_settings (id, maintenance_enabled, maintenance_message) VALUES (1, 0, '');
CREATE TABLE IF NOT EXISTS maintenance_allowlist (
    telegram_id INTEGER NOT NULL PRIMARY KEY
);
"""

DEFAULT_MAINTENANCE_MESSAGE = (
    "Ведутся технические работы. Пожалуйста, зайдите позже."
)


async def init_db() -> None:
    global _ADMIN_IDS_CACHE, _ADMIN_IDS_CACHE_TIME, _ADMIN_USERNAMES_CACHE, _ADMIN_USERNAMES_CACHE_TIME
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with _connect() as db:
        await db.executescript(INIT_SQL)
        await db.executescript(INIT_VISITS_SQL)
        await db.executescript(INIT_ADMINS_SQL)
        await db.executescript(INIT_CONTEST_SQL)
        await db.executescript(INIT_BOT_SERVICE_SQL)
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
            await db.execute("ALTER TABLE contest_reminders ADD COLUMN last_daily_msk TEXT NULL")
            await db.commit()
    except aiosqlite.OperationalError:
        pass
    try:
        async with _connect() as db:
            await db.executescript(INIT_ADMINS_SQL)
            await db.commit()
    except aiosqlite.OperationalError:
        pass
    await ensure_contest_reminders_row()
    _ADMIN_IDS_CACHE = _ADMIN_USERNAMES_CACHE = None
    _ADMIN_IDS_CACHE_TIME = _ADMIN_USERNAMES_CACHE_TIME = 0
    try:
        async with _connect() as db:
            await db.execute(
                "ALTER TABLE bot_settings ADD COLUMN broadcasts_enabled INTEGER NOT NULL DEFAULT 1"
            )
            await db.commit()
    except aiosqlite.OperationalError:
        pass


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


async def get_participants_by_source(source: str) -> list[dict]:
    """
    Только чтение: участники, у которых при регистрации указан источник (например ac1, ac2).
    Данные в БД не изменяются.
    """
    src = (source or "").strip()[:100]
    if not src:
        return []
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT participant_number, telegram_id, username, name, age, occupation, goal, phone,
                   created_at, left_at, source
            FROM participants
            WHERE source = ?
            ORDER BY participant_number
            """,
            (src,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_visitors_by_source_not_registered(source: str) -> list[dict]:
    """
    Только чтение: визиты без регистрации с указанным источником (зашли по ссылке и ушли).
    """
    src = (source or "").strip()[:100]
    if not src:
        return []
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT v.telegram_id, v.source, v.first_seen_at
            FROM visits v
            LEFT JOIN participants p ON p.telegram_id = v.telegram_id
            WHERE p.telegram_id IS NULL AND v.source = ?
            ORDER BY v.first_seen_at
            """,
            (src,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


_SKIP = object()


def _event_at_for_api(raw_ev: Optional[str]) -> Optional[str]:
    """Для API/бота: канонический ISO; если парсер не съел — отдаём как в БД (ничего не теряем)."""
    if not raw_ev or not str(raw_ev).strip():
        return None
    can = canonical_event_iso(raw_ev)
    return can if can is not None else str(raw_ev).strip()


async def get_contest_settings() -> dict:
    """Настройки розыгрыша: дата/время эфира (ISO UTC), ссылка, тексты рассылок. Всё из SQLite."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT event_at, contest_link, first_touch_text, postponement_notice FROM contest_settings WHERE id = 1"
        )
        row = await cursor.fetchone()
        if not row:
            return {
                "event_at": None,
                "contest_link": "",
                "first_touch_text": "",
                "postponement_notice": "",
            }
        d = dict(row)
        raw_ev = d.get("event_at")
        return {
            "event_at": _event_at_for_api(raw_ev),
            "contest_link": (d.get("contest_link") or "") or "",
            "first_touch_text": (d.get("first_touch_text") or "") or "",
            "postponement_notice": (d.get("postponement_notice") or "") or "",
        }


async def ensure_contest_reminders_row() -> None:
    """
    Без строки id=1 UPDATE contest_reminders не меняет ни одной записи — снимок и флаги
    не сохраняются, напоминания могли не уходить вообще.
    """
    async with _connect() as db:
        await db.execute(
            """
            INSERT OR IGNORE INTO contest_reminders (id, event_at_snapshot, sent_2h, sent_15m, sent_5m, last_daily_msk)
            VALUES (1, NULL, 0, 0, 0, NULL)
            """
        )
        await db.commit()


async def save_contest_settings(
    event_at=_SKIP,
    contest_link=_SKIP,
    first_touch_text=_SKIP,
    postponement_notice=_SKIP,
) -> None:
    """
    Сохраняет только переданные поля. Для сброса даты передайте event_at=None.
    При изменении event_at сбрасываются флаги напоминаний (новый эфир).

    Сырые значения читаются из БД в одном соединении — при правке только ссылки
    дата эфира из строки SQLite не затирается (раньше merge шёл через API-нормализацию).
    """
    async with _connect() as db:
        cursor = await db.execute(
            "SELECT event_at, contest_link, first_touch_text, postponement_notice FROM contest_settings WHERE id = 1"
        )
        row = await cursor.fetchone()
        if not row:
            await db.execute(
                """
                INSERT INTO contest_settings (id, event_at, contest_link, first_touch_text, postponement_notice)
                VALUES (1, NULL, '', '', '')
                """
            )
            old_ev, old_link, old_first, old_post = None, "", "", ""
        else:
            old_ev, old_link, old_first, old_post = row[0], row[1], row[2], row[3]

        if event_at is _SKIP:
            new_event = old_ev
        elif event_at is None:
            new_event = None
        else:
            new_event = canonical_event_iso(event_at)
            if new_event is None:
                new_event = str(event_at).strip()[:200] or None

        new_link = old_link if contest_link is _SKIP else contest_link
        new_first = old_first if first_touch_text is _SKIP else first_touch_text
        new_post = old_post if postponement_notice is _SKIP else postponement_notice

        old_can = canonical_event_iso(old_ev) if old_ev else None
        new_can = canonical_event_iso(new_event) if new_event else None
        event_changed = event_at is not _SKIP and (old_can != new_can or (old_can is None) != (new_can is None))

        await db.execute(
            """
            UPDATE contest_settings
            SET event_at = ?, contest_link = ?, first_touch_text = ?, postponement_notice = ?
            WHERE id = 1
            """,
            (new_event, new_link, new_first, new_post),
        )
        if event_changed:
            snap = new_can if new_can is not None else new_event
            # Нельзя вызывать ensure_contest_reminders_row() здесь: открывается второе соединение
            # при незакоммиченной транзакции на этом же `db` → SQLite «database is locked».
            await db.execute(
                """
                INSERT OR IGNORE INTO contest_reminders (id, event_at_snapshot, sent_2h, sent_15m, sent_5m, last_daily_msk)
                VALUES (1, NULL, 0, 0, 0, NULL)
                """
            )
            await db.execute(
                """
                UPDATE contest_reminders
                SET sent_2h = 0, sent_15m = 0, sent_5m = 0, event_at_snapshot = ?, last_daily_msk = NULL
                WHERE id = 1
                """,
                (snap,),
            )
        await db.commit()


async def get_contest_reminder_flags() -> dict:
    async with _connect() as db:
        cursor = await db.execute(
            "SELECT event_at_snapshot, sent_2h, sent_15m, sent_5m, last_daily_msk FROM contest_reminders WHERE id = 1"
        )
        row = await cursor.fetchone()
        if not row:
            return {
                "event_at_snapshot": None,
                "sent_2h": 0,
                "sent_15m": 0,
                "sent_5m": 0,
                "last_daily_msk": None,
            }
        return {
            "event_at_snapshot": row[0],
            "sent_2h": row[1],
            "sent_15m": row[2],
            "sent_5m": row[3],
            "last_daily_msk": row[4] if len(row) > 4 else None,
        }


async def mark_contest_reminder_sent(kind: str) -> None:
    await ensure_contest_reminders_row()
    col = {"2h": "sent_2h", "15m": "sent_15m", "5m": "sent_5m"}.get(kind)
    if not col:
        return
    async with _connect() as db:
        await db.execute(f"UPDATE contest_reminders SET {col} = 1 WHERE id = 1")
        await db.commit()


async def mark_last_daily_msk_sent(ymd_msk: str) -> None:
    """Дата YYYY-MM-DD по Москве — чтобы не слать второй раз ежедневное напоминание в 12:00."""
    await ensure_contest_reminders_row()
    async with _connect() as db:
        await db.execute(
            "UPDATE contest_reminders SET last_daily_msk = ? WHERE id = 1",
            (ymd_msk,),
        )
        await db.commit()


async def sync_reminder_snapshot_if_needed(event_at: Optional[str]) -> None:
    """Если снимок не совпадает с текущей датой эфира — сбросить флаги (на случай рассинхрона)."""
    await ensure_contest_reminders_row()
    can = canonical_event_iso(event_at)
    flags = await get_contest_reminder_flags()
    snap_can = canonical_event_iso(flags.get("event_at_snapshot"))
    if snap_can != can:
        async with _connect() as db:
            await db.execute(
                """
                UPDATE contest_reminders
                SET sent_2h = 0, sent_15m = 0, sent_5m = 0, event_at_snapshot = ?, last_daily_msk = NULL
                WHERE id = 1
                """,
                (can,),
            )
            await db.commit()


async def get_active_raffle_recipient_telegram_ids() -> list:
    """Telegram ID участников в розыгрыше (не вышедших) — для рассылок и напоминаний."""
    async with _connect() as db:
        cursor = await db.execute(
            "SELECT telegram_id FROM participants WHERE left_at IS NULL ORDER BY participant_number"
        )
        rows = await cursor.fetchall()
        return [r[0] for r in rows]


async def replace_participants_and_visits_from_import(
    participants: list,
    visitors: list,
) -> None:
    """
    Полностью заменяет таблицы participants и visits данными из импорта Excel
    (формат как у «Выгрузить Excel»). Настройки розыгрыша, админы, bot_settings не трогаем.
    """
    async with _connect() as db:
        await db.execute("DELETE FROM participants")
        await db.execute("DELETE FROM visits")
        await db.execute("DELETE FROM sqlite_sequence WHERE name IN ('participants', 'visits')")
        for p in participants:
            await db.execute(
                """
                INSERT INTO participants
                (participant_number, telegram_id, username, name, age, occupation, goal, phone, created_at, left_at, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    p["participant_number"],
                    p["telegram_id"],
                    p.get("username") or "",
                    p["name"],
                    p["age"],
                    p["occupation"],
                    p["goal"],
                    p["phone"],
                    p["created_at"],
                    p.get("left_at"),
                    (p.get("source") or "").strip()[:100] or None,
                ),
            )
        for v in visitors:
            await db.execute(
                "INSERT OR REPLACE INTO visits (telegram_id, source, first_seen_at) VALUES (?, ?, ?)",
                (
                    v["telegram_id"],
                    (v.get("source") or "").strip()[:100] or None,
                    v["first_seen_at"],
                ),
            )
        await db.commit()


async def get_bot_settings() -> dict:
    """Режим техработ, текст ответа, глобальный переключатель рассылок участникам."""
    async with _connect() as db:
        cursor = await db.execute(
            "SELECT maintenance_enabled, maintenance_message, broadcasts_enabled FROM bot_settings WHERE id = 1"
        )
        row = await cursor.fetchone()
    if not row:
        return {
            "maintenance_enabled": False,
            "maintenance_message": "",
            "broadcasts_enabled": True,
        }
    bc = row[2] if len(row) > 2 else 1
    return {
        "maintenance_enabled": bool(row[0]),
        "maintenance_message": row[1] if row[1] is not None else "",
        "broadcasts_enabled": bool(bc),
    }


async def set_maintenance_enabled(enabled: bool) -> None:
    async with _connect() as db:
        await db.execute(
            "UPDATE bot_settings SET maintenance_enabled = ? WHERE id = 1",
            (1 if enabled else 0,),
        )
        await db.commit()


async def set_maintenance_message(text: str) -> None:
    text = (text or "").strip()[:2000]
    async with _connect() as db:
        await db.execute(
            "UPDATE bot_settings SET maintenance_message = ? WHERE id = 1",
            (text,),
        )
        await db.commit()


async def set_broadcasts_enabled(enabled: bool) -> None:
    """Вкл/выкл все рассылки участникам: автонапоминания и ручные из админки."""
    async with _connect() as db:
        await db.execute(
            "UPDATE bot_settings SET broadcasts_enabled = ? WHERE id = 1",
            (1 if enabled else 0,),
        )
        await db.commit()


async def get_maintenance_reply_text() -> str:
    s = await get_bot_settings()
    t = (s.get("maintenance_message") or "").strip()
    return t if t else DEFAULT_MAINTENANCE_MESSAGE


async def get_maintenance_allowlist_ids() -> list:
    async with _connect() as db:
        cursor = await db.execute(
            "SELECT telegram_id FROM maintenance_allowlist ORDER BY telegram_id"
        )
        rows = await cursor.fetchall()
    return [r[0] for r in rows]


async def add_maintenance_allowlist(telegram_id: int) -> bool:
    """Добавить ID в белый список. False — уже был."""
    try:
        async with _connect() as db:
            await db.execute(
                "INSERT INTO maintenance_allowlist (telegram_id) VALUES (?)",
                (telegram_id,),
            )
            await db.commit()
        return True
    except aiosqlite.IntegrityError:
        return False


async def remove_maintenance_allowlist(telegram_id: int) -> bool:
    """Удалить ID из белого списка. False — не было."""
    async with _connect() as db:
        cursor = await db.execute(
            "DELETE FROM maintenance_allowlist WHERE telegram_id = ?",
            (telegram_id,),
        )
        await db.commit()
        return cursor.rowcount > 0


async def user_passes_maintenance(telegram_id: int, username: Optional[str]) -> bool:
    """Можно обрабатывать апдейт: техработы выкл, или админ, или в белом списке."""
    settings = await get_bot_settings()
    if not settings.get("maintenance_enabled"):
        return True
    admin_ids, admin_usernames = await asyncio.gather(
        get_all_admin_ids(), get_admin_usernames()
    )
    if telegram_id in admin_ids or (
        username and username.lower() in admin_usernames
    ):
        return True
    async with _connect() as db:
        cursor = await db.execute(
            "SELECT 1 FROM maintenance_allowlist WHERE telegram_id = ?",
            (telegram_id,),
        )
        return (await cursor.fetchone()) is not None


async def admin_upsert_participant(
    telegram_id: int,
    username: Optional[str],
    name: str,
    age: str,
    occupation: str,
    goal: str,
    phone: str,
) -> tuple[int, str]:
    """
    Ручное добавление или обновление участника (админка).
    Возвращает (participant_number, 'created'|'updated').
    """
    uname = (username or "").strip().lstrip("@") or ""
    src = "admin"
    created_at = datetime.utcnow().isoformat() + "Z"
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT participant_number FROM participants WHERE telegram_id = ?",
            (telegram_id,),
        )
        row = await cursor.fetchone()
    if not row:
        num = await get_next_participant_number()
        async with _connect() as db:
            await db.execute(
                """
                INSERT INTO participants
                (participant_number, telegram_id, username, name, age, occupation, goal, phone, created_at, left_at, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
                """,
                (num, telegram_id, uname, name, age, occupation, goal, phone, created_at, src),
            )
            await db.commit()
        return num, "created"
    num = row["participant_number"]
    async with _connect() as db:
        await db.execute(
            """
            UPDATE participants
            SET username = ?, name = ?, age = ?, occupation = ?, goal = ?, phone = ?,
                left_at = NULL, source = ?
            WHERE telegram_id = ?
            """,
            (uname, name, age, occupation, goal, phone, src, telegram_id),
        )
        await db.commit()
    return num, "updated"


async def wipe_database() -> None:
    """Полная очистка базы: участники, визиты, настройки розыгрыша. Все данные удаляются безвозвратно."""
    async with _connect() as db:
        await db.execute("DELETE FROM participants")
        await db.execute("DELETE FROM visits")
        await db.execute("DELETE FROM sqlite_sequence WHERE name IN ('participants', 'visits')")
        await db.execute(
            "UPDATE contest_settings SET event_at = NULL, contest_link = '', first_touch_text = '', postponement_notice = '' WHERE id = 1"
        )
        await db.execute(
            "UPDATE contest_reminders SET event_at_snapshot = NULL, sent_2h = 0, sent_15m = 0, sent_5m = 0, last_daily_msk = NULL WHERE id = 1"
        )
        await db.execute(
            "UPDATE bot_settings SET maintenance_enabled = 0, maintenance_message = '', broadcasts_enabled = 1 WHERE id = 1"
        )
        await db.execute("DELETE FROM maintenance_allowlist")
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
