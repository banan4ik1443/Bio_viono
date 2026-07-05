"""
Iris: Био-войны — игровой Telegram-бот.

Механика оригинальной мини-игры «Био-войны» (лаборатория, навыки,
заражение, пассивный доход с жертв, топы) + собственные команды
"фарм" и "мешок". Корпорации не реализованы.

Все настраиваемые параметры вынесены в отдельный файл config.py.

Установка:
    pip install -r requirements.txt
    (вставь токен бота в config.BOT_TOKEN в config.py)
    python main.py
"""

import asyncio
import logging
import os
import random
import re
import sqlite3
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart
from aiogram.types import Message

import config

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=config.BOT_TOKEN)
dp = Dispatcher()


# ======================================================================
# ===================== БАЗА ДАННЫХ (db) ===============================
# ======================================================================


def db_connect():
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    os.makedirs(config.DATA_DIR, exist_ok=True)
    conn = db_connect()
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS players (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            balance INTEGER NOT NULL DEFAULT 0,
            last_farm TEXT
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS labs (
            user_id INTEGER PRIMARY KEY,
            lab_name TEXT,
            pathogen_name TEXT,

            bio_exp INTEGER NOT NULL DEFAULT 0,

            lvl_pathogen INTEGER NOT NULL DEFAULT 1,
            lvl_development INTEGER NOT NULL DEFAULT 1,
            lvl_infectivity INTEGER NOT NULL DEFAULT 1,
            lvl_immunity INTEGER NOT NULL DEFAULT 1,
            lvl_lethality INTEGER NOT NULL DEFAULT 1,
            lvl_security INTEGER NOT NULL DEFAULT 1,

            visible INTEGER NOT NULL DEFAULT 1,

            fever_until TEXT,
            infected_by INTEGER,

            victims_count INTEGER NOT NULL DEFAULT 0,
            deleted INTEGER NOT NULL DEFAULT 0,

            created_at TEXT,

            FOREIGN KEY (user_id) REFERENCES players(user_id)
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS infections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            attacker_id INTEGER NOT NULL,
            victim_id INTEGER NOT NULL,
            infected_at TEXT NOT NULL,
            last_income_at TEXT,
            active INTEGER NOT NULL DEFAULT 1
        )
        """
    )

    conn.commit()
    conn.close()


def get_or_create_player(user_id: int, username: str, first_name: str):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT * FROM players WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    if row is None:
        cur.execute(
            "INSERT INTO players (user_id, username, first_name, balance, last_farm) VALUES (?, ?, ?, 0, NULL)",
            (user_id, username, first_name),
        )
        conn.commit()
        cur.execute("SELECT * FROM players WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
    else:
        cur.execute(
            "UPDATE players SET username = ?, first_name = ? WHERE user_id = ?",
            (username, first_name, user_id),
        )
        conn.commit()
    conn.close()
    return row


def update_balance_and_farm_time(user_id: int, amount: int, farm_time: str):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        "UPDATE players SET balance = balance + ?, last_farm = ? WHERE user_id = ?",
        (amount, farm_time, user_id),
    )
    conn.commit()
    conn.close()


def get_balance(user_id: int) -> int:
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT balance FROM players WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row["balance"] if row else 0


def add_balance(user_id: int, amount: int):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("UPDATE players SET balance = balance + ? WHERE user_id = ?", (amount, user_id))
    conn.commit()
    conn.close()


def spend_balance(user_id: int, amount: int) -> bool:
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT balance FROM players WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    if row is None or row["balance"] < amount:
        conn.close()
        return False
    cur.execute("UPDATE players SET balance = balance - ? WHERE user_id = ?", (amount, user_id))
    conn.commit()
    conn.close()
    return True


def get_lab(user_id: int):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT * FROM labs WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row


def has_active_lab(user_id: int) -> bool:
    lab = get_lab(user_id)
    return lab is not None and lab["deleted"] == 0


def create_lab(user_id: int, created_at: str):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT * FROM labs WHERE user_id = ?", (user_id,))
    existing = cur.fetchone()
    if existing is None:
        cur.execute(
            "INSERT INTO labs (user_id, created_at) VALUES (?, ?)",
            (user_id, created_at),
        )
    else:
        cur.execute("UPDATE labs SET deleted = 0 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


def update_lab_field(user_id: int, field: str, value):
    allowed_fields = {
        "lab_name", "pathogen_name", "bio_exp",
        "lvl_pathogen", "lvl_development", "lvl_infectivity",
        "lvl_immunity", "lvl_lethality", "lvl_security",
        "visible", "fever_until", "infected_by",
        "victims_count", "deleted",
    }
    if field not in allowed_fields:
        raise ValueError(f"Недопустимое поле лабы: {field}")
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(f"UPDATE labs SET {field} = ? WHERE user_id = ?", (value, user_id))
    conn.commit()
    conn.close()


def increment_lab_level(user_id: int, skill_field: str, levels: int, exp_gain: int):
    allowed_skills = {
        "lvl_pathogen", "lvl_development", "lvl_infectivity",
        "lvl_immunity", "lvl_lethality", "lvl_security",
    }
    if skill_field not in allowed_skills:
        raise ValueError(f"Недопустимый навык: {skill_field}")
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        f"UPDATE labs SET {skill_field} = {skill_field} + ?, bio_exp = bio_exp + ? WHERE user_id = ?",
        (levels, exp_gain, user_id),
    )
    conn.commit()
    conn.close()


def soft_delete_lab(user_id: int):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("UPDATE labs SET deleted = 1 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


def restore_lab(user_id: int, penalty_multiplier: float):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE labs SET
            deleted = 0,
            victims_count = 0,
            lvl_pathogen = MAX(1, CAST(lvl_pathogen * ? AS INTEGER)),
            lvl_development = MAX(1, CAST(lvl_development * ? AS INTEGER)),
            lvl_infectivity = MAX(1, CAST(lvl_infectivity * ? AS INTEGER)),
            lvl_immunity = MAX(1, CAST(lvl_immunity * ? AS INTEGER)),
            lvl_lethality = MAX(1, CAST(lvl_lethality * ? AS INTEGER)),
            lvl_security = MAX(1, CAST(lvl_security * ? AS INTEGER))
        WHERE user_id = ?
        """,
        (
            penalty_multiplier, penalty_multiplier, penalty_multiplier,
            penalty_multiplier, penalty_multiplier, penalty_multiplier,
            user_id,
        ),
    )
    conn.commit()
    conn.close()


