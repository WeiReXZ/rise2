"""
Обработчики бота: приветствие, анкета (имя, возраст, деятельность, цель), запрос телефона, завершение.
"""
import asyncio
import io
from aiogram import types
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters import BoundFilter
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.types import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

from database import (
    save_participant,
    get_participant_by_telegram_id,
    get_stats,
    get_visitor_stats,
    leave_contest,
    get_participants_by_numbers,
    get_all_participants,
    add_visit,
    get_visitors_not_registered,
    get_participants_by_source,
    get_visitors_by_source_not_registered,
    wipe_database,
    get_all_admin_ids,
    get_admin_usernames,
    add_admin_by_id,
    add_admin_by_username,
    get_contest_settings,
    save_contest_settings,
    get_active_raffle_recipient_telegram_ids,
    get_bot_settings,
    set_maintenance_enabled,
    set_maintenance_message,
    set_broadcasts_enabled,
    get_maintenance_allowlist_ids,
    add_maintenance_allowlist,
    remove_maintenance_allowlist,
    admin_upsert_participant,
    replace_participants_and_visits_from_import,
)
from config import ADMIN_IDS, CHECKLIST_PATH, BOT_USERNAME
from reminders import (
    format_event_utc_for_admin_msk,
    parse_msk_datetime_to_utc_iso,
)
from export_excel import build_excel_bytes
from export_by_source import build_docx_bytes, build_txt_bytes, build_message_text_chunks
from import_excel import parse_excel_bytes


class Survey(StatesGroup):
    name = State()
    age = State()
    occupation = State()
    goal = State()
    phone = State()


class Admin(StatesGroup):
    search_by_number = State()
    delete_db_confirm = State()
    add_admin = State()
    contest_hub = State()
    contest_input = State()
    maintenance_hub = State()
    maintenance_message_input = State()
    maintenance_allow = State()
    add_participant_tg = State()
    add_participant_username = State()
    add_participant_name = State()
    add_participant_age = State()
    add_participant_occupation = State()
    add_participant_goal = State()
    add_participant_phone = State()
    excel_import = State()


# --- Тексты по ТЗ ---
WELCOME = """Hello! Рады, что вы здесь. Ваше намерение учиться — это уже большой вклад в ваш будущий успех. Чтобы мы могли выдать вам номер участника и подготовить для вас лучший образовательный путь, ответьте на пару вопросов."""

ASK_NAME = "Как вас зовут? (Введите имя)"

AGE_OPTIONS = ["до 18", "18-25", "26-35", "36+"]
OCCUPATION_OPTIONS = [
    "Студент",
    "Работаю в найме",
    "Предприниматель",
    "Фрилансер",
    "Домохозяйка / в поиске",
]
GOAL_OPTIONS = [
    "Для путешествий",
    "Для карьеры и работы",
    "Для переезда",
    "Для саморазвития",
    "Для сдачи экзаменов",
]

ASK_PHONE = """Почти готово! Оставьте ваш номер телефона, чтобы мы могли мгновенно связаться с вами в случае выигрыша гранта на обучение."""

PHONE_BUTTON_TEXT = "Отправить номер телефона и получить номер участника"

FINISH_TEMPLATE = """Поздравляем! Ваш контакт подтвержден. Ваш номер участника: №{participant_number}.
Вы в деле! В качестве бонуса за вашу решительность держите наш чек-лист. Изучайте с пользой, мы верим в ваш прогресс!"""

ALREADY_REGISTERED = """Вы уже зарегистрированы в розыгрыше.
Ваш номер участника: №{participant_number}. Ждём результатов!"""

MY_NUMBER_NO = "Сначала пройдите регистрацию: нажмите «Начать» в меню."
MY_NUMBER_YES = "Ваш номер участника: №{participant_number}."

LEFT_CONTEST = "Вы вышли из конкурса. Спасибо за участие. Повторная регистрация невозможна."
ALREADY_LEFT = "Вы уже вышли из конкурса. Повторная регистрация невозможна."

FALLBACK_MSG = "Не понял. Нажмите /start для начала или выберите пункт в меню."


def kb_remove():
    return ReplyKeyboardRemove()


def kb_ages():
    keyboard = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    for opt in AGE_OPTIONS:
        keyboard.add(KeyboardButton(opt))
    return keyboard


def kb_occupations():
    keyboard = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    for opt in OCCUPATION_OPTIONS:
        keyboard.add(KeyboardButton(opt))
    return keyboard


def kb_goals():
    keyboard = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    for opt in GOAL_OPTIONS:
        keyboard.add(KeyboardButton(opt))
    return keyboard


def kb_phone():
    return ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True).add(
        KeyboardButton(PHONE_BUTTON_TEXT, request_contact=True)
    )


# Кнопки для пользователя (после регистрации)
def kb_user_menu():
    return ReplyKeyboardMarkup(resize_keyboard=True).add(
        KeyboardButton("Мой номер участника"),
        KeyboardButton("Выйти из конкурса"),
    )


# Кнопки для админа
def kb_admin_menu():
    return ReplyKeyboardMarkup(resize_keyboard=True).add(
        KeyboardButton("Выгрузить Excel"),
        KeyboardButton("Статистика"),
        KeyboardButton("Поиск по номеру"),
        KeyboardButton("Реферал.ссылки"),
        KeyboardButton("Добавить админа"),
        KeyboardButton("Удалить базу"),
        KeyboardButton("Вывод данных"),
        KeyboardButton("Данные розыгрыша"),
    ).add(
        KeyboardButton("Техработы и доступ"),
        KeyboardButton("Добавить участника"),
    ).add(
        KeyboardButton("Импорт из Excel"),
    )


def kb_admin_back_only():
    return ReplyKeyboardMarkup(resize_keyboard=True).add(KeyboardButton("« Админ-меню"))


def kb_maintenance_menu(maintenance_on: bool, broadcasts_on: bool):
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(
        KeyboardButton("Выключить техработы" if maintenance_on else "Включить техработы"),
    )
    kb.add(
        KeyboardButton("Отключить рассылки" if broadcasts_on else "Включить рассылки"),
    )
    kb.add(
        KeyboardButton("Текст сообщения"),
        KeyboardButton("Белый список: показать"),
    )
    kb.add(
        KeyboardButton("Белый список: добавить"),
        KeyboardButton("Белый список: удалить"),
    )
    kb.add(KeyboardButton("« Админ-меню"))
    return kb


