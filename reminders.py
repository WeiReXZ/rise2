"""Фоновые напоминания о розыгрыше (12:00 МСК ежедневно + за 2 ч, 15 мин, 5 мин до эфира).

Глобальное отключение рассылок — в админке «Техработы и доступ» (тогда не уходят ни авто-, ни ручные рассылки участникам).
Ручная рассылка не меняет флаги автонапоминаний, пока рассылки включены.
"""
import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from aiogram import Bot

from database import (
    canonical_event_iso,
    get_contest_settings,
    get_contest_reminder_flags,
    get_bot_settings,
    mark_contest_reminder_sent,
    mark_last_daily_msk_sent,
    sync_reminder_snapshot_if_needed,
    get_active_raffle_recipient_telegram_ids,
)

logger = logging.getLogger(__name__)

# Чтобы не заливать лог: одно и то же предупреждение не чаще чем раз в N секунд
_LOG_THROTTLE: dict[str, float] = {}
_LOG_THROTTLE_SEC = 300.0


def _log_throttled(key: str, level: int, fmt: str, *args) -> None:
    now = time.monotonic()
    last = _LOG_THROTTLE.get(key, 0.0)
    if now - last < _LOG_THROTTLE_SEC:
        return
    _LOG_THROTTLE[key] = now
    logger.log(level, fmt, *args)


REMINDER_2H = (
    "⏰ Напоминаю, что розыгрыш пройдет через два часа на нашем канале"
)
REMINDER_15M = "🔖 15 минут до эфира – осталось совсем ничего"
REMINDER_5M = (
    "🔥5 минут до эфира – год бесплатного английского может стать твоим"
)

TICK_SEC = 10
SEND_DELAY = 0.04
MSK = ZoneInfo("Europe/Moscow")
# Окно 12:00 МСК (первые ~6 минут), чтобы успеть при тике раз в 10 с
DAILY_NOON_MINUTE_END = 6


def parse_event_at_utc(iso_str: str) -> datetime:
    s = iso_str.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def format_event_utc_for_admin_msk(iso_str: str) -> str:
    dt = parse_event_at_utc(iso_str)
    msk = dt.astimezone(ZoneInfo("Europe/Moscow"))
    return msk.strftime("%d.%m.%Y %H:%M (МСК)")


def parse_msk_datetime_to_utc_iso(text: str) -> str:
    """ДД.ММ.ГГГГ ЧЧ:ММ — время по Москве → ISO UTC."""
    text = text.strip()
    parts = text.split()
    if len(parts) < 2:
        raise ValueError("Нужен формат: ДД.ММ.ГГГГ ЧЧ:ММ (время — Москва)")
    d, t = parts[0], parts[1]
    day, month, year = map(int, d.split("."))
    hparts = t.split(":")
    hour = int(hparts[0])
    minute = int(hparts[1]) if len(hparts) > 1 else 0
    dt = datetime(year, month, day, hour, minute, tzinfo=ZoneInfo("Europe/Moscow"))
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _ru_days_left(n: int) -> str:
    n = int(n)
    if n <= 0:
        return "Скоро эфир."
    if n % 10 == 1 and n % 100 != 11:
        return f"До эфира остался {n} день."
    if 2 <= n % 10 <= 4 and (n % 100 < 10 or n % 100 >= 20):
        return f"До эфира осталось {n} дня."
    return f"До эфира осталось {n} дней."


def _with_optional_link(body: str, link: str) -> str:
    link = (link or "").strip()
    if link:
        return f"{body}\n\n{link}"
    return body


def _threshold_reached(now_utc: datetime, dt_utc: datetime, delta: timedelta) -> bool:
    """Момент «за N до эфира» наступил: сравнение по unix-секундам (без гонок микросекунд)."""
    thr = dt_utc - delta
    return int(now_utc.timestamp()) >= int(thr.timestamp())


def _reminder_already_sent(flags: dict, col: str) -> bool:
    """Флаг из SQLite: только 1 = уже отправляли (0/None/строка '0' — нет)."""
    v = flags.get(col)
    if v is None:
        return False
    try:
        return int(v) == 1
    except (TypeError, ValueError):
        return bool(v)


async def _send_to_all(bot: Bot, text: str) -> tuple[int, int]:
    """Возвращает (успешно доставлено, всего адресатов)."""
    ids = await get_active_raffle_recipient_telegram_ids()
    if not ids:
        logger.warning(
            "Авторассылка: получателей 0 — в таблице participants нет строк с left_at IS NULL "
            "(никто не зарегистрирован или все вышли из конкурса)."
        )
        return 0, 0
    ok = 0
    for uid in ids:
        try:
            await bot.send_message(uid, text)
            ok += 1
        except Exception as e:
            logger.warning("Напоминание: не отправлено %s: %s", uid, e)
        await asyncio.sleep(SEND_DELAY)
    return ok, len(ids)