def get_all_active_labs():
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT * FROM labs WHERE deleted = 0")
    rows = cur.fetchall()
    conn.close()
    return rows


def get_random_active_lab_user_id(exclude_user_id: int):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        "SELECT user_id FROM labs WHERE deleted = 0 AND user_id != ? ORDER BY RANDOM() LIMIT 1",
        (exclude_user_id,),
    )
    row = cur.fetchone()
    conn.close()
    return row["user_id"] if row else None


def create_infection(attacker_id: int, victim_id: int, infected_at: str):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        "UPDATE infections SET active = 0 WHERE victim_id = ? AND active = 1",
        (victim_id,),
    )
    cur.execute(
        "INSERT INTO infections (attacker_id, victim_id, infected_at, last_income_at, active) VALUES (?, ?, ?, ?, 1)",
        (attacker_id, victim_id, infected_at, infected_at),
    )
    conn.commit()
    conn.close()


def get_active_infections():
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT * FROM infections WHERE active = 1")
    rows = cur.fetchall()
    conn.close()
    return rows


def get_active_infections_by_attacker(attacker_id: int):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM infections WHERE active = 1 AND attacker_id = ?",
        (attacker_id,),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def set_infection_last_income(infection_id: int, when_iso: str):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        "UPDATE infections SET last_income_at = ? WHERE id = ?",
        (when_iso, infection_id),
    )
    conn.commit()
    conn.close()


def deactivate_infection(infection_id: int):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("UPDATE infections SET active = 0 WHERE id = ?", (infection_id,))
    conn.commit()
    conn.close()


# ======================================================================
# ===================== ВСПОМОГАТЕЛЬНОЕ (utils) ========================
# ======================================================================


def normalize_text(text: str) -> str:
    text = text.strip().lower()
    text = re.sub(r"^!\s*", "", text)
    return text


def matches(text: str, triggers: set) -> bool:
    return normalize_text(text) in triggers


def format_timedelta(td: timedelta) -> str:
    total_seconds = int(td.total_seconds())
    if total_seconds < 0:
        total_seconds = 0
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    parts = []
    if hours:
        parts.append(f"{hours} ч")
    if minutes:
        parts.append(f"{minutes} мин")
    if seconds or not parts:
        parts.append(f"{seconds} сек")
    return " ".join(parts)


def display_name(user) -> str:
    if user.username:
        return f"@{user.username}"
    return user.first_name or "Игрок"


def get_target_user_id(message: Message):
    """Определяет цель команды через реплай или text_mention."""
    if message.reply_to_message and message.reply_to_message.from_user:
        target = message.reply_to_message.from_user
        return target.id, display_name(target)

    if message.entities:
        for entity in message.entities:
            if entity.type == "text_mention" and entity.user:
                return entity.user.id, display_name(entity.user)

    return None, None


def parse_leading_int(text: str, default: int = 1) -> int:
    match = re.search(r"\d+", text)
    if match:
        return int(match.group())
    return default


# ======================================================================
# ===================== ФАРМ / МЕШОК ====================================
# ======================================================================