def _format_maintenance_panel(settings: dict, allow_ids: list) -> str:
    en = settings.get("maintenance_enabled")
    br = settings.get("broadcasts_enabled", True)
    msg = (settings.get("maintenance_message") or "").strip()
    msg_s = msg if msg else "(по умолчанию: текст о техработах)"
    allow_s = ", ".join(str(x) for x in allow_ids) if allow_ids else "пусто"
    return (
        "Техработы и доступ\n\n"
        f"Режим техработ: {'включён' if en else 'выключен'}\n"
        f"Рассылки участникам: {'включены' if br else 'отключены'} "
        "(автонапоминания 2ч/15м/5м, 12:00 МСК и кнопки «Рассылка: …» в данных розыгрыша)\n"
        f"Сообщение для остальных пользователей:\n{msg_s}\n\n"
        f"Белый список (Telegram ID, кроме админов): {allow_s}\n\n"
        "Админы из настроек и таблицы «админы» всегда могут пользоваться ботом."
    )


def _kb_maintenance(settings: dict):
    return kb_maintenance_menu(
        bool(settings.get("maintenance_enabled")),
        bool(settings.get("broadcasts_enabled", True)),
    )


def kb_ages_admin():
    k = kb_ages()
    k.add(KeyboardButton("« Админ-меню"))
    return k


def kb_occupations_admin():
    k = kb_occupations()
    k.add(KeyboardButton("« Админ-меню"))
    return k


def kb_goals_admin():
    k = kb_goals()
    k.add(KeyboardButton("« Админ-меню"))
    return k


DEFAULT_FIRST_TOUCH = "Первое касание — это текст с ссылкой.\n\n{link}"
DEFAULT_POSTPONEMENT = (
    "Напоминаем вам, что розыгрыш мы перенесли на дату, на такое время.\n\n{link}"
)


def kb_contest_menu():
    return ReplyKeyboardMarkup(resize_keyboard=True).add(
        KeyboardButton("Дата и время эфира"),
        KeyboardButton("Ссылка на канал"),
    ).add(
        KeyboardButton("Текст первого касания"),
        KeyboardButton("Текст о переносе"),
    ).add(
        KeyboardButton("Рассылка: первое касание"),
        KeyboardButton("Рассылка: о переносе"),
    ).add(
        KeyboardButton("« Админ-меню"),
    )


def _format_contest_panel(settings: dict) -> str:
    ev = settings.get("event_at")
    ev_s = format_event_utc_for_admin_msk(ev) if ev else "не задано"
    link = (settings.get("contest_link") or "").strip() or "не задана"
    ft = (settings.get("first_touch_text") or "").strip() or "(по умолчанию)"
    po = (settings.get("postponement_notice") or "").strip() or "(по умолчанию)"
    return (
        "Данные розыгрыша\n\n"
        f"Дата и время эфира (МСК): {ev_s}\n"
        f"Ссылка на канал: {link}\n\n"
        "Текст первого касания (в рассылке подставится {link}):\n"
        f"{ft[:500]}{'…' if len(ft) > 500 else ''}\n\n"
        "Текст о переносе:\n"
        f"{po[:500]}{'…' if len(po) > 500 else ''}\n\n"
        "Каждый день в 12:00 (МСК) — напоминание: сколько дней осталось и дата/время эфира; в день эфира — сколько часов до начала.\n"
        "За 2 ч, 15 мин и 5 мин до эфира — отдельные автосообщения. Ручная рассылка не отменяет их."
    )


def kb_export_sources() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Аккаунты по ссылке ac1",
                    callback_data="de_src_ac1",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Аккаунты по ссылке ac2",
                    callback_data="de_src_ac2",
                ),
            ],
        ]
    )


def kb_export_format(source: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton("Word", callback_data=f"de_fmt_{source}_docx"),
                InlineKeyboardButton("Excel", callback_data=f"de_fmt_{source}_xlsx"),
            ],
            [
                InlineKeyboardButton("Txt", callback_data=f"de_fmt_{source}_txt"),
                InlineKeyboardButton("В сообщении", callback_data=f"de_fmt_{source}_msg"),
            ],
            [
                InlineKeyboardButton("« Назад", callback_data="de_back"),
            ],
        ]
    )


# --- Проверка админа (config + добавленные через бота) ---
class IsAdmin(BoundFilter):
    key = "is_admin"

    def __init__(self, is_admin: bool):
        self.is_admin = is_admin

    async def check(self, message: types.Message):
        if not message.from_user:
            return False
        admin_ids, admin_usernames = await asyncio.gather(get_all_admin_ids(), get_admin_usernames())
        user = message.from_user
        user_is_admin = (
            user.id in admin_ids
            or (user.username and user.username.lower() in admin_usernames)
        )
        return user_is_admin is self.is_admin


async def user_is_admin(user: types.User) -> bool:
    """Проверка админа (config + таблица admins)."""
    if not user:
        return False
    admin_ids, admin_usernames = await asyncio.gather(get_all_admin_ids(), get_admin_usernames())
    return user.id in admin_ids or (user.username and user.username.lower() in admin_usernames)


