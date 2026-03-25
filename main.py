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
from maintenance_middleware import MaintenanceMiddleware
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
    cmd_excel_import_start,
    excel_import_message,
    cmd_stats,
    cmd_search_start,
    cmd_search_result,
    cmd_referral_links,
    cmd_add_admin_start,
    cmd_add_admin_enter,
    cmd_delete_db_start,
    cmd_delete_db_confirm,
    cmd_data_export_menu,
    callback_data_export,
    cmd_contest_open,
    contest_hub_handler,
    contest_input_handler,
    cmd_maintenance_open,
    maintenance_hub_handler,
    maintenance_message_input_handler,
    maintenance_allow_handler,
    cmd_add_participant_start,
    add_participant_tg_handler,
    add_participant_username_handler,
    add_participant_name_handler,
    add_participant_age_handler,
    add_participant_occupation_handler,
    add_participant_goal_handler,
    add_participant_phone_handler,
    fallback_handler,
)
from reminders import reminder_loop

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

    # Сначала состояния админки розыгрыша (чтобы не перехватывались state="*")
    dp.register_message_handler(contest_hub_handler, state=Admin.contest_hub, is_admin=True)
    dp.register_message_handler(contest_input_handler, state=Admin.contest_input, is_admin=True)

    # Админ-кнопки обрабатываем в любом состоянии (state="*"), чтобы срабатывали до анкеты
    dp.register_message_handler(cmd_export, Command("export"), is_admin=True, state="*")
    dp.register_message_handler(cmd_export, Text(equals="Выгрузить Excel"), is_admin=True, state="*")
    dp.register_message_handler(cmd_excel_import_start, Text(equals="Импорт из Excel"), is_admin=True, state="*")
    dp.register_message_handler(
        excel_import_message,
        state=Admin.excel_import,
        is_admin=True,
        content_types=[types.ContentType.ANY],
    )
    dp.register_message_handler(cmd_stats, Command("stats"), is_admin=True, state="*")
    dp.register_message_handler(cmd_stats, Text(equals="Статистика"), is_admin=True, state="*")
    dp.register_message_handler(cmd_search_start, Text(equals="Поиск по номеру"), is_admin=True, state="*")
    dp.register_message_handler(cmd_search_result, state=Admin.search_by_number, is_admin=True)
    dp.register_message_handler(cmd_referral_links, Text(equals="Реферал.ссылки"), is_admin=True, state="*")
    dp.register_message_handler(cmd_add_admin_start, Text(equals="Добавить админа"), is_admin=True, state="*")
    dp.register_message_handler(cmd_add_admin_enter, state=Admin.add_admin, is_admin=True)
    dp.register_message_handler(cmd_delete_db_start, Text(equals="Удалить базу"), is_admin=True, state="*")
    dp.register_message_handler(cmd_delete_db_confirm, state=Admin.delete_db_confirm, is_admin=True)
    dp.register_message_handler(cmd_data_export_menu, Text(equals="Вывод данных"), is_admin=True, state="*")
    dp.register_message_handler(cmd_contest_open, Text(equals="Данные розыгрыша"), is_admin=True, state="*")
    dp.register_message_handler(cmd_maintenance_open, Text(equals="Техработы и доступ"), is_admin=True, state="*")
    dp.register_message_handler(maintenance_hub_handler, state=Admin.maintenance_hub, is_admin=True)
    dp.register_message_handler(
        maintenance_message_input_handler, state=Admin.maintenance_message_input, is_admin=True
    )
    dp.register_message_handler(maintenance_allow_handler, state=Admin.maintenance_allow, is_admin=True)
    dp.register_message_handler(cmd_add_participant_start, Text(equals="Добавить участника"), is_admin=True, state="*")
    dp.register_message_handler(add_participant_tg_handler, state=Admin.add_participant_tg, is_admin=True)
    dp.register_message_handler(add_participant_username_handler, state=Admin.add_participant_username, is_admin=True)
    dp.register_message_handler(add_participant_name_handler, state=Admin.add_participant_name, is_admin=True)
    dp.register_message_handler(add_participant_age_handler, state=Admin.add_participant_age, is_admin=True)
    dp.register_message_handler(add_participant_occupation_handler, state=Admin.add_participant_occupation, is_admin=True)
    dp.register_message_handler(add_participant_goal_handler, state=Admin.add_participant_goal, is_admin=True)
    dp.register_message_handler(add_participant_phone_handler, state=Admin.add_participant_phone, is_admin=True)
    dp.register_callback_query_handler(
        callback_data_export,
        lambda c: c.data and str(c.data).startswith("de_"),
        state="*",
    )

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

    # В конце: ответ на любое сообщение, которое не обработал ни один из предыдущих обработчиков
    dp.register_message_handler(fallback_handler, state="*", content_types=[types.ContentType.ANY])


async def main() -> None:
    logger.info("Запуск бота...")
    await init_db()
    bot = Bot(token=BOT_TOKEN)
    storage = MemoryStorage()
    dp = Dispatcher(
        bot,
        storage=storage,
        run_tasks_by_default=True,
        throttling_rate_limit=0.05,
    )
    dp.filters_factory.bind(IsAdmin)
    dp.setup_middleware(MaintenanceMiddleware())
    setup_dispatcher(dp)

    async def errors_handler(update, exception):
        logger.exception("Ошибка при обработке: %s", exception)
        try:
            if update and getattr(update, "message", None) and update.message:
                await update.message.answer("Произошла ошибка. Попробуйте ещё раз или /start")
        except Exception:
            pass
        return True
    dp.register_errors_handler(errors_handler)

    reminder_task = None
    try:
        logger.info("Подключение к Telegram API...")
        await bot.set_my_commands([
            types.BotCommand("start", "Начать"),
            types.BotCommand("mynumber", "Мой номер участника"),
        ])
        logger.info("Бот запущен. ADMIN_IDS: %s", ADMIN_IDS)
        reminder_task = asyncio.create_task(reminder_loop(bot))
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
    finally:
        if reminder_task is not None:
            reminder_task.cancel()
            try:
                await reminder_task
            except asyncio.CancelledError:
                pass
        try:
            session = await bot.get_session()
            await session.close()
        except Exception as e:
            logger.debug("Закрытие сессии: %s", e)


if __name__ == "__main__":
    asyncio.run(main())