async def do_farm(message: Message):
    user_id = message.from_user.id
    player = get_or_create_player(
        user_id,
        message.from_user.username or "",
        message.from_user.first_name or "",
    )

    now = datetime.utcnow()

    if player["last_farm"]:
        last_farm_time = datetime.fromisoformat(player["last_farm"])
        elapsed = now - last_farm_time
        cooldown = timedelta(minutes=config.FARM_COOLDOWN_MINUTES)
        if elapsed < cooldown:
            remaining = cooldown - elapsed
            await message.reply(
                f"⏳ Фарм ещё на кулдауне!\n"
                f"Подожди ещё {format_timedelta(remaining)}."
            )
            return

    yield_value = random.randint(config.YIELD_MIN, config.YIELD_MAX)
    bonus_percent = random.randint(config.BONUS_MIN_PERCENT, config.BONUS_MAX_PERCENT)

    base_total = config.FARM_BASE_REWARD * yield_value
    reward = round(base_total * (1 + bonus_percent / 100))

    update_balance_and_farm_time(user_id, reward, now.isoformat())

    await message.reply(
        "✅ ЗАЧЁТ!\n"
        f"☢️ +{reward} {config.CURRENCY_SYMBOL} = {config.FARM_BASE_REWARD}×{yield_value} ({bonus_percent}%)\n\n"
        f"⏳ Урожайность: {yield_value}"
    )


async def do_bag(message: Message):
    user_id = message.from_user.id
    player = get_or_create_player(
        user_id,
        message.from_user.username or "",
        message.from_user.first_name or "",
    )
    balance = player["balance"]
    name = message.from_user.first_name or "Игрок"

    await message.reply(
        f"🎒 Мешок игрока {name}\n"
        f"💰 Баланс: {balance} {config.CURRENCY_SYMBOL}"
    )


# ======================================================================
# ===================== ЛАБОРАТОРИЯ =====================================
# ======================================================================


def ensure_lab(user_id: int):
    # на случай если игрок ещё не существует в players (защита от FOREIGN KEY ошибки)
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM players WHERE user_id = ?", (user_id,))
    if cur.fetchone() is None:
        cur.execute(
            "INSERT INTO players (user_id, username, first_name, balance, last_farm) VALUES (?, '', '', 0, NULL)",
            (user_id,),
        )
        conn.commit()
    conn.close()

    if not has_active_lab(user_id):
        create_lab(user_id, datetime.utcnow().isoformat())


def skill_upgrade_cost(current_level: int, levels: int) -> int:
    total = 0
    for i in range(levels):
        lvl = current_level + i
        total += config.SKILL_BASE_COST + lvl * config.SKILL_COST_PER_LEVEL
    return total


def format_lab_card(lab_row, owner_name: str) -> str:
    lab_name = lab_row["lab_name"] or "(без названия)"
    pathogen_name = lab_row["pathogen_name"] or "(без названия)"

    return (
        f"🧫 Лаборатория игрока {owner_name}\n\n"
        f"🏷️ Название лабы: {lab_name}\n"
        f"🦠 Патоген: {pathogen_name}\n"
        f"☣️ Био-опыт: {lab_row['bio_exp']}\n"
        f"🎯 Жертв заражено: {lab_row['victims_count']}\n\n"
        f"— Навыки —\n"
        f"🧪 Патоген (ячейки): {lab_row['lvl_pathogen']}\n"
        f"⚗️ Разработка: {lab_row['lvl_development']}\n"
        f"🧬 Заразность: {lab_row['lvl_infectivity']}\n"
        f"🛡️ Иммунитет: {lab_row['lvl_immunity']}\n"
        f"☠️ Летальность: {lab_row['lvl_lethality']}\n"
        f"🔒 Безопасность: {lab_row['lvl_security']}"
    )


async def do_lab(message: Message, in_dm: bool = False):
    user = message.from_user
    ensure_lab(user.id)
    lab = get_lab(user.id)

    if lab["visible"] == 0 and message.from_user.id != user.id:
        await message.reply("🔒 Игрок скрыл информацию о своей лаборатории.")
        return

    card = format_lab_card(lab, display_name(user))

    if in_dm:
        try:
            await message.bot.send_message(user.id, card)
            await message.reply("📨 Информация о лаборатории отправлена тебе в личные сообщения.")
        except Exception:
            await message.reply(
                "⚠️ Не получилось отправить сообщение в лс. "
                "Сначала напиши боту в личку /start, потом попробуй снова."
            )
    else:
        await message.reply(card)


async def do_lab_visibility(message: Message, allow: bool):
    user_id = message.from_user.id
    ensure_lab(user_id)
    update_lab_field(user_id, "visible", 1 if allow else 0)
    if allow:
        await message.reply("👁️ Теперь другие игроки могут смотреть твою лабораторию (!лаб).")
    else:
        await message.reply("🙈 Теперь другие игроки не смогут смотреть твою лабораторию.")


async def do_set_pathogen_name(message: Message, name: str):
    user_id = message.from_user.id
    ensure_lab(user_id)
    name = name.strip()
    if not name:
        await message.reply("⚠️ Укажи название патогена: +имя патогена {название}")
        return
    if len(name) > 32:
        await message.reply("⚠️ Название патогена слишком длинное (максимум 32 символа).")
        return
    update_lab_field(user_id, "pathogen_name", name)
    await message.reply(f"🦠 Название патогена установлено: {name}")