# --- Обработчики ---
async def cmd_start(message: types.Message, state: FSMContext):
    try:
        await state.finish()
        user = message.from_user
        if not user:
            return
        # С какой ссылки зашёл: t.me/bot?start=ac1 → сохраняем ac1
        start_payload = (message.get_args() or "").strip()[:100] or None
        existing = await get_participant_by_telegram_id(user.id)
        if existing:
            if existing.get("left_at"):
                await message.answer(
                    ALREADY_LEFT,
                    reply_markup=kb_remove(),
                )
                return
            admin_ids = await get_all_admin_ids()
            admin_usernames = await get_admin_usernames()
            is_admin = user.id in admin_ids or (user.username and user.username.lower() in admin_usernames)
            markup = kb_admin_menu() if is_admin else kb_user_menu()
            await message.answer(
                ALREADY_REGISTERED.format(participant_number=existing["participant_number"]),
                reply_markup=markup,
            )
            return
        # Сначала отвечаем — пользователь сразу видит приветствие, без зависания на БД
        await message.answer(WELCOME)
        await message.answer(ASK_NAME)
        await state.set_state(Survey.name)
        await state.update_data(source=start_payload)
        # Учёт визита и источник (ac1/ac2) — после ответа; при блокировке БД юзер уже получил текст
        await add_visit(user.id, start_payload)
        admin_ids, admin_usernames = await asyncio.gather(get_all_admin_ids(), get_admin_usernames())
        if user.id in admin_ids or (user.username and user.username.lower() in admin_usernames):
            await message.answer("Вы админ. Используйте кнопки ниже.", reply_markup=kb_admin_menu())
    except Exception:
        # На всякий случай: при любой ошибке всё равно пытаемся ответить
        try:
            await message.answer(FALLBACK_MSG)
        except Exception:
            pass
        raise


async def cmd_mynumber(message: types.Message, state: FSMContext):
    """Мой номер участника — по команде или по кнопке."""
    await state.finish()
    user = message.from_user
    if not user:
        return
    existing = await get_participant_by_telegram_id(user.id)
    if existing:
        await message.answer(
            MY_NUMBER_YES.format(participant_number=existing["participant_number"]),
            reply_markup=kb_user_menu(),
        )
    else:
        await message.answer(MY_NUMBER_NO, reply_markup=kb_remove())


async def survey_name(message: types.Message, state: FSMContext):
    name = (message.text or "").strip()
    if not name or len(name) > 200:
        await message.answer("Введите, пожалуйста, имя (до 200 символов).")
        return
    await state.update_data(name=name)
    await state.set_state(Survey.age)
    await message.answer("Укажите ваш возраст:", reply_markup=kb_ages())


async def survey_age(message: types.Message, state: FSMContext):
    if message.text not in AGE_OPTIONS:
        await message.answer("Выберите вариант возраста кнопкой ниже.", reply_markup=kb_ages())
        return
    await state.update_data(age=message.text)
    await state.set_state(Survey.occupation)
    await message.answer("Ваш род деятельности?", reply_markup=kb_occupations())


async def survey_occupation(message: types.Message, state: FSMContext):
    if message.text not in OCCUPATION_OPTIONS:
        await message.answer("Выберите вариант кнопкой.", reply_markup=kb_occupations())
        return
    await state.update_data(occupation=message.text)
    await state.set_state(Survey.goal)
    await message.answer("Цель изучения английского?", reply_markup=kb_goals())


async def survey_goal(message: types.Message, state: FSMContext):
    if message.text not in GOAL_OPTIONS:
        await message.answer("Выберите вариант кнопкой.", reply_markup=kb_goals())
        return
    await state.update_data(goal=message.text)
    await state.set_state(Survey.phone)
    await message.answer(ASK_PHONE, reply_markup=kb_phone())


async def survey_phone_contact(message: types.Message, state: FSMContext):
    if not message.contact or not message.contact.phone_number:
        await message.answer(ASK_PHONE, reply_markup=kb_phone())
        return
    phone = message.contact.phone_number
    if message.contact.user_id and message.contact.user_id != message.from_user.id:
        await message.answer("Пожалуйста, отправьте именно свой номер телефона.", reply_markup=kb_phone())
        return

    data = await state.get_data()
    name = data.get("name") or ""
    age = data.get("age") or ""
    occupation = data.get("occupation") or ""
    goal = data.get("goal") or ""
    source = data.get("source")

    if not all([name, age, occupation, goal]):
        await state.finish()
        await message.answer("Что-то пошло не так. Начните заново: /start", reply_markup=kb_remove())
        return

    user = message.from_user
    participant_number = await save_participant(
        telegram_id=user.id,
        username=user.username,
        name=name,
        age=age,
        occupation=occupation,
        goal=goal,
        phone=phone,
        source=source,
    )
    await state.finish()
    admin_ids, admin_usernames = await asyncio.gather(get_all_admin_ids(), get_admin_usernames())
    is_admin = user.id in admin_ids or (user.username and user.username.lower() in admin_usernames)
    markup = kb_admin_menu() if is_admin else kb_user_menu()
    await message.answer(
        FINISH_TEMPLATE.format(participant_number=participant_number),
        reply_markup=markup,
    )
    # Бонус: чек-лист файлом
    if CHECKLIST_PATH.exists():
        try:
            await message.answer_document(
                types.InputFile(str(CHECKLIST_PATH), filename="чек-лист.txt"),
                caption="Ваш бонусный чек-лист. Удачи!",
            )
        except Exception:
            pass


async def survey_phone_text(message: types.Message, state: FSMContext):
    """Если пользователь написал текст вместо нажатия кнопки с контактом."""
    await message.answer("Пожалуйста, нажмите кнопку ниже и отправьте номер телефона.", reply_markup=kb_phone())


# --- Админ: выгрузка в Excel (в фоне, чтобы не блокировать ответы другим) ---
async def cmd_export(message: types.Message):
    participants = await get_all_participants()
    visitors = await get_visitors_not_registered()
    if not participants and not visitors:
        await message.answer("Нет данных для выгрузки.", reply_markup=kb_admin_menu())
        return
    await message.answer("Формирую файл…", reply_markup=kb_admin_menu())
    loop = asyncio.get_event_loop()
    try:
        buf = await loop.run_in_executor(
            None,
            lambda: build_excel_bytes(participants, visitors),
        )
        cap = f"Участники: {len(participants)}. Визиты без регистрации: {len(visitors)}."
        await message.answer_document(
            types.InputFile(buf, filename="participants.xlsx"),
            caption=cap,
        )
        await message.answer("Готово.", reply_markup=kb_admin_menu())
    except Exception as e:
        await message.answer(f"Ошибка выгрузки: {e}", reply_markup=kb_admin_menu())


