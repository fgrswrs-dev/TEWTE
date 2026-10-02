import sqlite3
import time
import socket
import ssl
import urllib.error
import random
import threading
import traceback
from unixgram import Bot

# ================== КОНФИГУРАЦИЯ ==================
TOKEN = "2634307496:TmnDgXxkd3duna5mjN7q27Tounevaiuw"
bot = Bot(token=TOKEN)

BOT_NAME = "RTTL GAME"
ADMIN_USERNAMES = ["tael", "easy"]
HOUSE_FEE = 0.08  # 8% комиссия проекта
EXPIRATION_TIME = 3600  # 1 час в секундах до авто-отмены
# ==================================================

# ----------------- БАЗА ДАННЫХ И МИГРАЦИЯ -----------------
conn = sqlite3.connect("rttl_permanent.db", check_same_thread=False)
cursor = conn.cursor()

# 1. Таблица игроков
cursor.execute("""
CREATE TABLE IF NOT EXISTS players (
    username TEXT PRIMARY KEY,
    last_chat_id INTEGER,
    balance_stars REAL DEFAULT 0.0,
    balance_commission REAL DEFAULT 0.0,
    total_deposited REAL DEFAULT 0.0,
    wins INTEGER DEFAULT 0,
    losses INTEGER DEFAULT 0
)
""")

# 2. Таблица дуэлей
cursor.execute("""
CREATE TABLE IF NOT EXISTS duels (
    duel_id TEXT PRIMARY KEY,
    game_mode TEXT DEFAULT 'карты',
    creator_username TEXT,
    chat_id INTEGER,
    amount REAL,
    status TEXT DEFAULT 'waiting',
    created_at INTEGER DEFAULT 0
)
""")
conn.commit()

# Автоматическая миграция колонок (чтобы база не ломалась)
cursor.execute("PRAGMA table_info(players)")
player_cols = [col[1] for col in cursor.fetchall()]
if "balance_commission" not in player_cols:
    try:
        cursor.execute("ALTER TABLE players ADD COLUMN balance_commission REAL DEFAULT 0.0")
        conn.commit()
    except Exception:
        pass
if "total_deposited" not in player_cols:
    try:
        cursor.execute("ALTER TABLE players ADD COLUMN total_deposited REAL DEFAULT 0.0")
        conn.commit()
    except Exception:
        pass

cursor.execute("PRAGMA table_info(duels)")
duel_cols = [col[1] for col in cursor.fetchall()]
if "game_mode" not in duel_cols:
    try:
        cursor.execute("ALTER TABLE duels ADD COLUMN game_mode TEXT DEFAULT 'карты'")
        conn.commit()
    except Exception:
        pass
if "created_at" not in duel_cols:
    try:
        cursor.execute("ALTER TABLE duels ADD COLUMN created_at INTEGER DEFAULT 0")
        conn.commit()
    except Exception:
        pass


def init_admins_once():
    cursor.execute("SELECT username FROM players WHERE username = 'tael'")
    if not cursor.fetchone():
        cursor.execute(
            "INSERT INTO players (username, balance_stars, balance_commission, total_deposited) VALUES ('tael', 100000.0, 0.0, 100000.0)"
        )
    cursor.execute("SELECT username FROM players WHERE username = 'easy'")
    if not cursor.fetchone():
        cursor.execute(
            "INSERT INTO players (username, balance_stars, balance_commission, total_deposited) VALUES ('easy', 50000.0, 0.0, 50000.0)"
        )
    conn.commit()


init_admins_once()


# ----------------- УТИЛИТЫ ИЗВЛЕЧЕНИЯ ДАННЫХ -----------------
def get_sender_user(message):
    if isinstance(message, dict):
        for k in ["from", "from_user", "sender", "user"]:
            if k in message and message[k]:
                return message[k]
        return message

    for attr in ["from_user", "from", "sender", "author", "user"]:
        u = getattr(message, attr, None)
        if u:
            return u
    if hasattr(message, "json") and isinstance(message.json, dict):
        return message.json.get("from") or message.json.get("sender")
    return None


def get_clean_username(user) -> str:
    if not user:
        return ""
    if isinstance(user, dict):
        for k in ["username", "user_name"]:
            val = user.get(k)
            if val:
                return str(val).lstrip("@").strip().lower()
        for k in ["first_name", "name", "id"]:
            val = user.get(k)
            if val:
                clean = str(val).split("|")[0].lstrip("@").strip().lower()
                return clean if clean else str(val).strip().lower()
        return ""

    for attr in ["username", "user_name"]:
        val = getattr(user, attr, None)
        if val:
            return str(val).lstrip("@").strip().lower()

    for attr in ["first_name", "name", "nick", "id"]:
        val = getattr(user, attr, None)
        if val is not None and str(val).strip():
            clean = str(val).split("|")[0].lstrip("@").strip().lower()
            return clean if clean else str(val).strip().lower()

    return ""