async def do_clear_pathogen_name(message: Message):
    user_id = message.from_user.id
    ensure_lab(user_id)
    update_lab_field(user_id, "pathogen_name", None)
    await message.reply("🦠 Название патогена удалено.")


async def do_set_lab_name(message: Message, name: str):
    user_id = message.from_user.id
    ensure_lab(user_id)
    name = name.strip()
    if not name:
        await message.reply("⚠️ Укажи название лаборатории: +имя лабы {название}")
        return
    if len(name) > 32:
        await message.reply("⚠️ Название лаборатории слишком длинное (максимум 32 символа).")
        return
    update_lab_field(user_id, "lab_name", name)
    await message.reply(f"🏷️ Название лаборатории установлено: {name}")


async def do_clear_lab_name(message: Message):
    user_id = message.from_user.id
    ensure_lab(user_id)
    update_lab_field(user_id, "lab_name", None)
    await message.reply("🏷️ Название лаборатории удалено.")


async def do_upgrade_skill(message: Message, keyword: str, levels: int):
    user_id = message.from_user.id
    ensure_lab(user_id)

    field = config.SKILL_FIELD_BY_KEYWORD.get(keyword)
    if field is None:
        await message.reply("⚠️ Неизвестный навык.")
        return

    levels = max(1, min(levels, config.MAX_LEVELS_PER_UPGRADE))

    lab = get_lab(user_id)
    current_level = lab[field]

    cost = skill_upgrade_cost(current_level, levels)
    balance = get_balance(user_id)

    if balance < cost:
        await message.reply(
            f"💸 Не хватает {config.CURRENCY_NAME}!\n"
            f"Нужно: {cost} {config.CURRENCY_SYMBOL}, у тебя: {balance} {config.CURRENCY_SYMBOL}."
        )
        return

    ok = spend_balance(user_id, cost)
    if not ok:
        await message.reply("⚠️ Не получилось списать средства, попробуй ещё раз.")
        return

    exp_gain = levels * config.EXP_PER_LEVEL
    increment_lab_level(user_id, field, levels, exp_gain)

    skill_name = config.SKILL_NAMES[field]
    new_level = current_level + levels

    await message.reply(
        f"✅ Навык прокачан!\n"
        f"🔧 {skill_name}: {current_level} → {new_level}\n"
        f"💰 Потрачено: {cost} {config.CURRENCY_SYMBOL}\n"
        f"☣️ Получено био-опыта: +{exp_gain}"
    )


async def do_delete_lab(message: Message):
    user_id = message.from_user.id
    if not has_active_lab(user_id):
        await message.reply("⚠️ У тебя ещё нет лаборатории.")
        return
    soft_delete_lab(user_id)
    await message.reply(
        "🗑️ Твоя лаборатория удалена, участие в игре прекращено.\n"
        "⚠️ Если ты восстановишь лабу командой «!восстановить лабу», "
        "твои навыки будут снижены, а счётчик жертв обнулится."
    )


async def do_restore_lab(message: Message):
    user_id = message.from_user.id
    lab = get_lab(user_id)
    if lab is None or lab["deleted"] == 0:
        await message.reply("⚠️ Восстанавливать нечего — у тебя уже есть активная лаборатория.")
        return
    restore_lab(user_id, config.RESTORE_PENALTY_MULTIPLIER)
    await message.reply(
        "♻️ Лаборатория восстановлена!\n"
        f"⚠️ Характеристики снижены (×{config.RESTORE_PENALTY_MULTIPLIER}), "
        "все прошлые жертвы потеряны."
    )


# ======================================================================
# ===================== ЗАРАЖЕНИЕ ========================================
# ======================================================================


def total_lab_power(lab_row) -> int:
    return (
        lab_row["lvl_pathogen"]
        + lab_row["lvl_development"]
        + lab_row["lvl_infectivity"]
        + lab_row["lvl_immunity"]
        + lab_row["lvl_lethality"]
        + lab_row["lvl_security"]
    )


def is_in_fever(lab_row) -> bool:
    if not lab_row["fever_until"]:
        return False
    return datetime.utcnow() < datetime.fromisoformat(lab_row["fever_until"])


def calc_infection_chance(attacker_lab, victim_lab) -> float:
    infectivity = attacker_lab["lvl_infectivity"]
    immunity = victim_lab["lvl_immunity"]
    diff = infectivity - immunity
    chance = config.INFECTION_BASE_CHANCE + diff * 0.03
    return max(0.05, min(0.95, chance))


def calc_fever_minutes(attacker_lab) -> int:
    power = total_lab_power(attacker_lab)
    return config.FEVER_BASE_MINUTES + power * config.FEVER_MINUTES_PER_ATTACKER_LEVEL