async def cmd_excel_import_start(message: types.Message, state: FSMContext):
    await state.finish()
    await state.set_state(Admin.excel_import)
    await message.answer(
        "Импорт из Excel (.xlsx)\n\n"
        "Пришлите файл в том же формате, что даёт «Выгрузить Excel»: "
        "листы «Участники» и «Визиты без регистрации», первая строка — заголовки без изменений.\n\n"
        "Текущие участники и визиты будут полностью удалены и заменены данными из файла. "
        "Настройки розыгрыша, админы и техработы не затрагиваются.\n\n"
        "Отмена — « Админ-меню».",
        reply_markup=kb_admin_back_only(),
    )


async def excel_import_message(message: types.Message, state: FSMContext):
    text = (message.text or "").strip()
    if text == "« Админ-меню":
        await state.finish()
        await message.answer("Меню админа.", reply_markup=kb_admin_menu())
        return
    if message.document:
        fn = (message.document.file_name or "").lower()
        if not fn.endswith(".xlsx"):
            await message.answer("Нужен файл .xlsx (как при выгрузке).", reply_markup=kb_admin_back_only())
            return
        size = message.document.file_size or 0
        if size > 20 * 1024 * 1024:
            await message.answer("Файл больше 20 МБ.", reply_markup=kb_admin_back_only())
            return
        await message.answer("Читаю файл…", reply_markup=kb_admin_back_only())
        bot = message.bot
        buf = io.BytesIO()
        f = await bot.get_file(message.document.file_id)
        await bot.download_file(f.file_path, destination=buf)
        raw = buf.getvalue()
        loop = asyncio.get_event_loop()
        try:
            participants, visitors = await loop.run_in_executor(
                None,
                lambda: parse_excel_bytes(raw),
            )
        except ValueError as e:
            await message.answer(f"Ошибка разбора: {e}", reply_markup=kb_admin_back_only())
            return
        try:
            await replace_participants_and_visits_from_import(participants, visitors)
        except Exception as e:
            await message.answer(f"Ошибка записи в базу: {e}", reply_markup=kb_admin_back_only())
            return
        await state.finish()
        await message.answer(
            f"Готово. Участников: {len(participants)}, визитов без регистрации: {len(visitors)}.",
            reply_markup=kb_admin_menu(),
        )
        return
    await message.answer(
        "Пришлите файл .xlsx одним сообщением (как документ), либо « Админ-меню» для отмены.",
        reply_markup=kb_admin_back_only(),
    )


# --- Выйти из конкурса (повторная регистрация невозможна) ---
async def cmd_leave_contest(message: types.Message, state: FSMContext):
    await state.finish()
    user = message.from_user
    if not user:
        return
    ok = await leave_contest(user.id)
    if ok:
        await message.answer(LEFT_CONTEST, reply_markup=kb_remove())
    else:
        await message.answer(ALREADY_LEFT, reply_markup=kb_remove())


# --- Админ: поиск по номеру участника ---
SEARCH_PROMPT = "Введите номера участников через запятую (например: 1, 3, 5):"
SEARCH_NOT_FOUND = "Участники с номерами {numbers} не найдены."
SEARCH_ONE = """№{participant_number}
Имя: {name}
Возраст: {age}
Род деятельности: {occupation}
Цель: {goal}
Телефон: {phone}
Telegram ID: {telegram_id}
Username: {username}
Источник: {source}
Регистрация: {created_at}
Выход: {left_at}"""


async def cmd_search_start(message: types.Message, state: FSMContext):
    await state.finish()
    await state.set_state(Admin.search_by_number)
    await message.answer(SEARCH_PROMPT, reply_markup=kb_remove())


async def cmd_search_result(message: types.Message, state: FSMContext):
    text = (message.text or "").strip()
    numbers = []
    for part in text.replace(" ", "").split(","):
        part = part.strip()
        if part.isdigit():
            numbers.append(int(part))
    numbers = sorted(set(numbers))
    await state.finish()
    await message.answer("Ищу...", reply_markup=kb_admin_menu())
    if not numbers:
        await message.answer("Не указаны номера. Нажмите «Поиск по номеру» и введите номера через запятую.", reply_markup=kb_admin_menu())
        return
    participants = await get_participants_by_numbers(numbers)
    if not participants:
        await message.answer(
            SEARCH_NOT_FOUND.format(numbers=", ".join(str(n) for n in numbers)),
            reply_markup=kb_admin_menu(),
        )
        return
    for i, p in enumerate(participants):
        if i > 0 and len(participants) > 5:
            await asyncio.sleep(0.04)
        left = p.get("left_at") or "—"
        username = (p.get("username") or "").strip()
        username = ("@" + username) if username else "—"
        source = p.get("source") or "—"
        created = (p.get("created_at") or "")[:19].replace("T", " ")
        left_str = (left[:19].replace("T", " ")) if left != "—" else "—"
        msg = SEARCH_ONE.format(
            participant_number=p["participant_number"],
            name=p.get("name", ""),
            age=p.get("age", ""),
            occupation=p.get("occupation", ""),
            goal=p.get("goal", ""),
            phone=p.get("phone", ""),
            telegram_id=p.get("telegram_id", ""),
            username=username,
            source=source,
            created_at=created,
            left_at=left_str,
        )
        await message.answer(msg)
    await message.answer(f"Найдено: {len(participants)} из {len(numbers)}.", reply_markup=kb_admin_menu())


# --- Админ: реферальные ссылки (только админ) ---
def _referral_links_text() -> str:
    base = f"https://t.me/{BOT_USERNAME}?start="
    return (
        "Реферал.ссылки\n\n"
        "Аккаунт 1 (ac1):\n"
        f"{base}ac1\n\n"
        "Аккаунт 2 (ac2):\n"
        f"{base}ac2"
    )


async def cmd_referral_links(message: types.Message):
    await message.answer(_referral_links_text(), reply_markup=kb_admin_menu())


# --- Админ: добавить админа (по ID или @username) ---
ADD_ADMIN_PROMPT = "Введите Telegram ID (число) или @username нового админа:"
ADD_ADMIN_DONE = "Админ добавлен."
ADD_ADMIN_EXISTS = "Этот админ уже есть."
ADD_ADMIN_BAD = "Неверный формат. Введите число (ID) или @username. Или /start для отмены."