def get_chat_id(message) -> int:
    if isinstance(message, dict):
        chat = message.get("chat")
        if isinstance(chat, dict) and "id" in chat:
            return chat["id"]
        if "chat_id" in message:
            return message["chat_id"]
        if "from" in message and isinstance(message["from"], dict) and "id" in message["from"]:
            return message["from"]["id"]

    chat = getattr(message, "chat", None)
    if chat:
        if hasattr(chat, "id"):
            return chat.id
        if isinstance(chat, dict) and "id" in chat:
            return chat["id"]
    if hasattr(message, "chat_id") and message.chat_id:
        return message.chat_id
    u = get_sender_user(message)
    if u:
        if hasattr(u, "id"):
            return u.id
        if isinstance(u, dict) and "id" in u:
            return u["id"]
    return 0


def get_message_text(message) -> str:
    if isinstance(message, dict):
        for k in ["text", "body", "caption", "message", "content"]:
            if k in message and message[k]:
                return str(message[k]).strip()
        return ""
    for attr in ["text", "body", "caption", "message", "content"]:
        val = getattr(message, attr, None)
        if val is not None and str(val).strip():
            return str(val).strip()
    if hasattr(message, "json") and isinstance(message.json, dict):
        return str(message.json.get("text") or "").strip()
    return ""


def get_or_register_player(user, chat_id: int):
    username = get_clean_username(user)
    if not username:
        return None

    cursor.execute(
        "SELECT username, balance_stars, balance_commission, total_deposited, wins, losses FROM players WHERE username = ?",
        (username,)
    )
    row = cursor.fetchone()

    if not row:
        cursor.execute(
            "INSERT INTO players (username, last_chat_id, balance_stars, balance_commission, total_deposited) VALUES (?, ?, 0.0, 0.0, 0.0)",
            (username, chat_id)
        )
        conn.commit()
        return {"username": username, "stars": 0.0, "commission": 0.0, "total_dep": 0.0, "wins": 0, "losses": 0}

    cursor.execute("UPDATE players SET last_chat_id = ? WHERE username = ?", (chat_id, username))
    conn.commit()
    return {"username": row[0], "stars": row[1], "commission": row[2], "total_dep": row[3], "wins": row[4], "losses": row[5]}


def safe_send(chat_id: int, text: str, reply_markup=None):
    if reply_markup:
        try:
            return bot.send_message(chat_id, text, reply_markup=reply_markup)
        except Exception:
            pass
    try:
        return bot.send_message(chat_id, text)
    except Exception as e:
        print(f"[Ошибка отправки в {chat_id}]: {e}")
        return None


# ----------------- ИГРОВЫЕ МЕХАНИКИ -----------------
CARD_RANKS = [
    ("6", 6), ("7", 7), ("8", 8), ("9", 9), ("10", 10),
    ("Валет", 11), ("Дама", 12), ("Король", 13), ("Туз", 14)
]
CARD_SUITS = ["♠️", "♥️", "♦️", "♣️"]


def play_game_round(game_mode: str):
    if game_mode == "карты":
        rank_name, rank_val = random.choice(CARD_RANKS)
        suit = random.choice(CARD_SUITS)
        return f"[{suit} {rank_name}] ({rank_val} очк.)", rank_val

    elif game_mode == "дартс":
        roll = random.randint(1, 100)
        if roll <= 15:
            return "🎯 Мимо мишени! (0 очков)", 0
        elif roll <= 75:
            score = random.choice([10, 15, 20, 25])
            return f"🎯 Попадание: {score} очков!", score
        elif roll <= 92:
            return "🎯 Зелёное кольцо! (40 очков)", 40
        else:
            return "🎯 В ЯБЛОЧКО! BULLSEYE! (50 очков) 💥", 50

    elif game_mode == "баскет":
        shots = [random.choice([0, 2, 3]) for _ in range(3)]
        total = sum(shots)
        visual = " ".join(["🗑️ Мимо" if s == 0 else f"🏀 +{s}п" for s in shots])
        return f"{visual} | Всего: **{total} очков**", total

    elif game_mode == "футбол":
        goals = [random.choice([True, False]) for _ in range(3)]
        total = sum(1 for g in goals if g)
        visual = " ".join(["⚽ ГОЛ!" if g else "🧤 Сейв!" for g in goals])
        return f"{visual} | Забито: **{total}/3**", total

    elif game_mode == "боулинг":
        pins = random.choice([3, 5, 7, 8, 9, 10])
        if pins == 10:
            return "🎳 💥 СТРАЙК! Все 10 кеглей сбиты!", 10
        return f"🎳 Сбито кеглей: {pins} из 10", pins

    elif game_mode == "монетка":
        side = random.choice([("🦅 ОРЁЛ", 2), ("🪙 РЕШКА", 1)])
        return f"{side[0]}", side[1]

    val = random.randint(1, 100)
    return f"🎲 Число: {val}", val