async def attempt_infect(message: Message, attacker_id: int, victim_id: int, victim_name: str):
    if attacker_id == victim_id:
        await message.reply("🚫 Нельзя заразить самого себя.")
        return

    if not has_active_lab(attacker_id):
        await message.reply("⚠️ У тебя ещё нет лаборатории. Введи «!лаб», чтобы создать её.")
        return

    if not has_active_lab(victim_id):
        await message.reply(f"⚠️ У игрока {victim_name} нет лаборатории — заразить нельзя.")
        return

    attacker_lab = get_lab(attacker_id)
    victim_lab = get_lab(victim_id)

    if is_in_fever(attacker_lab):
        await message.reply(
            "🤒 Ты сейчас в горячке и не можешь никого заражать.\n"
            f"Можешь купить вакцину (команда «вакцина») за {config.VACCINE_COST} {config.CURRENCY_SYMBOL}, "
            "либо дождаться, пока горячка спадёт."
        )
        return

    chance = calc_infection_chance(attacker_lab, victim_lab)
    success = random.random() < chance

    if success:
        now = datetime.utcnow()
        create_infection(attacker_id, victim_id, now.isoformat())
        update_lab_field(victim_id, "infected_by", attacker_id)

        new_victims_count = attacker_lab["victims_count"] + 1
        update_lab_field(attacker_id, "victims_count", new_victims_count)

        fever_minutes = calc_fever_minutes(attacker_lab)
        fever_until = now + timedelta(minutes=fever_minutes)
        update_lab_field(victim_id, "fever_until", fever_until.isoformat())

        exp_gain = 5
        increment_lab_level(attacker_id, "lvl_pathogen", 0, exp_gain)

        await message.reply(
            f"☣️ Успех! Ты заразил игрока {victim_name}.\n"
            f"🤒 У жертвы началась горячка на {fever_minutes} мин.\n"
            f"☣️ Получено био-опыта: +{exp_gain}"
        )
    else:
        await message.reply(
            f"🛡️ Не получилось заразить {victim_name} — сработал иммунитет."
        )


async def do_infect_target(message: Message, target_id: int, target_name: str, attempts: int = 1):
    attacker_id = message.from_user.id
    attempts = max(1, min(attempts, 5))
    for _ in range(attempts):
        await attempt_infect(message, attacker_id, target_id, target_name)


async def do_infect_relative(message: Message, mode: str, attempts: int = 1):
    attacker_id = message.from_user.id

    if not has_active_lab(attacker_id):
        await message.reply("⚠️ У тебя ещё нет лаборатории. Введи «!лаб», чтобы создать её.")
        return

    attacker_lab = get_lab(attacker_id)
    attacker_power = total_lab_power(attacker_lab)

    candidates = []
    for lab in get_all_active_labs():
        if lab["user_id"] == attacker_id:
            continue
        power = total_lab_power(lab)
        if mode == "weaker" and power < attacker_power:
            candidates.append(lab)
        elif mode == "stronger" and power > attacker_power:
            candidates.append(lab)
        elif mode == "equal" and power == attacker_power:
            candidates.append(lab)

    if not candidates:
        await message.reply("😕 Не нашлось подходящих целей для заражения.")
        return

    target_lab = random.choice(candidates)
    target_id = target_lab["user_id"]

    try:
        chat_member = await message.bot.get_chat_member(message.chat.id, target_id)
        target_name = display_name(chat_member.user)
    except Exception:
        target_name = f"игрок {target_id}"

    await do_infect_target(message, target_id, target_name, attempts)


async def do_infect_random(message: Message, attempts: int = 1, mode: str = None):
    attacker_id = message.from_user.id

    if not has_active_lab(attacker_id):
        await message.reply("⚠️ У тебя ещё нет лаборатории. Введи «!лаб», чтобы создать её.")
        return

    if mode:
        await do_infect_relative(message, mode, attempts)
        return

    target_id = get_random_active_lab_user_id(attacker_id)
    if target_id is None:
        await message.reply("😕 Не нашлось игроков с лабораторией для заражения.")
        return

    try:
        chat_member = await message.bot.get_chat_member(message.chat.id, target_id)
        target_name = display_name(chat_member.user)
    except Exception:
        target_name = f"игрок {target_id}"

    await do_infect_target(message, target_id, target_name, attempts)


async def do_vaccine(message: Message):
    user_id = message.from_user.id
    lab = get_lab(user_id)
    if lab is None or lab["deleted"] == 1:
        await message.reply("⚠️ У тебя нет лаборатории.")
        return
    if not is_in_fever(lab):
        await message.reply("✅ У тебя сейчас нет горячки.")
        return

    ok = spend_balance(user_id, config.VACCINE_COST)
    if not ok:
        await message.reply(
            f"💸 Не хватает {config.CURRENCY_NAME} на вакцину. "
            f"Нужно: {config.VACCINE_COST} {config.CURRENCY_SYMBOL}."
        )
        return

    update_lab_field(user_id, "fever_until", None)

    # вакцина также снимает активное заражение (жертва выздоравливает)
    for infection in get_active_infections():
        if infection["victim_id"] == user_id:
            deactivate_infection(infection["id"])

    await message.reply(f"💉 Вакцина куплена за {config.VACCINE_COST} {config.CURRENCY_SYMBOL}. Горячка снята!")