async def cmd_add_admin_start(message: types.Message, state: FSMContext):
    await state.finish()
    await state.set_state(Admin.add_admin)
    await message.answer(ADD_ADMIN_PROMPT, reply_markup=kb_remove())


async def cmd_add_admin_enter(message: types.Message, state: FSMContext):
    text = (message.text or "").strip()
    if not text:
        await message.answer(ADD_ADMIN_BAD)
        return
    # Отмена
    if text.lower() == "/start":
        await state.finish()
        await message.answer("Отменено.", reply_markup=kb_admin_menu())
        return
    # По ID (только цифры, разумная длина)
    if text.isdigit() and 5 <= len(text) <= 15:
        ok = await add_admin_by_id(int(text))
        await state.finish()
        await message.answer(ADD_ADMIN_DONE if ok else ADD_ADMIN_EXISTS, reply_markup=kb_admin_menu())
        return
    # По username (с @ или без)
    if text.lstrip("@").replace("_", "").isalnum() and len(text.lstrip("@")) >= 4:
        ok = await add_admin_by_username(text)
        await state.finish()
        await message.answer(
            ADD_ADMIN_DONE if ok else ADD_ADMIN_EXISTS,
            reply_markup=kb_admin_menu(),
        )
        return
    await message.answer(ADD_ADMIN_BAD)


# --- Админ: удаление базы (подтверждение словом «Параллелепипед») ---
DELETE_DB_CONFIRM_WORD = "Параллелепипед"
DELETE_DB_PROMPT = (
    "Вы уверены? Вся база будет удалена безвозвратно (участники, визиты, статистика).\n\n"
    f"Для подтверждения введите точно: {DELETE_DB_CONFIRM_WORD}"
)
DELETE_DB_WRONG = f'Неверно. Введите точно: {DELETE_DB_CONFIRM_WORD}\nИли /start для отмены.'
DELETE_DB_DONE = "База полностью очищена. Все данные удалены."


async def cmd_delete_db_start(message: types.Message, state: FSMContext):
    await state.finish()
    await state.set_state(Admin.delete_db_confirm)
    await message.answer(DELETE_DB_PROMPT, reply_markup=kb_remove())


async def cmd_delete_db_confirm(message: types.Message, state: FSMContext):
    text = (message.text or "").strip()
    if text != DELETE_DB_CONFIRM_WORD:
        await message.answer(DELETE_DB_WRONG)
        return
    await wipe_database()
    await state.finish()
    await message.answer(DELETE_DB_DONE, reply_markup=kb_admin_menu())


# --- Админ: статистика (по команде /stats или кнопке) ---
async def cmd_stats(message: types.Message):
    stats = await get_stats()
    visitor_stats = await get_visitor_stats()
    lines = [
        "Участники (с регистрацией):",
        f"Всего: {stats['total']}",
        f"За сегодня: {stats['today']}",
        "",
        "По источникам (зарегистрировались):",
    ]
    for s in stats.get("by_source", []):
        lines.append(f"  • {s['source']}: {s['count']}")
    if not stats.get("by_source"):
        lines.append("  — пока нет данных")
    lines.extend(["", "По целям:"])
    for g in stats["by_goal"]:
        lines.append(f"  • {g['goal']}: {g['count']}")
    if not stats["by_goal"]:
        lines.append("  — пока нет данных")
    lines.extend([
        "",
        "Визиты без регистрации (зашли и вышли):",
        f"Всего: {visitor_stats['total']}",
    ])
    for s in visitor_stats.get("by_source", []):
        lines.append(f"  • {s['source']}: {s['count']}")
    if not visitor_stats.get("by_source") and visitor_stats["total"] == 0:
        lines.append("  — пока нет данных")
    await message.answer("\n".join(lines), reply_markup=kb_admin_menu())


def _apply_contest_link(text: str, link: str) -> str:
    link = (link or "").strip()
    if "{link}" in text:
        return text.replace("{link}", link or "(ссылка не задана)")
    if link:
        return text.rstrip() + "\n\n" + link
    return text


async def cmd_contest_open(message: types.Message, state: FSMContext):
    await state.finish()
    await state.set_state(Admin.contest_hub)
    settings = await get_contest_settings()
    await message.answer(_format_contest_panel(settings), reply_markup=kb_contest_menu())


async def contest_hub_handler(message: types.Message, state: FSMContext):
    text = (message.text or "").strip()
    if text == "« Админ-меню":
        await state.finish()
        await message.answer("Меню админа.", reply_markup=kb_admin_menu())
        return
    if text == "Дата и время эфира":
        await state.update_data(contest_field="event")
        await state.set_state(Admin.contest_input)
        await message.answer(
            "Введите дату и время эфира: ДД.ММ.ГГГГ ЧЧ:ММ (время по Москве).\n"
            "Пример: 25.03.2025 19:00\n\n"
            "Чтобы убрать дату (и автонапоминания), отправьте: сброс",
        )
        return
    if text == "Ссылка на канал":
        await state.update_data(contest_field="link")
        await state.set_state(Admin.contest_input)
        await message.answer(
            "Отправьте ссылку на канал (https://…).\n"
            "Она подставится в напоминания и в {link} в текстах.\n"
            "Чтобы очистить: сброс",
        )
        return
    if text == "Текст первого касания":
        await state.update_data(contest_field="first_touch")
        await state.set_state(Admin.contest_input)
        await message.answer(
            "Отправьте текст первого касания. Вставьте {link} там, где нужна ссылка.\n"
            "По умолчанию используется шаблон, если оставить пустым — нажмите «Отмена» и снова «Рассылка».\n\n"
            "Отправьте один символ «-» чтобы записать текст по умолчанию.",
        )
        return
    if text == "Текст о переносе":
        await state.update_data(contest_field="postponement")
        await state.set_state(Admin.contest_input)
        await message.answer(
            "Текст о переносе розыгрыша. Подставьте {link} при необходимости.\n"
            "Один символ «-» — шаблон по умолчанию.",
        )
        return
    if text == "Рассылка: первое касание":
        await _broadcast_contest_template(message, "first")
        return
    if text == "Рассылка: о переносе":
        await _broadcast_contest_template(message, "post")
        return
    settings = await get_contest_settings()
    await message.answer(_format_contest_panel(settings), reply_markup=kb_contest_menu())