GAMES = {
    "карты": {"name": "Карты", "emoji": "🃏"},
    "дартс": {"name": "Дартс", "emoji": "🎯"},
    "баскет": {"name": "Баскетбол", "emoji": "🏀"},
    "футбол": {"name": "Футбол", "emoji": "⚽"},
    "боулинг": {"name": "Боулинг", "emoji": "🎳"},
    "монетка": {"name": "Монетка", "emoji": "🪙"}
}

CMD_TO_MODE = {
    "карты": "карты", "карта": "карты", "дуэль": "карты", "бой": "карты", "к": "карты",
    "дартс": "дартс", "дарт": "дартс", "darts": "дартс", "дротик": "дартс",
    "баскет": "баскет", "баскетбол": "баскет", "basket": "баскет",
    "футбол": "футбол", "фут": "футбол", "пенальти": "футбол", "гол": "футбол",
    "боулинг": "боулинг", "bowl": "боулинг", "кегли": "боулинг",
    "монетка": "монетка", "монета": "монетка", "coin": "монетка", "флип": "монетка"
}


def build_menu_kb(username: str):
    kb = [
        [{"text": "🎮 Все игры 1 на 1", "callback_data": "menu_games"}],
        [{"text": "🏆 Топ игроков", "callback_data": "menu_top"}],
        [{"text": "📥 Пополнить баланс", "callback_data": "menu_deposit"}],
        [{"text": "📤 Вывести Stars", "callback_data": "menu_withdraw"}],
        [{"text": "👤 Профиль / Баланс", "callback_data": "menu_profile"}]
    ]
    if username == "tael":
        kb.append([{"text": "💼 Касса комиссии (8%)", "callback_data": "menu_profit"}])
    return {"inline_keyboard": kb}


def get_top_balance_text() -> str:
    cursor.execute("SELECT username, balance_stars, wins FROM players ORDER BY balance_stars DESC LIMIT 10")
    rows = cursor.fetchall()
    if not rows:
        return "🏆 Топ игроков пока пуст."

    medals = ["🥇", "🥈", "🥉"] + [f"{i}." for i in range(4, 11)]
    text = "🏆 **ТОП-10 ИГРОКОВ ПО БАЛАНСУ:**\n\n"
    for idx, row in enumerate(rows):
        prefix = medals[idx] if idx < len(medals) else f"{idx + 1}."
        text += f"{prefix} @{row[0]} — **{row[1]:g} Stars** (Побед: {row[2]})\n"
    return text


def get_top_deposit_text() -> str:
    cursor.execute("SELECT username, total_deposited FROM players WHERE total_deposited > 0 ORDER BY total_deposited DESC LIMIT 10")
    rows = cursor.fetchall()
    if not rows:
        return "💎 Топ пополнений пока пуст."

    medals = ["🥇", "🥈", "🥉"] + [f"{i}." for i in range(4, 11)]
    text = "💎 **ТОП-10 ПО ДЕПОЗИТАМ (ТОП ДЕП):**\n\n"
    for idx, row in enumerate(rows):
        prefix = medals[idx] if idx < len(medals) else f"{idx + 1}."
        text += f"{prefix} @{row[0]} — деп: **{row[1]:g} Stars**\n"
    return text


# ----------------- ФОНОВЫЙ ТАЙМЕР АВТО-ОТМЕНЫ (ЧЕРЕЗ 1 ЧАС) -----------------
def auto_cancel_duels_worker():
    """Фоновый поток: проверяет ставки старше 1 часа и отменяет их с возвратом средств."""
    while True:
        try:
            time.sleep(30)
            now = int(time.time())
            expired_limit = now - EXPIRATION_TIME

            # Находим ставки, которые висят больше часа
            cursor.execute("""
                SELECT duel_id, creator_username, chat_id, amount 
                FROM duels 
                WHERE status = 'waiting' AND created_at > 0 AND created_at <= ?
            """, (expired_limit,))
            expired_duels = cursor.fetchall()

            for d in expired_duels:
                d_id, creator, c_id, amt = d
                # Возвращаем баланс игроку
                cursor.execute("UPDATE players SET balance_stars = balance_stars + ? WHERE username = ?", (amt, creator))
                cursor.execute("UPDATE duels SET status = 'expired' WHERE duel_id = ?", (d_id,))
                conn.commit()

                safe_send(
                    c_id,
                    f"⏰ **ДУЭЛЬ АВТОМАТИЧЕСКИ ОТМЕНЕНА (1 ЧАС)!**\n\n"
                    f"Игру `{d_id}` никто не принял за 60 минут.\n"
                    f"💰 Ставка **{amt:g} Stars** полностью возвращена игроку @{creator}."
                )
        except Exception as e:
            print(f"[ОШИБКА В ТАЙМЕРЕ АВТО-ОТМЕНЫ]: {e}")


# Запускаем фоновый таймер
cancel_thread = threading.Thread(target=auto_cancel_duels_worker, daemon=True)
cancel_thread.start()