# ======================================================================
# ===================== ПАССИВНЫЙ ДОХОД С ЖЕРТВ ==========================
# ======================================================================


def process_passive_income_tick():
    """
    Начисляет доход владельцам активных заражений и снимает заражение,
    если у жертвы истекла горячка (жертва выздоровела).
    Возвращает список (attacker_id, total_income) для тех, кому что-то начислили.
    """
    now = datetime.utcnow()
    incomes = {}  # attacker_id -> сумма начисленного дохода за этот тик

    for infection in get_active_infections():
        victim_lab = get_lab(infection["victim_id"])

        # если лаба жертвы удалена или горячка прошла — заражение снимается
        if victim_lab is None or victim_lab["deleted"] == 1 or not is_in_fever(victim_lab):
            deactivate_infection(infection["id"])
            continue

        attacker_lab = get_lab(infection["attacker_id"])
        if attacker_lab is None or attacker_lab["deleted"] == 1:
            deactivate_infection(infection["id"])
            continue

        income = (
            config.PASSIVE_INCOME_PER_VICTIM_BASE
            + attacker_lab["lvl_development"] * config.PASSIVE_INCOME_PER_DEVELOPMENT_LEVEL
        )

        add_balance(infection["attacker_id"], income)
        set_infection_last_income(infection["id"], now.isoformat())

        incomes[infection["attacker_id"]] = incomes.get(infection["attacker_id"], 0) + income

    return incomes


async def passive_income_loop():
    """Фоновая задача: раз в config.PASSIVE_INCOME_INTERVAL_MINUTES начисляет доход
    всем игрокам с активными заражёнными жертвами и уведомляет их в личку."""
    interval_seconds = config.PASSIVE_INCOME_INTERVAL_MINUTES * 60
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            incomes = process_passive_income_tick()
            for attacker_id, total in incomes.items():
                try:
                    await bot.send_message(
                        attacker_id,
                        f"☣️ Пассивный доход с заражённых жертв: +{total} {config.CURRENCY_SYMBOL}",
                    )
                except Exception:
                    # игрок мог не начать диалог с ботом в лс — просто пропускаем уведомление
                    pass
        except Exception:
            logger.exception("Ошибка в цикле начисления пассивного дохода")


async def do_income_status(message: Message):
    """Команда 'доход' — показывает сколько игрок получает за тик и активных жертв."""
    user_id = message.from_user.id
    lab = get_lab(user_id)
    if lab is None or lab["deleted"] == 1:
        await message.reply("⚠️ У тебя нет лаборатории.")
        return

    active = get_active_infections_by_attacker(user_id)
    victims_alive = 0
    for infection in active:
        victim_lab = get_lab(infection["victim_id"])
        if victim_lab is not None and victim_lab["deleted"] == 0 and is_in_fever(victim_lab):
            victims_alive += 1

    income_per_victim = config.PASSIVE_INCOME_PER_VICTIM_BASE + lab["lvl_development"] * config.PASSIVE_INCOME_PER_DEVELOPMENT_LEVEL
    total_per_tick = income_per_victim * victims_alive

    await message.reply(
        f"☣️ Активных заражённых жертв: {victims_alive}\n"
        f"💰 Доход за одну жертву: {income_per_victim} {config.CURRENCY_SYMBOL} / тик\n"
        f"📈 Суммарный доход: {total_per_tick} {config.CURRENCY_SYMBOL} каждые {config.PASSIVE_INCOME_INTERVAL_MINUTES} мин."
    )


# ======================================================================
# ===================== ТОПЫ =============================================
# ======================================================================


async def _resolve_names(message: Message, user_ids):
    names = {}
    for uid in user_ids:
        try:
            chat_member = await message.bot.get_chat_member(message.chat.id, uid)
            names[uid] = display_name(chat_member.user)
        except Exception:
            names[uid] = f"игрок {uid}"
    return names


async def do_biotop_global(message: Message):
    labs = get_all_active_labs()
    top = sorted(labs, key=lambda l: l["bio_exp"], reverse=True)[:10]

    if not top:
        await message.reply("😕 Пока нет ни одной лаборатории.")
        return

    names = await _resolve_names(message, [lab["user_id"] for lab in top])

    lines = ["🏆 Глобальный биотоп по опыту:\n"]
    for i, lab in enumerate(top, start=1):
        name = names.get(lab["user_id"], f"игрок {lab['user_id']}")
        lines.append(f"{i}. {name} — {lab['bio_exp']} опыта")

    await message.reply("\n".join(lines))


