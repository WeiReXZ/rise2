"""
Обработчики бота: приветствие, анкета (имя, возраст, деятельность, цель), запрос телефона, завершение.
"""
from aiogram import types
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters import BoundFilter
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove

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
    wipe_database,
    get_all_admin_ids,
    get_admin_usernames,
    add_admin_by_id,
    add_admin_by_username,
)
from config import ADMIN_IDS, CHECKLIST_PATH, BOT_USERNAME


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
    )


# --- Проверка админа (config + добавленные через бота) ---
class IsAdmin(BoundFilter):
    key = "is_admin"

    def __init__(self, is_admin: bool):
        self.is_admin = is_admin

    async def check(self, message: types.Message):
        if not message.from_user:
            return False
        admin_ids = await get_all_admin_ids()
        admin_usernames = await get_admin_usernames()
        user = message.from_user
        user_is_admin = (
            user.id in admin_ids
            or (user.username and user.username.lower() in admin_usernames)
        )
        return user_is_admin is self.is_admin


# --- Обработчики ---
async def cmd_start(message: types.Message, state: FSMContext):
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
    await add_visit(user.id, start_payload)
    await state.update_data(source=start_payload)
    # Админ видит кнопки сразу, даже если ещё не регистрировался
    admin_ids = await get_all_admin_ids()
    admin_usernames = await get_admin_usernames()
    if user.id in admin_ids or (user.username and user.username.lower() in admin_usernames):
        await message.answer("Вы админ. Используйте кнопки ниже.", reply_markup=kb_admin_menu())
    await message.answer(WELCOME)
    await message.answer(ASK_NAME)
    await state.set_state(Survey.name)


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
    admin_ids = await get_all_admin_ids()
    admin_usernames = await get_admin_usernames()
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


# --- Админ: выгрузка в Excel (по команде /export или кнопке) ---
async def cmd_export(message: types.Message):
    from export_excel import build_excel_bytes

    participants = await get_all_participants()
    visitors = await get_visitors_not_registered()
    if not participants and not visitors:
        await message.answer("Нет данных для выгрузки.", reply_markup=kb_admin_menu())
        return
    try:
        buf = build_excel_bytes(participants, visitors)
        cap = f"Участники: {len(participants)}. Визиты без регистрации: {len(visitors)}."
        await message.answer_document(
            types.InputFile(buf, filename="participants.xlsx"),
            caption=cap,
        )
        await message.answer("Готово. Что дальше?", reply_markup=kb_admin_menu())
    except Exception as e:
        await message.answer(f"Ошибка выгрузки: {e}", reply_markup=kb_admin_menu())


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
    for p in participants:
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