# ----------------- ОБРАБОТЧИК КНОПОК МЕНЮ -----------------
@bot.callback_query_handler(func=lambda call: bool(call.data and not call.data.startswith("accept_") and not call.data.startswith("cancel_")))
def menu_callbacks(call):
    chat_id = getattr(call.message, "chat", None)
    chat_id = chat_id.id if chat_id and hasattr(chat_id, "id") else call.from_user.id
    username = get_clean_username(call.from_user)
    player = get_or_register_player(call.from_user, chat_id)

    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass

    if call.data == "menu_games":
        text = (
            "🎮 **Команды для запуска игр 1 на 1 в чате:**\n\n"
            "• `карты 50` или `/карты 50`\n"
            "• `дартс 50` или `/дартс 50`\n"
            "• `баскет 50` или `/баскет 50`\n"
            "• `футбол 50` или `/футбол 50`\n"
            "• `боулинг 50` или `/боулинг 50`\n"
            "• `монетка 50` или `/монетка 50`\n\n"
            "⚔️ Для принятия: кнопка под постом или `принять <ID>`\n"
            "❌ Для отмены: кнопка «Отменить» или `отмена <ID>`"
        )
        safe_send(chat_id, text)

    elif call.data == "menu_top":
        safe_send(chat_id, get_top_balance_text())

    elif call.data == "menu_deposit":
        text = (
            "📥 **Пополнение баланса Stars:**\n\n"
            "Для пополнения напишите администраторам проекта:\n"
            "• @tael\n"
            "• @easy\n\n"
            "*(Автоматическое пополнение появится чуть позже)*"
        )
        safe_send(chat_id, text)

    elif call.data == "menu_withdraw":
        text = (
            "📤 **Вывод Stars:**\n\n"
            f"Ваш текущий баланс: **{player['stars']:g} Stars**\n\n"
            "Для вывода используйте команду:\n"
            "`/withdraw <сумма>` (например: `/withdraw 100`)"
        )
        safe_send(chat_id, text)

    elif call.data == "menu_profile":
        text = (
            f"👤 **Профиль @{username}:**\n\n"
            f"⭐️ Основной баланс: **{player['stars']:g} Stars**\n"
            f"💎 Депозиты: **{player['total_dep']:g} Stars**\n"
            f"🏆 Побед: **{player['wins']}**\n"
            f"💀 Поражений: **{player['losses']}**"
        )
        safe_send(chat_id, text)

    elif call.data == "menu_profit":
        if username != "tael":
            return
        cursor.execute("SELECT balance_commission FROM players WHERE username = 'tael'")
        row = cursor.fetchone()
        comm = row[0] if row else 0.0
        safe_send(
            chat_id,
            f"💼 **ВТОРОЙ СЧЁТ @tael (КАССА КОМИССИЙ 8%):**\n\n"
            f"💰 Накоплено с дуэлей: **{comm:g} Stars**\n\n"
            "С каждой сыгранной дуэли в чате ровно 8% от общего банка падают на этот счёт."
        )


# ----------------- РУЧНАЯ ОТМЕНА ДУЭЛИ ЧЕРЕЗ КНОПКУ -----------------
@bot.callback_query_handler(func=lambda call: bool(call.data and call.data.startswith("cancel_")))
def cancel_duel_handler(call):
    try:
        duel_id = call.data.replace("cancel_", "")
        username = get_clean_username(call.from_user)
        chat_id = get_chat_id(call.message)

        cursor.execute("SELECT creator_username, amount, status FROM duels WHERE duel_id = ?", (duel_id,))
        row = cursor.fetchone()

        if not row or row[2] != "waiting":
            bot.answer_callback_query(call.id, text="Эту игру нельзя отменить или она уже начата!", show_alert=True)
            return

        creator, amount, _ = row

        if username != creator and username not in ADMIN_USERNAMES:
            bot.answer_callback_query(call.id, text="Отменить ставку может только её создатель!", show_alert=True)
            return

        # Возвращаем баланс создателю
        cursor.execute("UPDATE players SET balance_stars = balance_stars + ? WHERE username = ?", (amount, creator))
        cursor.execute("UPDATE duels SET status = 'cancelled' WHERE duel_id = ?", (duel_id,))
        conn.commit()

        try:
            bot.answer_callback_query(call.id, text="Дуэль успешно отменена, звёзды возвращены!")
        except Exception:
            pass

        safe_send(
            chat_id,
            f"❌ Дуэль `{duel_id}` была отменена.\n"
            f"💰 **{amount:g} Stars** возвращены на баланс @{creator}."
        )

    except Exception as e:
        print(f"[Ошибка отмены дуэли]: {e}")