async def do_biotop_chat(message: Message):
    chat_id = message.chat.id
    labs = get_all_active_labs()

    member_ids = []
    for lab in labs:
        try:
            member = await message.bot.get_chat_member(chat_id, lab["user_id"])
            if member.status not in ("left", "kicked"):
                member_ids.append(lab["user_id"])
        except Exception:
            continue

    chat_labs = [lab for lab in labs if lab["user_id"] in member_ids]
    top = sorted(chat_labs, key=lambda l: l["bio_exp"], reverse=True)[:10]

    if not top:
        await message.reply("😕 В этом чате пока нет игроков с лабораторией.")
        return

    names = await _resolve_names(message, [lab["user_id"] for lab in top])

    lines = ["🏆 Локальный биотоп чата по опыту:\n"]
    for i, lab in enumerate(top, start=1):
        name = names.get(lab["user_id"], f"игрок {lab['user_id']}")
        lines.append(f"{i}. {name} — {lab['bio_exp']} опыта")

    await message.reply("\n".join(lines))


async def do_biotop_infections(message: Message):
    labs = get_all_active_labs()
    top = sorted(labs, key=lambda l: l["victims_count"], reverse=True)[:10]

    top = [lab for lab in top if lab["victims_count"] > 0]
    if not top:
        await message.reply("😕 Пока никто никого не заразил.")
        return

    names = await _resolve_names(message, [lab["user_id"] for lab in top])

    lines = ["☣️ Топ по количеству заражённых жертв:\n"]
    for i, lab in enumerate(top, start=1):
        name = names.get(lab["user_id"], f"игрок {lab['user_id']}")
        lines.append(f"{i}. {name} — {lab['victims_count']} жертв")

    await message.reply("\n".join(lines))


# ======================================================================
# ===================== ХЕНДЛЕРЫ TELEGRAM ================================
# ======================================================================


@dp.message(CommandStart())
async def cmd_start(message: Message):
    get_or_create_player(
        message.from_user.id,
        message.from_user.username or "",
        message.from_user.first_name or "",
    )
    await message.answer(
        "Привет! Это игровой бот Iris: био-войны 🧬\n\n"
        "— Фарм —\n"
        "фарм / фарма — фармить койны (раз в 5 минут)\n"
        "мешок — посмотреть баланс\n\n"
        "— Лаборатория —\n"
        "!лаб — посмотреть свою лабораторию\n"
        "+имя лабы {название} / -имя лабы\n"
        "+имя патогена {название} / -имя патогена\n"
        "+патоген / +разработка / +заразность / +иммунитет / +летальность / +безопасность {N}\n"
        "-биоигра — удалить лабораторию\n"
        "!восстановить лабу — восстановить со штрафом\n\n"
        "— Заражение —\n"
        "заразить (в ответ на сообщение) — заразить игрока\n"
        "заразить слабее/сильнее/равный — заразить по силе\n"
        "заразить чат — заразить случайного из чата\n"
        "заразить рандом — заразить случайного игрока всей игры\n"
        "вакцина — снять горячку за койны\n"
        "доход — посмотреть свой пассивный доход с жертв\n\n"
        "— Топы —\n"
        "биотоп / биотоп чата / биотоп заражений\n\n"
        f"Подробная справка по оригинальной игре: {config.HELP_URL}"
    )