async def contest_input_handler(message: types.Message, state: FSMContext):
    data = await state.get_data()
    field = data.get("contest_field")
    text = message.text
    if text is None:
        await message.answer("Нужен текст. Повторите или нажмите « Админ-меню».")
        return
    text = text.strip()
    if field == "event":
        if text.lower() == "сброс":
            await save_contest_settings(event_at=None)
        else:
            try:
                iso = parse_msk_datetime_to_utc_iso(text)
                await save_contest_settings(event_at=iso)
            except ValueError as e:
                await message.answer(f"Не разобрали дату: {e}\nПопробуйте снова.")
                return
    elif field == "link":
        if text.lower() == "сброс":
            await save_contest_settings(contest_link="")
        else:
            await save_contest_settings(contest_link=text[:2000])
    elif field == "first_touch":
        if text == "-":
            await save_contest_settings(first_touch_text=DEFAULT_FIRST_TOUCH)
        else:
            await save_contest_settings(first_touch_text=text[:8000])
    elif field == "postponement":
        if text == "-":
            await save_contest_settings(postponement_notice=DEFAULT_POSTPONEMENT)
        else:
            await save_contest_settings(postponement_notice=text[:8000])
    else:
        await state.set_state(Admin.contest_hub)
        await message.answer("Неизвестное поле.", reply_markup=kb_contest_menu())
        return

    await state.set_state(Admin.contest_hub)
    settings = await get_contest_settings()
    await message.answer("Сохранено.\n\n" + _format_contest_panel(settings), reply_markup=kb_contest_menu())


async def _broadcast_contest_template(message: types.Message, kind: str):
    """Ручная рассылка: не меняет флаги автонапоминаний (2 ч / 15 мин / 5 мин и 12:00 МСК)."""
    bot_settings = await get_bot_settings()
    if not bot_settings.get("broadcasts_enabled", True):
        await message.answer(
            "Рассылки участникам отключены в «Техработы и доступ». "
            "Включите «Включить рассылки», чтобы снова слать сообщения.",
            reply_markup=kb_contest_menu(),
        )
        return
    settings = await get_contest_settings()
    link = (settings.get("contest_link") or "").strip()
    if kind == "first":
        raw = (settings.get("first_touch_text") or "").strip()
        body = raw if raw else DEFAULT_FIRST_TOUCH
    else:
        raw = (settings.get("postponement_notice") or "").strip()
        body = raw if raw else DEFAULT_POSTPONEMENT
    out = _apply_contest_link(body, link)
    if not out.strip():
        await message.answer("Текст пустой.", reply_markup=kb_contest_menu())
        return
    ids = await get_active_raffle_recipient_telegram_ids()
    if not ids:
        await message.answer("Нет активных участников для рассылки.", reply_markup=kb_contest_menu())
        return
    await message.answer(f"Рассылка {len(ids)} получателям…", reply_markup=kb_contest_menu())
    bot = message.bot
    ok = 0
    for i, uid in enumerate(ids):
        if i > 0:
            await asyncio.sleep(0.04)
        try:
            await bot.send_message(uid, out)
            ok += 1
        except Exception:
            pass
    await message.answer(f"Отправлено: {ok} из {len(ids)}.", reply_markup=kb_contest_menu())


async def cmd_maintenance_open(message: types.Message, state: FSMContext):
    await state.finish()
    await state.set_state(Admin.maintenance_hub)
    settings = await get_bot_settings()
    allow = await get_maintenance_allowlist_ids()
    await message.answer(
        _format_maintenance_panel(settings, allow),
        reply_markup=_kb_maintenance(settings),
    )


async def maintenance_hub_handler(message: types.Message, state: FSMContext):
    text = (message.text or "").strip()
    if text == "« Админ-меню":
        await state.finish()
        await message.answer("Меню админа.", reply_markup=kb_admin_menu())
        return
    settings = await get_bot_settings()
    if text == "Включить техработы":
        await set_maintenance_enabled(True)
        settings = await get_bot_settings()
        allow = await get_maintenance_allowlist_ids()
        await message.answer(
            _format_maintenance_panel(settings, allow),
            reply_markup=_kb_maintenance(settings),
        )
        return
    if text == "Выключить техработы":
        await set_maintenance_enabled(False)
        settings = await get_bot_settings()
        allow = await get_maintenance_allowlist_ids()
        await message.answer(
            _format_maintenance_panel(settings, allow),
            reply_markup=_kb_maintenance(settings),
        )
        return
    if text == "Отключить рассылки":
        await set_broadcasts_enabled(False)
        settings = await get_bot_settings()
        allow = await get_maintenance_allowlist_ids()
        await message.answer(
            _format_maintenance_panel(settings, allow),
            reply_markup=_kb_maintenance(settings),
        )
        return
    if text == "Включить рассылки":
        await set_broadcasts_enabled(True)
        settings = await get_bot_settings()
        allow = await get_maintenance_allowlist_ids()
        await message.answer(
            _format_maintenance_panel(settings, allow),
            reply_markup=_kb_maintenance(settings),
        )
        return
    if text == "Текст сообщения":
        await state.set_state(Admin.maintenance_message_input)
        await message.answer(
            "Отправьте текст, который увидят пользователи при включённых техработах "
            "(до 2000 символов). Пустой текст — будет стандартное сообщение о техработах.\n\n"
            "« Админ-меню» — отмена.",
            reply_markup=kb_admin_back_only(),
        )
        return
    if text == "Белый список: показать":
        allow = await get_maintenance_allowlist_ids()
        await message.answer(
            "ID в белом списке:\n" + ("\n".join(str(x) for x in allow) if allow else "— пусто —"),
            reply_markup=_kb_maintenance(settings),
        )
        return
    if text == "Белый список: добавить":
        await state.set_state(Admin.maintenance_allow)
        await state.update_data(allow_op="add")
        await message.answer(
            "Введите числовой Telegram ID пользователя, которому разрешить доступ к боту при техработах.\n\n"
            "« Админ-меню» — отмена.",
            reply_markup=kb_admin_back_only(),
        )
        return
    if text == "Белый список: удалить":
        await state.set_state(Admin.maintenance_allow)
        await state.update_data(allow_op="remove")
        await message.answer(
            "Введите Telegram ID, который убрать из белого списка.\n\n« Админ-меню» — отмена.",
            reply_markup=kb_admin_back_only(),
        )
        return
    allow = await get_maintenance_allowlist_ids()
    await message.answer(
        _format_maintenance_panel(settings, allow),
        reply_markup=_kb_maintenance(settings),
    )