# ----------------- ПРИНЯТИЕ ДУЭЛИ -----------------
@bot.callback_query_handler(func=lambda call: bool(call.data and call.data.startswith("accept_")))
def accept_duel_handler(call):
    try:
        duel_id = call.data.replace("accept_", "")
        opponent = get_clean_username(call.from_user)
        if not opponent:
            bot.answer_callback_query(call.id, text="Установите @username в профиле!", show_alert=True)
            return

        cursor.execute(
            "SELECT game_mode, creator_username, chat_id, amount, status FROM duels WHERE duel_id = ?",
            (duel_id,)
        )
        row = cursor.fetchone()

        if not row or row[4] != "waiting":
            bot.answer_callback_query(call.id, text="Дуэль уже завершена, отменена или неактивна!", show_alert=True)
            return

        game_mode, creator, chat_id, amount, _ = row

        if opponent == creator:
            bot.answer_callback_query(call.id, text="Нельзя играть против самого себя!", show_alert=True)
            return

        opp_player = get_or_register_player(call.from_user, chat_id)
        if opp_player["stars"] < amount:
            bot.answer_callback_query(call.id, text=f"Недостаточно Stars! Нужно {amount:g}.", show_alert=True)
            return

        # Блокируем дуэль и списываем ставку второго игрока
        cursor.execute("UPDATE duels SET status = 'active' WHERE duel_id = ?", (duel_id,))
        cursor.execute("UPDATE players SET balance_stars = balance_stars - ? WHERE username = ?", (amount, opponent))
        conn.commit()

        try:
            bot.answer_callback_query(call.id, text="Бой принят!")
        except Exception:
            pass

        mode_info = GAMES.get(game_mode, GAMES["карты"])
        emoji = mode_info["emoji"]

        safe_send(
            chat_id,
            f"🔥 **ДУЭЛЬ НАЧАЛАСЬ!**\n\n"
            f"👤 @{creator} ⚔️ @{opponent}\n"
            f"🎮 Игра: **{mode_info['name']}** {emoji}\n"
            f"⭐️️ Ставка: **{amount:g} Stars** с каждого\n\n"
            f"1️⃣ Ход делает @{creator}..."
        )

        round1_desc, round1_val = play_game_round(game_mode)
        time.sleep(2.5)

        safe_send(
            chat_id,
            f"👤 Результат @{creator}:\n"
            f"➡️ {round1_desc}\n\n"
            f"2️⃣ Теперь ход делает @{opponent}..."
        )

        round2_desc, round2_val = play_game_round(game_mode)
        time.sleep(2.5)

        safe_send(
            chat_id,
            f"👤 Результат @{opponent}:\n"
            f"➡️ {round2_desc}"
        )

        time.sleep(1.5)

        # РАСЧЁТ КОМИССИИ 8%
        total_pot = amount * 2
        fee_amount = round(total_pot * HOUSE_FEE, 2)
        win_amount = round(total_pot - fee_amount, 2)

        if round1_val > round2_val:
            # Начисляем победителю
            cursor.execute("UPDATE players SET balance_stars = balance_stars + ?, wins = wins + 1 WHERE username = ?", (win_amount, creator))
            cursor.execute("UPDATE players SET losses = losses + 1 WHERE username = ?", (opponent,))
            # Начисляем 8% комиссии на ВТОРОЙ баланс tael
            cursor.execute("UPDATE players SET balance_commission = balance_commission + ? WHERE username = 'tael'", (fee_amount,))
            conn.commit()

            safe_send(
                chat_id,
                f"🏆 **ПОБЕДИТЕЛЬ: @{creator}!** 🎉\n\n"
                f"{emoji} @{creator} одержал победу!\n"
                f"⭐️ Выигрыш: **+{win_amount:g} Stars** зачислен на баланс!\n"
                f"💼 *(Комиссия 8%: {fee_amount:g} Stars ушла в кассу)*"
            )

        elif round2_val > round1_val:
            cursor.execute("UPDATE players SET balance_stars = balance_stars + ?, wins = wins + 1 WHERE username = ?", (win_amount, opponent))
            cursor.execute("UPDATE players SET losses = losses + 1 WHERE username = ?", (creator,))
            # Начисляем 8% комиссии на ВТОРОЙ баланс tael
            cursor.execute("UPDATE players SET balance_commission = balance_commission + ? WHERE username = 'tael'", (fee_amount,))
            conn.commit()

            safe_send(
                chat_id,
                f"🏆 **ПОБЕДИТЕЛЬ: @{opponent}!** 🎉\n\n"
                f"{emoji} @{opponent} одержал победу!\n"
                f"⭐️ Выигрыш: **+{win_amount:g} Stars** зачислен на баланс!\n"
                f"💼 *(Комиссия 8%: {fee_amount:g} Stars ушла в кассу)*"
            )

        else:
            # НИЧЬЯ: комиссии нет, 100% возврат
            cursor.execute("UPDATE players SET balance_stars = balance_stars + ? WHERE username = ?", (amount, creator))
            cursor.execute("UPDATE players SET balance_stars = balance_stars + ? WHERE username = ?", (amount, opponent))
            conn.commit()

            safe_send(
                chat_id,
                f"🤝 **НИЧЬЯ!**\n\n"
                f"Одинаковый результат у обоих игроков!\n"
                f"Ставки по **{amount:g} Stars** полностью возвращены обоим."
            )

        cursor.execute("UPDATE duels SET status = 'finished' WHERE duel_id = ?", (duel_id,))
        conn.commit()

    except Exception as e:
        print(f"[Ошибка в accept_duel_handler]: {e}")
        traceback.print_exc()