@dp.message()
async def text_router(message: Message):
    if not message.text:
        return

    text = message.text
    norm = normalize_text(text)

    get_or_create_player(
        message.from_user.id,
        message.from_user.username or "",
        message.from_user.first_name or "",
    )

    # ---- фарм / мешок ----
    if matches(text, config.FARM_TRIGGERS):
        await do_farm(message)
        return
    if matches(text, config.BAG_TRIGGERS):
        await do_bag(message)
        return

    # ---- лаборатория: основные ----
    if matches(text, config.LAB_DM_TRIGGERS):
        await do_lab(message, in_dm=True)
        return
    if matches(text, config.LAB_TRIGGERS):
        await do_lab(message, in_dm=False)
        return
    if matches(text, config.LAB_SHOW_TRIGGERS):
        await do_lab_visibility(message, allow=True)
        return
    if matches(text, config.LAB_HIDE_TRIGGERS):
        await do_lab_visibility(message, allow=False)
        return

    # ---- редактирование лабы ----
    if norm.startswith(config.PATHOGEN_NAME_PREFIX):
        raw = re.sub(r"^!\s*", "", text.strip(), flags=re.IGNORECASE)
        name = raw[len("+имя патогена"):].strip()
        await do_set_pathogen_name(message, name)
        return
    if norm == config.PATHOGEN_NAME_CLEAR:
        await do_clear_pathogen_name(message)
        return
    if norm.startswith(config.LAB_NAME_PREFIX):
        raw = re.sub(r"^!\s*", "", text.strip(), flags=re.IGNORECASE)
        name = raw[len("+имя лабы"):].strip()
        await do_set_lab_name(message, name)
        return
    if norm == config.LAB_NAME_CLEAR:
        await do_clear_lab_name(message)
        return

    # ---- прокачка навыков ----
    for keyword, prefixes in config.SKILL_PREFIXES.items():
        for prefix in prefixes:
            if norm == prefix or norm.startswith(prefix + " "):
                levels = parse_leading_int(norm[len(prefix):], default=1)
                await do_upgrade_skill(message, keyword, levels)
                return

    # ---- удаление / восстановление лабы ----
    if matches(text, config.DELETE_LAB_TRIGGERS):
        await do_delete_lab(message)
        return
    if matches(text, config.RESTORE_LAB_TRIGGERS):
        await do_restore_lab(message)
        return

    # ---- вакцина ----
    if matches(text, config.VACCINE_TRIGGERS):
        await do_vaccine(message)
        return

    # ---- доход с жертв ----
    if matches(text, config.INCOME_STATUS_TRIGGERS):
        await do_income_status(message)
        return

    # ---- заражение ----
    if matches(text, config.INFECT_CHAT_TRIGGERS) or norm.startswith("заразить чат "):
        attempts = parse_leading_int(norm[len("заразить чат"):], default=1)
        target_id = get_random_active_lab_user_id(message.from_user.id)
        if target_id is None:
            await message.reply("😕 Не нашлось подходящих целей в чате.")
        else:
            try:
                member = await bot.get_chat_member(message.chat.id, target_id)
                target_name = display_name(member.user)
            except Exception:
                target_name = f"игрок {target_id}"
            await do_infect_target(message, target_id, target_name, attempts)
        return

    if norm.startswith(config.INFECT_RANDOM_PREFIX) or norm.startswith(config.INFECT_RANDOM_SHORT_PREFIX + " ") or norm == config.INFECT_RANDOM_SHORT_PREFIX:
        rest = norm
        for p in (config.INFECT_RANDOM_PREFIX, config.INFECT_RANDOM_SHORT_PREFIX):
            if rest.startswith(p):
                rest = rest[len(p):].strip()
                break
        mode = None
        if rest.startswith("слабее") or rest.startswith("-"):
            mode = "weaker"
        elif rest.startswith("сильнее") or rest.startswith("+"):
            mode = "stronger"
        elif rest.startswith("равный") or rest.startswith("="):
            mode = "equal"
        attempts = parse_leading_int(rest, default=1)
        await do_infect_random(message, attempts, mode)
        return

    if norm.startswith(config.INFECT_PREFIX):
        rest = norm[len(config.INFECT_PREFIX):].strip()

        mode = None
        if rest.startswith("слабее") or rest.startswith("-"):
            mode = "weaker"
            rest = rest[len("слабее"):].strip() if rest.startswith("слабее") else rest[1:].strip()
        elif rest.startswith("сильнее") or rest.startswith("+"):
            mode = "stronger"
            rest = rest[len("сильнее"):].strip() if rest.startswith("сильнее") else rest[1:].strip()
        elif rest.startswith("равный") or rest.startswith("="):
            mode = "equal"
            rest = rest[len("равный"):].strip() if rest.startswith("равный") else rest[1:].strip()

        if mode:
            attempts = parse_leading_int(rest, default=1)
            await do_infect_relative(message, mode, attempts)
            return

        target_id, target_name = get_target_user_id(message)
        if target_id is None:
            await message.reply(
                "⚠️ Укажи цель: ответь командой «заразить» на сообщение нужного игрока, "
                "или используй «заразить слабее/сильнее/равный», «заразить чат», «заразить рандом»."
            )
            return

        attempts = parse_leading_int(rest, default=1)
        await do_infect_target(message, target_id, target_name, attempts)
        return

    # ---- топы ----
    if matches(text, config.BIOTOP_CHAT_INFECTIONS_TRIGGERS):
        await do_biotop_infections(message)
        return
    if matches(text, config.BIOTOP_INFECTIONS_TRIGGERS):
        await do_biotop_infections(message)
        return
    if matches(text, config.BIOTOP_CHAT_TRIGGERS):
        await do_biotop_chat(message)
        return
    if matches(text, config.BIOTOP_TRIGGERS):
        await do_biotop_global(message)
        return

    # ---- помощь ----
    if matches(text, config.HELP_TRIGGERS):
        await message.reply(f"📖 Справка по игре «Био-войны»: {config.HELP_URL}")
        return


# ======================================================================
# ===================== ЗАПУСК ===========================================
# ======================================================================


async def main():
    init_db()
    logger.info("База данных готова, бот запускается...")
    if config.PASSIVE_INCOME_ENABLED:
        asyncio.create_task(passive_income_loop())
        logger.info("Фоновое начисление пассивного дохода запущено (интервал: %s мин.)", config.PASSIVE_INCOME_INTERVAL_MINUTES)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