async def maintenance_message_input_handler(message: types.Message, state: FSMContext):
    text = (message.text or "").strip()
    if text == "« Админ-меню":
        await state.set_state(Admin.maintenance_hub)
        settings = await get_bot_settings()
        allow = await get_maintenance_allowlist_ids()
        await message.answer(
            _format_maintenance_panel(settings, allow),
            reply_markup=_kb_maintenance(settings),
        )
        return
    await set_maintenance_message(text)
    await state.set_state(Admin.maintenance_hub)
    settings = await get_bot_settings()
    allow = await get_maintenance_allowlist_ids()
    await message.answer(
        "Текст сохранён.\n\n" + _format_maintenance_panel(settings, allow),
        reply_markup=_kb_maintenance(settings),
    )


async def maintenance_allow_handler(message: types.Message, state: FSMContext):
    text = (message.text or "").strip()
    if text == "« Админ-меню":
        await state.set_state(Admin.maintenance_hub)
        settings = await get_bot_settings()
        allow = await get_maintenance_allowlist_ids()
        await message.answer(
            _format_maintenance_panel(settings, allow),
            reply_markup=_kb_maintenance(settings),
        )
        return
    data = await state.get_data()
    op = data.get("allow_op") or "add"
    try:
        tid = int(text)
    except ValueError:
        await message.answer("Нужно целое число (Telegram ID). Попробуйте ещё раз.", reply_markup=kb_admin_back_only())
        return
    if op == "remove":
        ok = await remove_maintenance_allowlist(tid)
        note = "Удалён из белого списка." if ok else "Такого ID в списке не было."
    else:
        ok = await add_maintenance_allowlist(tid)
        note = "Добавлен в белый список." if ok else "Этот ID уже был в списке."
    await state.set_state(Admin.maintenance_hub)
    settings = await get_bot_settings()
    allow = await get_maintenance_allowlist_ids()
    await message.answer(
        note + "\n\n" + _format_maintenance_panel(settings, allow),
        reply_markup=_kb_maintenance(settings),
    )


async def cmd_add_participant_start(message: types.Message, state: FSMContext):
    await state.finish()
    await state.set_state(Admin.add_participant_tg)
    await message.answer(
        "Ручное добавление участника в базу (как при регистрации через бота). "
        "Если пользователь уже есть — данные обновятся, выход из конкурса снимется.\n\n"
        "Шаг 1/7. Введите числовой Telegram ID пользователя.\n"
        "Отмена — « Админ-меню».",
        reply_markup=kb_admin_back_only(),
    )


async def add_participant_tg_handler(message: types.Message, state: FSMContext):
    text = (message.text or "").strip()
    if text == "« Админ-меню":
        await state.finish()
        await message.answer("Меню админа.", reply_markup=kb_admin_menu())
        return
    try:
        tid = int(text)
    except ValueError:
        await message.answer("Нужно целое число (Telegram ID).", reply_markup=kb_admin_back_only())
        return
    if tid <= 0:
        await message.answer("ID должен быть положительным.", reply_markup=kb_admin_back_only())
        return
    await state.update_data(manual_tg_id=tid)
    await state.set_state(Admin.add_participant_username)
    await message.answer(
        "Шаг 2/7. Username в Telegram (без @) или «-» если нет.",
        reply_markup=kb_admin_back_only(),
    )


async def add_participant_username_handler(message: types.Message, state: FSMContext):
    text = (message.text or "").strip()
    if text == "« Админ-меню":
        await state.finish()
        await message.answer("Меню админа.", reply_markup=kb_admin_menu())
        return
    un = None if text in ("-", "—", "") else text.lstrip("@").strip()[:64]
    await state.update_data(manual_username=un)
    await state.set_state(Admin.add_participant_name)
    await message.answer("Шаг 3/7. Имя (как в анкете):", reply_markup=kb_admin_back_only())


async def add_participant_name_handler(message: types.Message, state: FSMContext):
    text = (message.text or "").strip()
    if text == "« Админ-меню":
        await state.finish()
        await message.answer("Меню админа.", reply_markup=kb_admin_menu())
        return
    if not text or len(text) > 200:
        await message.answer("Имя — до 200 символов.", reply_markup=kb_admin_back_only())
        return
    await state.update_data(manual_name=text)
    await state.set_state(Admin.add_participant_age)
    await message.answer("Шаг 4/7. Возраст — выберите кнопкой:", reply_markup=kb_ages_admin())


async def add_participant_age_handler(message: types.Message, state: FSMContext):
    text = (message.text or "").strip()
    if text == "« Админ-меню":
        await state.finish()
        await message.answer("Меню админа.", reply_markup=kb_admin_menu())
        return
    if text not in AGE_OPTIONS:
        await message.answer("Выберите возраст кнопкой ниже.", reply_markup=kb_ages_admin())
        return
    await state.update_data(manual_age=text)
    await state.set_state(Admin.add_participant_occupation)
    await message.answer("Шаг 5/7. Род деятельности:", reply_markup=kb_occupations_admin())


async def add_participant_occupation_handler(message: types.Message, state: FSMContext):
    text = (message.text or "").strip()
    if text == "« Админ-меню":
        await state.finish()
        await message.answer("Меню админа.", reply_markup=kb_admin_menu())
        return
    if text not in OCCUPATION_OPTIONS:
        await message.answer("Выберите вариант кнопкой.", reply_markup=kb_occupations_admin())
        return
    await state.update_data(manual_occupation=text)
    await state.set_state(Admin.add_participant_goal)
    await message.answer("Шаг 6/7. Цель изучения английского:", reply_markup=kb_goals_admin())


