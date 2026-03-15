"""
Telegram-бот для сбора лидов (розыгрыш года обучения английскому).
Запуск: python main.py (нужен .env с BOT_TOKEN и опционально ADMIN_IDS).
"""
import asyncio
import logging

import aiohttp
from aiogram import Bot, Dispatcher, types
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher.filters import CommandStart, Command, Text
from aiogram.utils import exceptions as aiogram_exceptions

from config import BOT_TOKEN, ADMIN_IDS
from database import init_db
from handlers import (
    Survey,
    Admin,
    IsAdmin,
    cmd_start,
    survey_name,
    survey_age,
    survey_occupation,
    survey_goal,
    survey_phone_contact,
    survey_phone_text,
    cmd_mynumber,
    cmd_leave_contest,
    cmd_export,
    cmd_stats,
    cmd_search_start,
    cmd_search_result,
    cmd_referral_links,
    cmd_add_admin_start,
    cmd_add_admin_enter,
    cmd_delete_db_start,
    cmd_delete_db_confirm,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def setup_dispatcher(dp: Dispatcher) -> None:
    # /start — без состояния
    dp.register_message_handler(cmd_start, CommandStart(), state="*")

    # Мой номер участника — по команде или по кнопке
    dp.register_message_handler(cmd_mynumber, Command("mynumber"), state="*")
    dp.register_message_handler(cmd_mynumber, Text(equals="Мой номер участника"), state="*")

    # Выйти из конкурса (повторная регистрация невозможна)
    dp.register_message_handler(cmd_leave_contest, Text(equals="Выйти из конкурса"), state="*")

    # Админ-кнопки обрабатываем в любом состоянии (state="*"), чтобы срабатывали до анкеты
    dp.register_message_handler(cmd_export, Command("export"), is_admin=True, state="*")
    dp.register_message_handler(cmd_export, Text(equals="Выгрузить Excel"), is_admin=True, state="*")
    dp.register_message_handler(cmd_stats, Command("stats"), is_admin=True, state="*")
    dp.register_message_handler(cmd_stats, Text(equals="Статистика"), is_admin=True, state="*")
    dp.register_message_handler(cmd_search_start, Text(equals="Поиск по номеру"), is_admin=True, state="*")
    dp.register_message_handler(cmd_search_result, state=Admin.search_by_number, is_admin=True)
    dp.register_message_handler(cmd_referral_links, Text(equals="Реферал.ссылки"), is_admin=True, state="*")
    dp.register_message_handler(cmd_add_admin_start, Text(equals="Добавить админа"), is_admin=True, state="*")
    dp.register_message_handler(cmd_add_admin_enter, state=Admin.add_admin, is_admin=True)
    dp.register_message_handler(cmd_delete_db_start, Text(equals="Удалить базу"), is_admin=True, state="*")
    dp.register_message_handler(cmd_delete_db_confirm, state=Admin.delete_db_confirm, is_admin=True)

    # Анкета по шагам
    dp.register_message_handler(survey_name, state=Survey.name)
    dp.register_message_handler(survey_age, state=Survey.age)
    dp.register_message_handler(survey_occupation, state=Survey.occupation)
    dp.register_message_handler(survey_goal, state=Survey.goal)
    dp.register_message_handler(
        survey_phone_contact,
        content_types=["contact"],
        state=Survey.phone,
    )
    dp.register_message_handler(survey_phone_text, state=Survey.phone)


async def main() -> None:
    await init_db()
    bot = Bot(token=BOT_TOKEN)
    storage = MemoryStorage()
    dp = Dispatcher(bot, storage=storage)
    dp.filters_factory.bind(IsAdmin)
    setup_dispatcher(dp)

    # Меню под полем ввода — не нужно вводить команды вручную
    await bot.set_my_commands([
        types.BotCommand("start", "Начать"),
        types.BotCommand("mynumber", "Мой номер участника"),
    ])
    logger.info("Бот запущен. ADMIN_IDS: %s", ADMIN_IDS)
    retry_delay = 10
    while True:
        try:
            await dp.start_polling()
            break
        except (
            aiogram_exceptions.NetworkError,
            ConnectionError,
            asyncio.TimeoutError,
            OSError,
            aiohttp.ClientError,
        ) as e:
            logger.warning("Сетевая ошибка (бот перезапустит через %s сек): %s", retry_delay, e)
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay + 5, 60)
        except Exception as e:
            logger.exception("Ошибка polling: %s", e)
            raise
    await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