async def process_daily_noon_msk(bot: Bot, dt_utc: datetime, link: str) -> None:
    """Раз в календарный день по Москве в ~12:00 — счётчик дней или часов до эфира."""
    now_msk = datetime.now(MSK)
    if now_msk.hour != 12 or now_msk.minute >= DAILY_NOON_MINUTE_END:
        return

    today_s = now_msk.date().isoformat()
    flags = await get_contest_reminder_flags()
    if flags.get("last_daily_msk") == today_s:
        return

    event_msk = dt_utc.astimezone(MSK)
    if now_msk >= event_msk:
        return

    d_now = now_msk.date()
    d_ev = event_msk.date()

    if d_now < d_ev:
        days = (d_ev - d_now).days
        head = _ru_days_left(days)
        msg = (
            f"📅 {head}\n"
            f"Эфир: {event_msk.strftime('%d.%m.%Y в %H:%M')} (МСК)."
        )
    elif d_now == d_ev:
        delta = event_msk - now_msk
        if delta.total_seconds() <= 0:
            return
        h = int(delta.total_seconds() // 3600)
        m = int((delta.total_seconds() % 3600) // 60)
        msg = (
            f"📅 Сегодня эфир в {event_msk.strftime('%H:%M')} (МСК).\n"
            f"До начала примерно {h} ч. {m} мин."
        )
    else:
        return

    text = _with_optional_link(msg, link)
    ok, total = await _send_to_all(bot, text)
    if total == 0 or ok > 0:
        await mark_last_daily_msk_sent(today_s)
    else:
        logger.warning(
            "Ежедневное 12:00 МСК: ни одному из %s получателей не ушло — last_daily не ставим, повторим тиком.",
            total,
        )
    logger.info("Ежедневное 12:00 МСК: отправлено %s из %s получателям", ok, total)


async def process_reminder_tick(bot: Bot) -> None:
    bot_settings = await get_bot_settings()
    if not bot_settings.get("broadcasts_enabled", True):
        _log_throttled(
            "broadcasts_disabled",
            logging.INFO,
            "Рассылки участникам отключены в настройках — автонапоминания не отправляются.",
        )
        return

    settings = await get_contest_settings()
    event_iso = settings.get("event_at")
    if not event_iso:
        _log_throttled(
            "no_event_at",
            logging.WARNING,
            "Напоминания: дата эфира не задана (contest_settings.event_at пусто) — "
            "автосообщения не работают. Укажите дату в «Данные розыгрыша».",
        )
        return

    await sync_reminder_snapshot_if_needed(event_iso)
    snap = canonical_event_iso((await get_contest_reminder_flags()).get("event_at_snapshot"))
    want = canonical_event_iso(event_iso)
    if snap != want:
        logger.warning(
            "Напоминания: после sync снимок всё ещё отличается от даты эфира (snapshot=%s, need=%s). "
            "Проверьте таблицу contest_reminders id=1.",
            snap,
            want,
        )

    try:
        dt = parse_event_at_utc(event_iso)
    except Exception as e:
        logger.warning("Некорректная дата эфира в настройках: %s", e)
        return

    now_utc = datetime.now(timezone.utc)
    if now_utc >= dt:
        msk = ZoneInfo("Europe/Moscow")
        _log_throttled(
            "event_in_past",
            logging.WARNING,
            "Напоминания: время эфира уже наступило или в прошлом "
            "(эфир %s МСК, сейчас %s МСК). "
            "Автонапоминания 2ч/15м/5м и 12:00 не отправляются. "
            "Проверьте год и дату в «Данные розыгрыша» (частая ошибка — прошлый год).",
            dt.astimezone(msk).strftime("%d.%m.%Y %H:%M"),
            now_utc.astimezone(msk).strftime("%d.%m.%Y %H:%M"),
        )
        return

    link = (settings.get("contest_link") or "").strip()

    # Сначала 2ч / 15м / 5м — чтобы не задерживать из‑за долгой рассылки «12:00 МСК»
    async def try_send(kind: str, delta: timedelta, body: str) -> None:
        now_t = datetime.now(timezone.utc)
        if now_t >= dt:
            return
        flags = await get_contest_reminder_flags()
        col = {"2h": "sent_2h", "15m": "sent_15m", "5m": "sent_5m"}[kind]
        if _reminder_already_sent(flags, col):
            return
        if not _threshold_reached(now_t, dt, delta):
            return
        text = _with_optional_link(body, link)
        ok, total = await _send_to_all(bot, text)
        # Раньше mark ставился всегда → при 0 доставок (ошибки API, сеть) флаг «уже слали»
        # блокировал повтор; ручная рассылка флаги не трогает — поэтому «вручную работает».
        if total == 0:
            await mark_contest_reminder_sent(kind)
        elif ok > 0:
            await mark_contest_reminder_sent(kind)
            logger.info("Напоминание %s: отправлено %s из %s получателям", kind, ok, total)
        else:
            logger.warning(
                "Напоминание %s: ни одному из %s получателей не ушло — флаг не ставим, повторим на следующем тике.",
                kind,
                total,
            )

    await try_send("2h", timedelta(hours=2), REMINDER_2H)
    await try_send("15m", timedelta(minutes=15), REMINDER_15M)
    await try_send("5m", timedelta(minutes=5), REMINDER_5M)

    await process_daily_noon_msk(bot, dt, link)


async def reminder_loop(bot: Bot) -> None:
    logger.info(
        "Цикл напоминаний запущен (тик каждые %s с): 2ч / 15м / 5м до эфира, ежедневно ~12:00 МСК.",
        TICK_SEC,
    )
    while True:
        try:
            await process_reminder_tick(bot)
        except asyncio.CancelledError:
            logger.info("Цикл напоминаний остановлен.")
            break
        except Exception:
            logger.exception("Ошибка в цикле напоминаний")
        await asyncio.sleep(TICK_SEC)