async def add_participant_goal_handler(message: types.Message, state: FSMContext):
    text = (message.text or "").strip()
    if text == "« Админ-меню":
        await state.finish()
        await message.answer("Меню админа.", reply_markup=kb_admin_menu())
        return
    if text not in GOAL_OPTIONS:
        await message.answer("Выберите вариант кнопкой.", reply_markup=kb_goals_admin())
        return
    await state.update_data(manual_goal=text)
    await state.set_state(Admin.add_participant_phone)
    await message.answer(
        "Шаг 7/7. Телефон (в любом формате, как в выгрузке):",
        reply_markup=kb_admin_back_only(),
    )


async def add_participant_phone_handler(message: types.Message, state: FSMContext):
    text = (message.text or "").strip()
    if text == "« Админ-меню":
        await state.finish()
        await message.answer("Меню админа.", reply_markup=kb_admin_menu())
        return
    if len(text) < 5 or len(text) > 40:
        await message.answer("Телефон — от 5 до 40 символов.", reply_markup=kb_admin_back_only())
        return
    data = await state.get_data()
    tid = data.get("manual_tg_id")
    if not tid:
        await state.finish()
        await message.answer("Сессия сброшена. Начните снова: «Добавить участника».", reply_markup=kb_admin_menu())
        return
    num, kind = await admin_upsert_participant(
        telegram_id=int(tid),
        username=data.get("manual_username"),
        name=data.get("manual_name") or "",
        age=data.get("manual_age") or "",
        occupation=data.get("manual_occupation") or "",
        goal=data.get("manual_goal") or "",
        phone=text,
    )
    await state.finish()
    verb = "Создана запись" if kind == "created" else "Обновлена запись"
    await message.answer(
        f"{verb}. Номер участника: №{num}.",
        reply_markup=kb_admin_menu(),
    )


# --- Админ: вывод данных по ac1/ac2 (инлайн в чате: Word / Excel / Txt / сообщение) ---
DATA_EXPORT_INTRO = (
    "Вывод данных по ссылкам — только чтение из базы, записи не удаляются и не меняются.\n\n"
    "Выберите источник:"
)


async def cmd_data_export_menu(message: types.Message):
    await message.answer(DATA_EXPORT_INTRO, reply_markup=kb_export_sources())


async def callback_data_export(callback_query: types.CallbackQuery):
    if not await user_is_admin(callback_query.from_user):
        await callback_query.answer("Нет доступа", show_alert=True)
        return
    data = callback_query.data or ""
    msg = callback_query.message
    if not msg:
        await callback_query.answer()
        return

    if data == "de_back":
        await callback_query.answer()
        try:
            await msg.edit_text(DATA_EXPORT_INTRO, reply_markup=kb_export_sources())
        except Exception:
            await msg.answer(DATA_EXPORT_INTRO, reply_markup=kb_export_sources())
        return

    if data in ("de_src_ac1", "de_src_ac2"):
        await callback_query.answer()
        src = "ac1" if data.endswith("ac1") else "ac2"
        text = f"Источник: {src}. Выберите формат выгрузки:"
        try:
            await msg.edit_text(text, reply_markup=kb_export_format(src))
        except Exception:
            await msg.answer(text, reply_markup=kb_export_format(src))
        return

    if not data.startswith("de_fmt_"):
        await callback_query.answer()
        return

    rest = data[7:]
    if "_" not in rest:
        await callback_query.answer()
        return
    source, kind = rest.rsplit("_", 1)
    if kind not in ("docx", "xlsx", "txt", "msg"):
        await callback_query.answer()
        return

    await callback_query.answer()
    participants = await get_participants_by_source(source)
    visitors = await get_visitors_by_source_not_registered(source)
    if not participants and not visitors:
        try:
            await msg.edit_text(
                f"По ссылке {source} нет записей (ни участников, ни визитов без регистрации).",
                reply_markup=kb_export_format(source),
            )
        except Exception:
            await msg.answer(
                f"По ссылке {source} нет записей.",
                reply_markup=kb_export_format(source),
            )
        return

    loop = asyncio.get_event_loop()
    try:
        if kind == "docx":
            buf = await loop.run_in_executor(
                None,
                lambda: build_docx_bytes(participants, visitors, source),
            )
            await msg.answer_document(
                types.InputFile(buf, filename=f"{source}_export.docx"),
                caption=f"{source}: участников {len(participants)}, визитов без регистрации {len(visitors)}.",
            )
        elif kind == "xlsx":
            buf = await loop.run_in_executor(
                None,
                lambda: build_excel_bytes(participants, visitors),
            )
            await msg.answer_document(
                types.InputFile(buf, filename=f"{source}_export.xlsx"),
                caption=f"{source}: участников {len(participants)}, визитов без регистрации {len(visitors)}.",
            )
        elif kind == "txt":
            buf = await loop.run_in_executor(
                None,
                lambda: build_txt_bytes(participants, visitors, source),
            )
            await msg.answer_document(
                types.InputFile(buf, filename=f"{source}_export.txt"),
                caption=f"{source}: участников {len(participants)}, визитов без регистрации {len(visitors)}.",
            )
        else:
            chunks = build_message_text_chunks(participants, visitors, source)
            for i, chunk in enumerate(chunks):
                if i > 0:
                    await asyncio.sleep(0.05)
                await msg.answer(chunk)
        try:
            await msg.edit_reply_markup(reply_markup=kb_export_format(source))
        except Exception:
            pass
        await msg.answer("Готово.", reply_markup=kb_admin_menu())
    except Exception as e:
        try:
            await msg.answer(f"Ошибка выгрузки: {e}", reply_markup=kb_admin_menu())
        except Exception:
            pass


# --- Ответ на любое необработанное сообщение (чтобы никто не оставался без ответа) ---
async def fallback_handler(message: types.Message, state: FSMContext):
    await state.finish()
    await message.answer(FALLBACK_MSG)