# ----------------- ПРИЁМНИК ВСЕХ СООБЩЕНИЙ -----------------
@bot.message_handler(func=lambda msg: True)
def all_messages_handler(message):
    try:
        chat_id = get_chat_id(message)
        if not chat_id:
            return

        raw_text = get_message_text(message)
        if not raw_text:
            return

        sender = get_sender_user(message)
        username = get_clean_username(sender)

        tokens = []
        for t in raw_text.lower().strip().split():
            if t.startswith("/") and "@" in t:
                t = t.split("@")[0]
            tokens.append(t)

        if not tokens:
            return

        clean_cmd = tokens[0].lstrip("/")

        # 1. СТАРТ И МЕНЮ
        if clean_cmd in ["start", "menu", "help"]:
            p = get_or_register_player(sender, chat_id)
            comm_info = f"\n💼 Баланс комиссии: **{p['commission']:g} Stars**" if username == "tael" else ""
            text = (
                f"👑 **Добро пожаловать в {BOT_NAME}!**\n\n"
                f"⭐️ Ваш баланс: **{p['stars']:g} Stars**{comm_info}\n\n"
                "Все дуэли проходят **строго 1 на 1** со ставками в Stars!"
            )
            safe_send(chat_id, text, reply_markup=build_menu_kb(username))
            return

        # 2. БАЛАНС
        if clean_cmd in ["баланс", "balance", "счет"]:
            p = get_or_register_player(sender, chat_id)
            comm_info = f"\n💼 Баланс комиссии: **{p['commission']:g} Stars**" if username == "tael" else ""
            safe_send(chat_id, f"⭐️ Баланс @{username}: **{p['stars']:g} Stars**{comm_info}")
            return

        # 3. ПРОСМОТР ВТОРОГО БАЛАНСА КОМИССИЙ (ТОЛЬКО ДЛЯ TAEL)
        if clean_cmd in ["profit", "касса", "комиссия"]:
            if username != "tael":
                return
            cursor.execute("SELECT balance_commission FROM players WHERE username = 'tael'")
            row = cursor.fetchone()
            comm = row[0] if row else 0.0
            safe_send(
                chat_id,
                f"💼 **ВТОРОЙ БАЛАНС @tael (КАССА 8% КОМИССИЙ):**\n\n"
                f"💰 Накоплено: **{comm:g} Stars**"
            )
            return

        # 4. ТОП ПО БАЛАНСУ
        if clean_cmd in ["топ", "top", "лидеры"]:
            if len(tokens) >= 2 and tokens[1] in ["деп", "депозит", "депозитов", "dep"]:
                safe_send(chat_id, get_top_deposit_text())
            else:
                safe_send(chat_id, get_top_balance_text())
            return

        # 5. ТОП ДЕП
        if clean_cmd in ["топдеп", "topdep"]:
            safe_send(chat_id, get_top_deposit_text())
            return

        # 6. АДМИН-ВЫДАЧА (/give @username 100)
        if clean_cmd in ["give", "givestars"]:
            if username not in ADMIN_USERNAMES:
                return
            if len(tokens) < 3:
                safe_send(chat_id, "Использование: `/give @username 100`")
                return
            target = tokens[1].lstrip("@").lower()
            try:
                amt = float(tokens[2].replace(",", "."))
                if amt <= 0:
                    raise ValueError()
            except ValueError:
                safe_send(chat_id, "❌ Неверная сумма.")
                return

            cursor.execute("SELECT username FROM players WHERE username = ?", (target,))
            if not cursor.fetchone():
                safe_send(chat_id, f"❌ Игрок @{target} ещё не запускал бота.")
                return

            cursor.execute("""
                UPDATE players 
                SET balance_stars = balance_stars + ?, total_deposited = total_deposited + ? 
                WHERE username = ?
            """, (amt, amt, target))
            conn.commit()

            safe_send(chat_id, f"✅ Администратор @{username} начислил **{amt:g} Stars** игроку @{target}!")
            return

        # 7. ВЫВОД ЗВЁЗД
        if clean_cmd in ["withdraw", "вывод"]:
            if len(tokens) < 2:
                safe_send(chat_id, "Использование: `/withdraw <сумма>`")
                return
            try:
                amt = float(tokens[1].replace(",", "."))
                if amt <= 0:
                    raise ValueError()
            except ValueError:
                safe_send(chat_id, "❌ Неверная сумма.")
                return

            p = get_or_register_player(sender, chat_id)
            if p["stars"] < amt:
                safe_send(chat_id, f"❌ Недостаточно средств! Ваш баланс: {p['stars']:g} Stars.")
                return

            cursor.execute("UPDATE players SET balance_stars = balance_stars - ? WHERE username = ?", (amt, username))
            conn.commit()

            safe_send(
                chat_id,
                f"✅ **Заявка на вывод принята!**\n\n"
                f"Сумма: **{amt:g} Stars**\n"
                f"Игрок: @{username}\n\n"
                f"Администраторы @tael и @easy получили уведомление о выплате."
            )
            return

        # 8. РУЧНАЯ ОТМЕНА ДУЭЛИ ТЕКСТОМ: "отмена ID"
        if clean_cmd in ["отмена", "cancel"] and len(tokens) >= 2:
            target_id = tokens[1].strip()
            cursor.execute("SELECT creator_username, amount, status FROM duels WHERE duel_id = ?", (target_id,))
            row = cursor.fetchone()

            if not row or row[2] != "waiting":
                safe_send(chat_id, "❌ Дуэль не найдена, уже началась или отменена.")
                return

            creator, amount, _ = row
            if username != creator and username not in ADMIN_USERNAMES:
                safe_send(chat_id, "❌ Отменить ставку может только создатель дуэли!")
                return

            cursor.execute("UPDATE players SET balance_stars = balance_stars + ? WHERE username = ?", (amount, creator))
            cursor.execute("UPDATE duels SET status = 'cancelled' WHERE duel_id = ?", (target_id,))
            conn.commit()

            safe_send(
                chat_id,
                f"❌ Дуэль `{target_id}` отменена.\n"
                f"💰 **{amount:g} Stars** возвращены на баланс @{creator}."
            )
            return

        # 9. ПРИНЯТИЕ ДУЭЛИ ТЕКСТОМ: "принять ID"
        if clean_cmd in ["принять", "играть", "+", "play", "accept"] and len(tokens) >= 2:
            target_id = tokens[1].strip()
            cursor.execute("""
                SELECT game_mode, creator_username, chat_id, amount, status 
                FROM duels WHERE duel_id = ?
            """, (target_id,))
            row = cursor.fetchone()
            if not row or row[4] != "waiting":
                safe_send(chat_id, "❌ Дуэль не найдена или уже завершена.")
                return

            game_mode, creator, d_chat_id, amount, _ = row
            if username == creator:
                safe_send(chat_id, "❌ Нельзя играть против самого себя!")
                return

            opp_player = get_or_register_player(sender, chat_id)
            if opp_player["stars"] < amount:
                safe_send(chat_id, f"❌ Недостаточно Stars! Нужно {amount:g}.")
                return

            cursor.execute("UPDATE duels SET status = 'active' WHERE duel_id = ?", (target_id,))
            cursor.execute("UPDATE players SET balance_stars = balance_stars - ? WHERE username = ?", (amount, username))
            conn.commit()

            mode_info = GAMES.get(game_mode, GAMES["карты"])
            emoji = mode_info["emoji"]

            safe_send(
                chat_id,
                f"🔥 **ДУЭЛЬ НАЧАЛАСЬ!**\n\n"
                f"👤 @{creator} ⚔️ @{username}\n"
                f"🎮 Игра: **{mode_info['name']}** {emoji}\n"
                f"⭐️ Ставка: **{amount:g} Stars** с каждого\n\n"
                f"1️⃣ Ход делает @{creator}..."
            )

            round1_desc, round1_val = play_game_round(game_mode)
            time.sleep(2.5)

            safe_send(
                chat_id,
                f"👤 Результат @{creator}:\n"
                f"➡️ {round1_desc}\n\n"
                f"2️⃣ Теперь ход делает @{username}..."
            )

            round2_desc, round2_val = play_game_round(game_mode)
            time.sleep(2.5)

            safe_send(
                chat_id,
                f"👤 Результат @{username}:\n"
                f"➡️ {round2_desc}"
            )

            time.sleep(1.5)
            total_pot = amount * 2
            fee_amount = round(total_pot * HOUSE_FEE, 2)
            win_amount = round(total_pot - fee_amount, 2)

            if round1_val > round2_val:
                cursor.execute("UPDATE players SET balance_stars = balance_stars + ?, wins = wins + 1 WHERE username = ?", (win_amount, creator))
                cursor.execute("UPDATE players SET losses = losses + 1 WHERE username = ?", (username,))
                cursor.execute("UPDATE players SET balance_commission = balance_commission + ? WHERE username = 'tael'", (fee_amount,))
                conn.commit()

                safe_send(
                    chat_id,
                    f"🏆 **ПОБЕДИТЕЛЬ: @{creator}!** 🎉\n\n"
                    f"{emoji} @{creator} обошёл соперника!\n"
                    f"⭐️ Выигрыш: **+{win_amount:g} Stars** зачислен победителю!\n"
                    f"💼 *(Комиссия 8%: {fee_amount:g} Stars ушла в кассу)*"
                )

            elif round2_val > round1_val:
                cursor.execute("UPDATE players SET balance_stars = balance_stars + ?, wins = wins + 1 WHERE username = ?", (win_amount, username))
                cursor.execute("UPDATE players SET losses = losses + 1 WHERE username = ?", (creator,))
                cursor.execute("UPDATE players SET balance_commission = balance_commission + ? WHERE username = 'tael'", (fee_amount,))
                conn.commit()

                safe_send(
                    chat_id,
                    f"🏆 **ПОБЕДИТЕЛЬ: @{username}!** 🎉\n\n"
                    f"{emoji} @{username} обошёл соперника!\n"
                    f"⭐️ Выигрыш: **+{win_amount:g} Stars** зачислен победителю!\n"
                    f"💼 *(Комиссия 8%: {fee_amount:g} Stars ушла в кассу)*"
                )

            else:
                cursor.execute("UPDATE players SET balance_stars = balance_stars + ? WHERE username = ?", (amount, creator))
                cursor.execute("UPDATE players SET balance_stars = balance_stars + ? WHERE username = ?", (amount, username))
                conn.commit()

                safe_send(
                    chat_id,
                    f"🤝 **НИЧЬЯ!**\n\n"
                    f"Одинаковый результат у обоих игроков!\n"
                    f"Ставки по **{amount:g} Stars** возвращены обоим."
                )

            cursor.execute("UPDATE duels SET status = 'finished' WHERE duel_id = ?", (target_id,))
            conn.commit()
            return

        # 10. СОЗДАНИЕ ДУЭЛИ В ЧАТЕ
        if clean_cmd in CMD_TO_MODE:
            game_mode = CMD_TO_MODE[clean_cmd]

            if len(tokens) < 2:
                safe_send(chat_id, f"ℹ️ Укажите ставку. Пример: `{clean_cmd} 50`")
                return

            try:
                amount = float(tokens[1].replace(",", "."))
                if amount <= 0:
                    raise ValueError()
            except ValueError:
                safe_send(chat_id, f"❌ Неверный формат суммы. Пример: `{clean_cmd} 50`")
                return

            p = get_or_register_player(sender, chat_id)
            if not p or p["stars"] < amount:
                cur_bal = p["stars"] if p else 0.0
                safe_send(chat_id, f"❌ Недостаточно Stars на балансе!\nВаш баланс: **{cur_bal:g} Stars**")
                return

            # Списываем ставку создателя
            cursor.execute("UPDATE players SET balance_stars = balance_stars - ? WHERE username = ?", (amount, username))
            duel_id = str(int(time.time() * 1000))[-6:]
            now_ts = int(time.time())

            cursor.execute("""
                INSERT INTO duels (duel_id, game_mode, creator_username, chat_id, amount, status, created_at)
                VALUES (?, ?, ?, ?, ?, 'waiting', ?)
            """, (duel_id, game_mode, username, chat_id, amount, now_ts))
            conn.commit()

            mode_info = GAMES[game_mode]
            total_pot = amount * 2
            win_pot = round(total_pot * (1 - HOUSE_FEE), 2)

            kb = {
                "inline_keyboard": [
                    [
                        {"text": f"⚔️️ Принять бой ({amount:g} Stars)", "callback_data": f"accept_{duel_id}"},
                        {"text": "❌ Отменить", "callback_data": f"cancel_{duel_id}"}
                    ]
                ]
            }

            safe_send(
                chat_id,
                f"{mode_info['emoji']} **{mode_info['name'].upper()} 1 НА 1!**\n\n"
                f"👤 Создатель: @{username}\n"
                f"💰 Ставка: **{amount:g} Stars** с каждого\n"
                f"🏆 Банк победителя: **~{win_pot:g} Stars** (за вычетом 8%)\n"
                f"🆔 ID дуэли: `{duel_id}`\n\n"
                f"👉 Нажмите **«Принять бой»** или напишите: `принять {duel_id}`\n"
                f"⏳ *(Авто-отмена через 1 час, если никто не примет)*",
                reply_markup=kb
            )

    except Exception as e:
        print(f"[КРИТИЧЕСКАЯ ОШИБКА ДИСПЕТЧЕРА]: {e}")
        traceback.print_exc()


# ----------------- ЗАПУСК POLLING -----------------
if __name__ == "__main__":
    print(f"[{BOT_NAME}] Бот успешно запущен (8% комиссия tael, авто-отмена 1 час)...")
    while True:
        try:
            bot.polling(none_stop=True, timeout=10)
        except (socket.timeout, TimeoutError, ssl.SSLError, urllib.error.URLError, ConnectionResetError):
            time.sleep(1)
        except Exception as e:
            print(f"[POLLING ERROR]: {e}")
            time.sleep(2)
