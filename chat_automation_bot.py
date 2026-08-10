"""
MemTrace (@memtracebot) — бот-логгер редактирований и удалений сообщений через Chat Automation
(механизм Secretary Mode, представленный Telegram 7 мая 2026).

КАК ЭТО НАСТРОИТЬ:
1. В @BotFather:
   - /mybots -> выберите бота -> Bot Settings -> Secretary Mode -> Enable
     (это отдельная настройка от обычного режима бота)
2. Установите зависимости:
   pip install -r requirements.txt
3. Задайте токен через переменную окружения (НЕ вписывайте в код):
   export TELEGRAM_BOT_TOKEN="ваш_токен_от_BotFather"     (Linux/macOS)
   set TELEGRAM_BOT_TOKEN=ваш_токен_от_BotFather           (Windows cmd)
   $env:TELEGRAM_BOT_TOKEN="ваш_токен_от_BotFather"        (Windows PowerShell)
4. Положите картинку главного меню рядом со скриптом под именем main_menu.png
   (файл уже подготовлен и лежит вместе с этим скриптом в выгрузке)
5. Запустите: python chat_automation_bot.py
6. В самом Telegram (у пользователя, который хочет логировать свои чаты):
   Settings -> Chat Automation -> подключить этого бота -> выбрать,
   какие чаты ему разрешены (или исключить контакты и т.п.)
   -- это делается нативно в приложении Telegram, бот здесь ни на что
   не влияет, он только получает то, что ему разрешили.

ЧТО ДЕЛАЕТ БОТ:
- При /start проводит пользователя через онбординг:
  1) проверка подписки на канал @cachedmemory
  2) выбор языка (RU/EN) -- дальше весь интерфейс бота на выбранном языке
  3) согласие с политикой конфиденциальности
  4) показ главного меню (с картинкой main_menu.png)
- Кэширует все входящие в разрешённых чатах сообщения (business_message)
- При редактировании (edited_business_message) шлёт владельцу
  уведомление "Было / Стало"
- При удалении (deleted_business_messages) шлёт владельцу
  уведомление с текстом удалённого сообщения
- Уведомления приходят в личный чат владельца с этим ботом

ОГРАНИЧЕНИЯ:
- Все словари в этой версии хранятся в памяти и обнуляются при
  перезапуске. Для продакшена стоит вынести в sqlite/redis.
- Бот видит business-сообщения только в чатах, которые владелец
  разрешил через Settings -> Chat Automation.
"""

import html
import io
import logging
import os
from datetime import datetime, timedelta, timezone

from PIL import Image

from image_gen import TopEntry, render_subscription_image, render_top_image
import storage

from telegram import (
    BotCommand,
    BotCommandScopeDefault,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    MenuButtonCommands,
    Update,
)
from telegram.error import BadRequest
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PreCheckoutQueryHandler,
    TypeHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

# --- Онбординг ---
REQUIRED_CHANNEL = "@cachedmemory"
REQUIRED_CHANNEL_URL = "https://t.me/cachedmemory"

# Путь к картинке главного меню (лежит рядом со скриптом)
MAIN_MENU_IMAGE_PATH = "main_menu.png"

# owner_user_id -> личный chat_id владельца (куда слать уведомления)
OWNER_CHATS: dict[int, int] = {}

# business_connection_id -> user_id владельца этого подключения.
# Без этой карты бот не может понять, КОМУ из клиентов слать уведомление
# об удалении/изменении -- обязательно для мультипользовательского продакшена.
BUSINESS_CONNECTION_OWNER: dict[str, int] = {}

# (business_connection_id, chat_id, message_id) -> {"text":, "date":, "sender":}
MESSAGE_CACHE: dict[tuple, dict] = {}

# user_id -> "ru" / "en"
USER_LANG: dict[int, str] = {}

# user_id -> согласился ли с политикой конфиденциальности
POLICY_ACCEPTED: dict[int, bool] = {}

# --- Подписка и рефералка ---
TRIAL_DURATION = timedelta(days=1)
REFERRAL_BONUS_REFERRER = timedelta(days=10)
REFERRAL_BONUS_INVITEE = timedelta(days=6)

SUBSCRIPTIONS: dict[int, datetime] = {}   # user_id -> дата истечения (UTC)
INVITED_COUNT: dict[int, int] = {}        # user_id -> сколько друзей привёл
REFERRED_BY: dict[int, int] = {}          # user_id -> кто пригласил (чтобы не начислять бонус дважды)
USER_NAMES: dict[int, str] = {}           # user_id -> отображаемое имя (для топа)
USER_USERNAMES: dict[int, str] = {}       # user_id -> @username (без @), для топа и аватарок

# Условная длина "полного круга" для картинки-таймера подписки: 30 дней
# (длительность одной оплаты Stars). Если оставшегося времени больше --
# кольцо просто рисуется полностью красным.
RING_REFERENCE_CYCLE = timedelta(days=30)

# --- Промокоды ---
PROMO_CODES: dict[str, dict] = {
    "WELCOME7": {"bonus_days": 7, "max_uses": 1000, "used": 0},
}
PROMO_USED_BY: dict[int, set] = {}

# --- Оплата через Telegram Stars ---
STARS_PRICE = 100  # звёзд за месяц
STARS_SUBSCRIPTION_BONUS = timedelta(days=30)
STARS_PAYLOAD = "memtrace_month_100"

# --- Контакт разработчика (реклама/сотрудничество) ---
DEVELOPER_CONTACT_URL = os.environ.get("DEVELOPER_CONTACT_URL", "https://t.me/ВАШ_USERNAME")

# --- Администраторы ---
# Базовый список задаётся через переменную окружения (запятая между ID),
# например: ADMIN_USER_IDS=123456789,987654321
# Дополнительных админов можно добавлять/убирать прямо в боте командами
# /addadmin и /deladmin -- они сохраняются в БД и переживают рестарт.
ADMIN_IDS: set[int] = {
    int(x) for x in os.environ.get("ADMIN_USER_IDS", "").replace(" ", "").split(",") if x.isdigit()
}


def _is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# ---------------------------------------------------------------------------
# Подписка -- вспомогательные функции
# ---------------------------------------------------------------------------

def _ensure_subscription(user_id: int) -> None:
    if user_id not in SUBSCRIPTIONS:
        SUBSCRIPTIONS[user_id] = datetime.now(timezone.utc) + TRIAL_DURATION
        INVITED_COUNT.setdefault(user_id, 0)


def _extend_subscription(user_id: int, bonus: timedelta) -> None:
    now = datetime.now(timezone.utc)
    current = SUBSCRIPTIONS.get(user_id, now)
    base = current if current > now else now
    SUBSCRIPTIONS[user_id] = base + bonus


def _remaining_days_hours(user_id: int) -> tuple[int, int]:
    expiry = SUBSCRIPTIONS.get(user_id)
    if expiry is None:
        return 0, 0
    delta = expiry - datetime.now(timezone.utc)
    if delta.total_seconds() <= 0:
        return 0, 0
    return delta.days, delta.seconds // 3600


def _is_active(user_id: int) -> bool:
    expiry = SUBSCRIPTIONS.get(user_id)
    return expiry is not None and expiry > datetime.now(timezone.utc)


def _lang(user_id: int) -> str:
    return USER_LANG.get(user_id, "ru")


def _fraction_remaining(user_id: int) -> float:
    """Доля оставшегося времени подписки от условного полного цикла (для кольца)."""
    expiry = SUBSCRIPTIONS.get(user_id)
    if expiry is None:
        return 0.0
    remaining = (expiry - datetime.now(timezone.utc)).total_seconds()
    if remaining <= 0:
        return 0.0
    return min(1.0, remaining / RING_REFERENCE_CYCLE.total_seconds())


async def _get_ref_link(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> str:
    bot_username = (await context.bot.get_me()).username
    return f"https://t.me/{bot_username}?start={user_id}"


# ---------------------------------------------------------------------------
# Сохранение состояния (SQLite, см. storage.py) -- чтобы подписки, рефералы
# и промокоды не обнулялись при каждом рестарте/деплое.
# ---------------------------------------------------------------------------

def _snapshot_state() -> dict:
    return {
        "subscriptions": {str(uid): storage.dt_to_str(dt) for uid, dt in SUBSCRIPTIONS.items()},
        "invited_count": {str(uid): n for uid, n in INVITED_COUNT.items()},
        "referred_by": {str(uid): ref for uid, ref in REFERRED_BY.items()},
        "user_names": {str(uid): name for uid, name in USER_NAMES.items()},
        "user_usernames": {str(uid): un for uid, un in USER_USERNAMES.items()},
        "owner_chats": {str(uid): cid for uid, cid in OWNER_CHATS.items()},
        "business_connection_owner": dict(BUSINESS_CONNECTION_OWNER),
        "user_lang": {str(uid): lang for uid, lang in USER_LANG.items()},
        "policy_accepted": {str(uid): ok for uid, ok in POLICY_ACCEPTED.items()},
        "promo_used_by": {str(uid): sorted(codes) for uid, codes in PROMO_USED_BY.items()},
        "promo_codes_used": {code: data["used"] for code, data in PROMO_CODES.items()},
        "promo_codes_meta": {
            code: {"bonus_days": data["bonus_days"], "max_uses": data["max_uses"]}
            for code, data in PROMO_CODES.items()
        },
        "admin_ids": sorted(ADMIN_IDS),
    }


def _restore_state() -> None:
    state = storage.load_all()

    for uid_s, iso in state["subscriptions"].items():
        try:
            SUBSCRIPTIONS[int(uid_s)] = storage.str_to_dt(iso)
        except (ValueError, TypeError):
            continue
    for uid_s, n in state["invited_count"].items():
        INVITED_COUNT[int(uid_s)] = n
    for uid_s, ref in state["referred_by"].items():
        REFERRED_BY[int(uid_s)] = ref
    for uid_s, name in state["user_names"].items():
        USER_NAMES[int(uid_s)] = name
    for uid_s, un in state["user_usernames"].items():
        USER_USERNAMES[int(uid_s)] = un
    for uid_s, cid in state["owner_chats"].items():
        OWNER_CHATS[int(uid_s)] = cid
    for bc_id, uid in state["business_connection_owner"].items():
        BUSINESS_CONNECTION_OWNER[bc_id] = uid
    for uid_s, lang in state["user_lang"].items():
        USER_LANG[int(uid_s)] = lang
    for uid_s, ok in state["policy_accepted"].items():
        POLICY_ACCEPTED[int(uid_s)] = ok
    for uid_s, codes in state["promo_used_by"].items():
        PROMO_USED_BY[int(uid_s)] = set(codes)
    # Сначала восстанавливаем сами определения кодов (в т.ч. добавленные
    # через /addpromo -- их не было в PROMO_CODES при старте процесса),
    # затем -- сколько раз каждый уже использован.
    for code, meta in state.get("promo_codes_meta", {}).items():
        PROMO_CODES.setdefault(code, {"bonus_days": meta["bonus_days"], "max_uses": meta["max_uses"], "used": 0})
    for code, used in state["promo_codes_used"].items():
        if code in PROMO_CODES:
            PROMO_CODES[code]["used"] = used
    for uid in state.get("admin_ids", []):
        ADMIN_IDS.add(int(uid))

    logger.info(
        "Состояние восстановлено из storage: %d подписок, %d промо-активаций.",
        len(SUBSCRIPTIONS), sum(len(c) for c in PROMO_USED_BY.values()),
    )


def _persist() -> None:
    """Синхронно сохраняет текущее состояние. Вызывается после важных изменений
    (оплата, промокод, реферал, онбординг) -- дешевле, чем кажется: файл
    маленький, sqlite locally это доли миллисекунды."""
    try:
        storage.save_all(_snapshot_state())
    except Exception:
        logger.exception("Не удалось сохранить состояние.")


async def _autosave_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Подстраховка на случай, если где-то забыли вызвать _persist() вручную."""
    _persist()


# ---------------------------------------------------------------------------
# Кнопки: единый двуязычный словарь подписей
# ---------------------------------------------------------------------------

BTN = {
    "status":     {"ru": "📊 Статус",       "en": "📊 Status"},
    "invite":     {"ru": "🎁 Пригласить",   "en": "🎁 Invite"},
    "promo":      {"ru": "🎟 Промокод",     "en": "🎟 Promo code"},
    "top":        {"ru": "🏆 Топ",          "en": "🏆 Top"},
    "lang":       {"ru": "🌐 Язык",         "en": "🌐 Language"},
    "subscription": {"ru": "⭐ Подписка",   "en": "⭐ Subscription"},
    "help":       {"ru": "ℹ️ Помощь",       "en": "ℹ️ Help"},
    "back":       {"ru": "⬅️ Назад",        "en": "⬅️ Back"},
    "subscribe_channel": {"ru": "📢 Подписаться", "en": "📢 Subscribe"},
    "check_subscription": {"ru": "✅ Я подписался", "en": "✅ I subscribed"},
    "accept":     {"ru": "✅ Принимаю",     "en": "✅ Accept"},
    "decline":    {"ru": "❌ Не принимаю",  "en": "❌ Decline"},
    "share":      {"ru": "📤 Поделиться",   "en": "📤 Share"},
    "developer":  {"ru": "📩 Разработчик",  "en": "📩 Developer"},
}


def t(key: str, lang: str) -> str:
    return BTN[key][lang]


# ---------------------------------------------------------------------------
# Тексты (двуязычные словари)
# ---------------------------------------------------------------------------

SUBSCRIBE_GATE_TEXT = (
    "📢 Прежде чем продолжить / Before you continue\n\n"
    f"Подпишись на наш канал {REQUIRED_CHANNEL}, чтобы пользоваться ботом.\n"
    f"Subscribe to our channel {REQUIRED_CHANNEL} to use the bot."
)

LANGUAGE_GATE_TEXT = "🌐 Выберите язык / Choose language:"

PRIVACY_POLICY_TEXT = {
    "ru": (
        "📜 Перед началом\n\n"
        "Чтобы пользоваться ботом, подтвердите, что согласны с Политикой "
        "конфиденциальности.\n\n"
        "Коротко: бот сохраняет сообщения только из чатов, которые вы "
        "подключили сами через Chat Automation; мы никому их не передаём.\n\n"
        "Полный текст — по кнопке ниже."
    ),
    "en": (
        "📜 Before we start\n\n"
        "To use the bot, please confirm you agree with the Privacy Policy.\n\n"
        "In short: the bot only stores messages from chats you connected "
        "yourself via Chat Automation; we never share them with anyone.\n\n"
        "Full text is in the button below."
    ),
}

PRIVACY_POLICY_FULL_TEXT = {
    "ru": (
        "📜 Политика конфиденциальности MemTrace\n\n"
        "1. Какие данные собираются\n"
        "• Telegram ID, имя и @username — чтобы вести подписку, рефералку и топ.\n"
        "• Текст и медиа (фото/видео/войсы/стикеры и т.п.) сообщений ТОЛЬКО из "
        "тех чатов, которые вы сами подключили к боту через Telegram "
        "Settings → Chat Automation. Бот не имеет доступа ни к чему за "
        "пределами явно разрешённых вами чатов.\n\n"
        "2. Зачем это нужно\n"
        "Чтобы при удалении или редактировании сообщения собеседником бот "
        "мог показать вам, что было в оригинале.\n\n"
        "3. Хранение\n"
        "Данные хранятся на сервере бота (локальная база + оперативная "
        "память процесса). Кэш сообщений хранится временно, пока не удалён "
        "автором или не вытеснен новыми сообщениями. Мы не продаём и не "
        "передаём данные третьим лицам.\n\n"
        "4. Оплата\n"
        "Платежи проходят через Telegram Stars — бот не видит и не хранит "
        "данные банковских карт.\n\n"
        "5. Отключение и удаление данных\n"
        "Отключить бота от своих чатов можно в любой момент: Settings → "
        "Chat Automation → отключить. Чтобы запросить полное удаление "
        "своих данных, напишите нам в поддержку.\n\n"
        "6. Возраст\n"
        "Бот предназначен для лиц старше 16 лет."
    ),
    "en": (
        "📜 MemTrace Privacy Policy\n\n"
        "1. What data is collected\n"
        "• Telegram ID, name and @username — to manage your subscription, "
        "referrals and the leaderboard.\n"
        "• Text and media (photos/videos/voice/stickers etc.) of messages "
        "ONLY from chats you personally connected via Telegram "
        "Settings → Chat Automation. The bot has no access to anything "
        "outside chats you explicitly allowed.\n\n"
        "2. Why\n"
        "So that if someone deletes or edits a message, the bot can show "
        "you what the original said.\n\n"
        "3. Storage\n"
        "Data is stored on the bot's server (a local database plus the "
        "process's memory). The message cache is temporary and kept only "
        "until the author deletes it or it's evicted by newer messages. "
        "We do not sell or share your data with third parties.\n\n"
        "4. Payments\n"
        "Payments go through Telegram Stars — the bot never sees or stores "
        "any card details.\n\n"
        "5. Disconnecting and data deletion\n"
        "You can disconnect the bot from your chats anytime: Settings → "
        "Chat Automation → disconnect. To request full deletion of your "
        "data, contact our support.\n\n"
        "6. Age\n"
        "The bot is intended for users aged 16 and older."
    ),
}

START_TEXT = {
    "ru": (
        "🕵️ MemTrace — ничто не исчезает бесследно\n\n"
        "Каждое стёртое сообщение оставляет след. Я его нахожу.\n\n"
        "🗑 удалённые сообщения\n"
        "✏️ изменённые (покажу «было / стало»)\n"
        "🔥 одноразовые фото и видео (view-once)\n"
        "плюс стикеры, GIF, голосовые и кружочки\n\n"
        "Собеседник думает, что удалил улику. Я уже сохранил копию.\n\n"
    ),
    "en": (
        "🕵️ MemTrace — nothing disappears without a trace\n\n"
        "Every erased message leaves a trace. I find it.\n\n"
        "🗑 deleted messages\n"
        "✏️ edits (I'll show «before / after»)\n"
        "🔥 view-once photos and videos\n"
        "plus stickers, GIFs, voice messages and video notes\n\n"
        "They think they deleted the evidence. I already saved a copy.\n\n"
    ),
}

CONNECT_TUTORIAL_TEXT = {
    "ru": (
        "ℹ️ MemTrace - как это работает\n\n"
        "Я сохраняю то, что обычно исчезает в чатах:\n"
        "🗑 удалённые сообщения\n"
        "✏️ изменённые (покажу «было/стало»)\n"
        "🔥 одноразовые фото и видео (view-once)\n"
        "плюс стикеры, GIF, голосовые и кружочки.\n\n"
        "1. Подключение (Telegram Premium НЕ нужен)\n"
        "• Профиль → «Изменить»\n"
        "• Раздел «Чат-боты» (Chat Automation)\n"
        "• Добавь туда @memtracebot\n"
        "После этого я начну следить за твоими чатами.\n\n"
        "2. Удалённые и изменённые\n"
        "Ничего делать не нужно - как только собеседник удалит или изменит "
        "сообщение, я сразу пришлю его сюда.\n\n"
        "3. Одноразовые фото/видео (🔥)\n"
        "Их Telegram не отдаёт автоматически. Чтобы сохранить:\n"
        "👉 просто ответь (reply) на одноразовое сообщение прямо в чате - "
        "и я тут же пришлю копию сюда.\n\n"
        "4. Подписка\n"
        "Я ловлю сообщения, пока активна подписка. Продлить:\n"
        "🎁 приглашай друзей (/invite): тебе +10 дн., другу +6 дн.\n"
        "🎟️ активируй промокоды (/promo КОД)\n\n"
        "Команды:\n"
        "/start - запуск и меню\n"
        "/menu - меню\n"
        "/status - подписка и рефералы\n"
        "/invite - твоя ссылка\n"
        "/promo КОД - активировать промокод\n"
        "/top - топ приглашающих\n"
        "/lang - язык\n"
        "/help - эта справка"
    ),
    "en": (
        "ℹ️ MemTrace - how it works\n\n"
        "I save what usually disappears in chats:\n"
        "🗑 deleted messages\n"
        "✏️ edited ones (I'll show «before/after»)\n"
        "🔥 view-once photos and videos\n"
        "plus stickers, GIFs, voice messages and video notes.\n\n"
        "1. Connecting (Telegram Premium NOT required)\n"
        "• Profile → \"Edit\"\n"
        "• \"Chat Automation\" section\n"
        "• Add @memtracebot there\n"
        "After that I'll start watching your chats.\n\n"
        "2. Deleted and edited messages\n"
        "You don't need to do anything - as soon as someone deletes or edits "
        "a message, I'll send it here right away.\n\n"
        "3. View-once photos/videos (🔥)\n"
        "Telegram doesn't hand these over automatically. To save one:\n"
        "👉 just reply to the view-once message right in the chat - "
        "and I'll immediately send you a copy here.\n\n"
        "4. Subscription\n"
        "I catch messages while your subscription is active. To extend it:\n"
        "🎁 invite friends (/invite): you get +10 days, your friend gets +6 days\n"
        "🎟️ redeem promo codes (/promo CODE)\n\n"
        "Commands:\n"
        "/start - launch and menu\n"
        "/menu - menu\n"
        "/status - subscription and referrals\n"
        "/invite - your link\n"
        "/promo CODE - redeem a promo code\n"
        "/top - top inviters\n"
        "/lang - language\n"
        "/help - this help"
    ),
}


PROMO_PROMPT_TEXT = {
    "ru": (
        "🎟 Активация промокода\n\n"
        "Отправь код следующим сообщением (можно как есть, регистр не важен).\n"
        "Например: WELCOME7"
    ),
    "en": (
        "🎟 Redeem a promo code\n\n"
        "Send the code as your next message (case doesn't matter).\n"
        "For example: WELCOME7"
    ),
}

STARS_BUY_TEXT = {
    "ru": (
        f"⭐ Подписка MemTrace — {STARS_PRICE} Stars / месяц\n\n"
        "Оплата проходит через встроенные в Telegram Stars — без карт "
        "и посредников. Нажми кнопку ниже, чтобы открыть счёт на оплату."
    ),
    "en": (
        f"⭐ MemTrace subscription — {STARS_PRICE} Stars / month\n\n"
        "Paid with Telegram's built-in Stars — no cards, no third parties. "
        "Tap the button below to open the invoice."
    ),
}

POLICY_DECLINE_TEXT = {
    "ru": (
        "Без согласия с политикой конфиденциальности пользоваться "
        "ботом, к сожалению, нельзя.\n\nМожешь передумать в любой момент:"
    ),
    "en": (
        "Unfortunately, you can't use the bot without agreeing to the "
        "Privacy Policy.\n\nYou can change your mind anytime:"
    ),
}

NOT_SUBSCRIBED_ALERT = {
    "ru": "Похоже, ты ещё не подписался 🙂",
    "en": "Looks like you haven't subscribed yet 🙂",
}

LANG_SET_TEXT = {
    "ru": "✅ Язык: Русский",
    "en": "✅ Language: English",
}

TOP_TITLE = {
    "ru": "🏆 Топ приглашающих",
    "en": "🏆 Top inviters",
}
TOP_EMPTY = {
    "ru": "Пока никто никого не пригласил. Будь первым!",
    "en": "Nobody has invited anyone yet. Be the first!",
}

PROMO_ERRORS = {
    "not_found": {"ru": "❌ Такого промокода не существует.", "en": "❌ This promo code doesn't exist."},
    "already_used": {"ru": "⚠️ Ты уже использовал этот код.", "en": "⚠️ You've already used this code."},
    "exhausted": {"ru": "⚠️ Лимит активаций этого кода исчерпан.", "en": "⚠️ This code has reached its usage limit."},
    "empty": {
        "ru": "🤔 Это не похоже на промокод. Отправь код ещё раз или нажми «Назад».",
        "en": "🤔 That doesn't look like a promo code. Send the code again or tap \"Back\".",
    },
}

BOT_COMMANDS_RU = [
    BotCommand("start", "запуск и меню"),
    BotCommand("menu", "меню"),
    BotCommand("status", "подписка и рефералы"),
    BotCommand("invite", "твоя ссылка"),
    BotCommand("promo", "активировать промокод"),
    BotCommand("top", "топ приглашающих"),
    BotCommand("lang", "язык"),
    BotCommand("help", "эта справка"),
]

BOT_COMMANDS_EN = [
    BotCommand("start", "launch and menu"),
    BotCommand("menu", "menu"),
    BotCommand("status", "subscription and referrals"),
    BotCommand("invite", "your link"),
    BotCommand("promo", "redeem a promo code"),
    BotCommand("top", "top inviters"),
    BotCommand("lang", "language"),
    BotCommand("help", "this help"),
]


# ---------------------------------------------------------------------------
# Форматирование текстов, зависящих от данных пользователя
# ---------------------------------------------------------------------------

async def format_status(user_id: int, context: ContextTypes.DEFAULT_TYPE, lang: str = "ru") -> str:
    days, hours = _remaining_days_hours(user_id)
    if lang == "en":
        return f"🛰 Your status\nSubscription: {days}d {hours}h left"
    return f"🛰 Твой статус\nПодписка: ещё {days} дн. {hours} ч."


async def format_invite(user_id: int, context: ContextTypes.DEFAULT_TYPE, lang: str = "ru") -> str:
    invited = INVITED_COUNT.get(user_id, 0)
    link = await _get_ref_link(user_id, context)
    r_days = REFERRAL_BONUS_REFERRER.days
    i_days = REFERRAL_BONUS_INVITEE.days
    if lang == "en":
        return (
            "🎁 Invite friends\n\n"
            "For every friend who joins via your link:\n"
            f"• you get +{r_days} days of subscription\n"
            f"• your friend gets +{i_days} days of subscription\n\n"
            f"👥 Invited so far: {invited}\n\n"
            f"🔗 Your link:\n{link}"
        )
    return (
        "🎁 Пригласи друзей\n\n"
        "За каждого друга, который зайдёт по твоей ссылке:\n"
        f"• тебе +{r_days} дн. подписки\n"
        f"• другу +{i_days} дн. подписки\n\n"
        f"👥 Уже приглашено: {invited}\n\n"
        f"🔗 Твоя ссылка:\n{link}"
    )


def format_top(lang: str) -> str:
    ranked = sorted(INVITED_COUNT.items(), key=lambda kv: kv[1], reverse=True)
    ranked = [(uid, n) for uid, n in ranked if n > 0][:10]
    title = TOP_TITLE[lang]
    if not ranked:
        return f"{title}\n\n{TOP_EMPTY[lang]}"

    medals = ["🥇", "🥈", "🥉"]
    lines = [title, ""]
    for i, (uid, n) in enumerate(ranked):
        prefix = medals[i] if i < 3 else f"{i + 1}."
        name = USER_NAMES.get(uid, f"ID {uid}")
        suffix = "friends" if lang == "en" else "друзей"
        lines.append(f"{prefix} {name} — {n} {suffix}")
    return "\n".join(lines)


async def _fetch_avatar(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> "Image.Image | None":
    """Скачивает аватарку пользователя из Telegram для картинки топа. None, если её нет."""
    try:
        photos = await context.bot.get_user_profile_photos(user_id, limit=1)
        if photos.total_count == 0:
            return None
        file_id = photos.photos[0][-1].file_id  # самый большой размер
        tg_file = await context.bot.get_file(file_id)
        buf = io.BytesIO()
        await tg_file.download_to_memory(buf)
        buf.seek(0)
        return Image.open(buf).convert("RGB")
    except Exception as exc:  # аватарки нет, приватность и т.п. -- не критично
        logger.debug("Не удалось получить аватар %s: %s", user_id, exc)
        return None


async def _build_top_entries(context: ContextTypes.DEFAULT_TYPE, limit: int = 10) -> list[TopEntry]:
    ranked = sorted(INVITED_COUNT.items(), key=lambda kv: kv[1], reverse=True)
    ranked = [(uid, n) for uid, n in ranked if n > 0][:limit]

    entries: list[TopEntry] = []
    for i, (uid, n) in enumerate(ranked, start=1):
        name = USER_NAMES.get(uid, f"ID {uid}")
        username = USER_USERNAMES.get(uid)
        photo = await _fetch_avatar(uid, context)
        entries.append(TopEntry(rank=i, name=name, username=username, invited=n, photo=photo))
    return entries


async def send_top_image(chat_id: int, context: ContextTypes.DEFAULT_TYPE, lang: str) -> None:
    try:
        entries = await _build_top_entries(context)
        buf = render_top_image(entries, lang)
        await context.bot.send_photo(
            chat_id=chat_id, photo=buf, filename="top.png", reply_markup=main_menu_keyboard(lang),
        )
    except Exception:
        logger.exception("Не удалось сгенерировать картинку топа, отправляю текстом")
        await context.bot.send_message(
            chat_id=chat_id, text=format_top(lang), reply_markup=main_menu_keyboard(lang),
        )


async def send_subscription_image(
    user_id: int, chat_id: int, context: ContextTypes.DEFAULT_TYPE, lang: str,
) -> None:
    days, hours = _remaining_days_hours(user_id)
    minutes = 0
    expiry = SUBSCRIPTIONS.get(user_id)
    if expiry is not None:
        delta = expiry - datetime.now(timezone.utc)
        if delta.total_seconds() > 0:
            minutes = (delta.seconds % 3600) // 60
    active = _is_active(user_id)
    fraction = _fraction_remaining(user_id)

    if not active:
        caption = "⏳ Подписка не активна." if lang == "ru" else "⏳ Subscription is not active."
    elif lang == "en":
        caption = f"⏳ Time left: {days}d {hours}h {minutes}m"
    else:
        caption = f"⏳ Осталось: {days} дн. {hours} ч. {minutes} мин."

    try:
        buf = render_subscription_image(days, hours, minutes, fraction, lang=lang, active=active)
        await context.bot.send_photo(
            chat_id=chat_id, photo=buf, filename="subscription.png",
            caption=caption, reply_markup=main_menu_keyboard(lang),
        )
    except Exception:
        logger.exception("Не удалось сгенерировать картинку подписки, отправляю текстом")
        await context.bot.send_message(
            chat_id=chat_id,
            text=await format_status(user_id, context, lang),
            reply_markup=main_menu_keyboard(lang),
        )


def _clear_promo_wait(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Сбрасывает ожидание ввода промокода (если пользователь ушёл в другой раздел)."""
    context.user_data.pop("awaiting_promo", None)


def _redeem_promo_code(user_id: int, code: str, lang: str) -> str:
    if not code:
        return PROMO_ERRORS["empty"][lang]

    promo = PROMO_CODES.get(code)
    used_by_user = PROMO_USED_BY.setdefault(user_id, set())

    if promo is None:
        return PROMO_ERRORS["not_found"][lang]
    if code in used_by_user:
        return PROMO_ERRORS["already_used"][lang]
    if promo["used"] >= promo["max_uses"]:
        return PROMO_ERRORS["exhausted"][lang]

    promo["used"] += 1
    used_by_user.add(code)
    _extend_subscription(user_id, timedelta(days=promo["bonus_days"]))
    _persist()

    if lang == "en":
        return f"✅ Code activated! +{promo['bonus_days']} days added to your subscription."
    return f"✅ Промокод активирован! +{promo['bonus_days']} дн. к подписке."


# ---------------------------------------------------------------------------
# Клавиатуры
# ---------------------------------------------------------------------------

def subscribe_gate_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📢 Подписаться / Subscribe", url=REQUIRED_CHANNEL_URL)],
            [InlineKeyboardButton("✅ Я подписался / I subscribed", callback_data="check_subscription")],
        ]
    )


def language_gate_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🇷🇺 Русский", callback_data="lang_ru"),
                InlineKeyboardButton("🇬🇧 English", callback_data="lang_en"),
            ]
        ]
    )


def privacy_policy_keyboard(lang: str) -> InlineKeyboardMarkup:
    full_text_label = "📄 Полный текст политики" if lang == "ru" else "📄 Full policy text"
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(full_text_label, callback_data="policy_full")],
            [
                InlineKeyboardButton(t("accept", lang), callback_data="policy_accept"),
                InlineKeyboardButton(t("decline", lang), callback_data="policy_decline"),
            ],
        ]
    )


def back_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(t("back", lang), callback_data="menu_back")]])


def buy_keyboard(lang: str) -> InlineKeyboardMarkup:
    label = f"⭐ Оплатить {STARS_PRICE} Stars" if lang == "ru" else f"⭐ Pay {STARS_PRICE} Stars"
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(label, callback_data="menu_buy_pay")],
            [InlineKeyboardButton(t("back", lang), callback_data="menu_back")],
        ]
    )


def invite_keyboard(link: str, lang: str) -> InlineKeyboardMarkup:
    share_text = "MemTrace" if lang == "en" else "MemTrace — ничто не исчезает бесследно"
    share_url = f"https://t.me/share/url?url={link}&text={share_text}"
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(t("share", lang), url=share_url)],
            [InlineKeyboardButton(t("back", lang), callback_data="menu_back")],
        ]
    )


def main_menu_keyboard(lang: str = "ru") -> InlineKeyboardMarkup:
    """Клавиатура под стартовым сообщением. Статус — во всю строку сверху."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(t("status", lang), callback_data="menu_status")],
            [
                InlineKeyboardButton(t("invite", lang), callback_data="menu_invite"),
                InlineKeyboardButton(t("promo", lang), callback_data="menu_promo"),
            ],
            [
                InlineKeyboardButton(t("top", lang), callback_data="menu_top"),
                InlineKeyboardButton(t("lang", lang), callback_data="menu_lang"),
            ],
            [InlineKeyboardButton(t("subscription", lang), callback_data="menu_buy")],
            [InlineKeyboardButton(t("help", lang), callback_data="menu_help")],
            [InlineKeyboardButton(t("developer", lang), url=DEVELOPER_CONTACT_URL)],
        ]
    )


# ---------------------------------------------------------------------------
# Онбординг: подписка -> язык -> политика -> главное меню
# ---------------------------------------------------------------------------

async def _is_subscribed(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        member = await context.bot.get_chat_member(REQUIRED_CHANNEL, user_id)
        return member.status not in ("left", "kicked")
    except BadRequest as exc:
        logger.warning("Не удалось проверить подписку на %s: %s", REQUIRED_CHANNEL, exc)
        return True


async def send_subscribe_gate(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    await context.bot.send_message(
        chat_id=chat_id, text=SUBSCRIBE_GATE_TEXT, reply_markup=subscribe_gate_keyboard(),
    )


async def send_language_gate(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    await context.bot.send_message(
        chat_id=chat_id, text=LANGUAGE_GATE_TEXT, reply_markup=language_gate_keyboard(),
    )


async def send_privacy_policy(chat_id: int, context: ContextTypes.DEFAULT_TYPE, lang: str) -> None:
    await context.bot.send_message(
        chat_id=chat_id, text=PRIVACY_POLICY_TEXT[lang], reply_markup=privacy_policy_keyboard(lang),
    )


async def send_main_menu(chat_id: int, context: ContextTypes.DEFAULT_TYPE, lang: str) -> None:
    if os.path.exists(MAIN_MENU_IMAGE_PATH):
        with open(MAIN_MENU_IMAGE_PATH, "rb") as photo:
            await context.bot.send_photo(
                chat_id=chat_id, photo=photo,
                caption=START_TEXT[lang], reply_markup=main_menu_keyboard(lang),
            )
    else:
        await context.bot.send_message(
            chat_id=chat_id, text=START_TEXT[lang], reply_markup=main_menu_keyboard(lang),
        )


async def advance_onboarding(user_id: int, chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Решает, какой шаг онбординга показать дальше."""
    if not await _is_subscribed(user_id, context):
        await send_subscribe_gate(chat_id, context)
        return

    if user_id not in USER_LANG:
        await send_language_gate(chat_id, context)
        return

    if not POLICY_ACCEPTED.get(user_id):
        await send_privacy_policy(chat_id, context, _lang(user_id))
        return

    await send_main_menu(chat_id, context, _lang(user_id))


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/start -- запоминаем chat_id владельца, обрабатываем реферала, ведём по онбордингу."""
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    OWNER_CHATS[user.id] = chat.id
    USER_NAMES[user.id] = user.full_name or (f"@{user.username}" if user.username else f"ID {user.id}")
    if user.username:
        USER_USERNAMES[user.id] = user.username
    _ensure_subscription(user.id)

    if context.args:
        try:
            referrer_id = int(context.args[0])
        except ValueError:
            referrer_id = None
        if referrer_id and referrer_id != user.id and user.id not in REFERRED_BY:
            REFERRED_BY[user.id] = referrer_id
            INVITED_COUNT[referrer_id] = INVITED_COUNT.get(referrer_id, 0) + 1
            _extend_subscription(referrer_id, REFERRAL_BONUS_REFERRER)
            _extend_subscription(user.id, REFERRAL_BONUS_INVITEE)

    _persist()
    await advance_onboarding(user.id, chat.id, context)


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/menu -- показывает главное меню (или доводит до него через онбординг)."""
    user = update.effective_user
    chat = update.effective_chat
    await advance_onboarding(user.id, chat.id, context)


async def lang_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/lang -- позволяет сменить язык в любой момент."""
    chat = update.effective_chat
    await send_language_gate(chat.id, context)


async def onboarding_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    await query.answer()
    action = query.data

    if action == "check_subscription":
        if await _is_subscribed(user.id, context):
            await query.message.delete()
            await advance_onboarding(user.id, query.message.chat_id, context)
        else:
            await query.answer(NOT_SUBSCRIBED_ALERT[_lang(user.id)], show_alert=True)
        return

    if action in ("lang_ru", "lang_en"):
        lang = "ru" if action == "lang_ru" else "en"
        USER_LANG[user.id] = lang
        _persist()
        await query.edit_message_text(LANG_SET_TEXT[lang])
        await advance_onboarding(user.id, query.message.chat_id, context)
        return

    if action == "policy_accept":
        POLICY_ACCEPTED[user.id] = True
        _persist()
        await query.message.delete()
        await advance_onboarding(user.id, query.message.chat_id, context)
        return

    if action == "policy_decline":
        lang = _lang(user.id)
        await query.edit_message_text(
            POLICY_DECLINE_TEXT[lang], reply_markup=privacy_policy_keyboard(lang),
        )
        return

    if action == "policy_full":
        lang = _lang(user.id)
        await context.bot.send_message(chat_id=query.message.chat_id, text=PRIVACY_POLICY_FULL_TEXT[lang])
        return


async def _replace_with_text(
    query, context: ContextTypes.DEFAULT_TYPE, text: str, reply_markup=None,
) -> None:
    """Заменяет текущее сообщение меню новым текстовым.

    Не используем query.edit_message_text() напрямую, потому что главное меню
    отправляется как ФОТО с подписью (main_menu.png) -- edit_message_text не
    умеет превращать фото-сообщение в текстовое и падает с ошибкой. Поэтому
    вместо edit всегда удаляем старое сообщение и шлём новое текстом -- это
    работает одинаково что после фото, что после текста.
    """
    chat_id = query.message.chat_id
    try:
        await query.message.delete()
    except Exception:
        logger.debug("Не удалось удалить предыдущее сообщение меню (возможно, уже удалено).")
    await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)


async def menu_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик inline-кнопок главного меню."""
    query = update.callback_query
    user = update.effective_user
    await query.answer()
    action = query.data
    lang = _lang(user.id)

    # Если пользователь ушёл в другой раздел меню, не дожидаясь ввода
    # промокода -- отменяем ожидание, чтобы случайное сообщение позже
    # не было принято за промокод.
    if action != "menu_promo":
        _clear_promo_wait(context)

    if action == "menu_help":
        await _replace_with_text(query, context, CONNECT_TUTORIAL_TEXT[lang], main_menu_keyboard(lang))
        return

    if action == "menu_invite":
        link = await _get_ref_link(user.id, context)
        await _replace_with_text(query, context, await format_invite(user.id, context, lang), invite_keyboard(link, lang))
        return

    if action == "menu_status":
        # Меняем текстовое/фото-сообщение меню на картинку с кольцом подписки.
        try:
            await query.message.delete()
        except Exception:
            logger.debug("Не удалось удалить предыдущее сообщение меню.")
        await send_subscription_image(user.id, query.message.chat_id, context, lang)
        return

    if action == "menu_promo":
        context.user_data["awaiting_promo"] = True
        await _replace_with_text(query, context, PROMO_PROMPT_TEXT[lang], back_keyboard(lang))
        return

    if action == "menu_top":
        # Топ рисуется как картинка через Pillow, а не текстом -- заменяем сообщение.
        try:
            await query.message.delete()
        except Exception:
            logger.debug("Не удалось удалить предыдущее сообщение меню.")
        await send_top_image(query.message.chat_id, context, lang)
        return

    if action == "menu_lang":
        await _replace_with_text(query, context, LANGUAGE_GATE_TEXT, language_gate_keyboard())
        return

    if action == "menu_buy":
        await _replace_with_text(query, context, STARS_BUY_TEXT[lang], buy_keyboard(lang))
        return

    if action == "menu_buy_pay":
        await send_stars_invoice(query.message.chat_id, context, lang)
        return

    if action == "menu_back":
        chat_id = query.message.chat_id
        try:
            await query.message.delete()
        except Exception:
            logger.debug("Не удалось удалить предыдущее сообщение меню.")
        await send_main_menu(chat_id, context, lang)
        return


async def send_stars_invoice(chat_id: int, context: ContextTypes.DEFAULT_TYPE, lang: str) -> None:
    title = "MemTrace — 1 месяц" if lang == "ru" else "MemTrace — 1 month"
    description = (
        f"Подписка MemTrace на 30 дней ({STARS_PRICE} Telegram Stars)"
        if lang == "ru"
        else f"MemTrace subscription for 30 days ({STARS_PRICE} Telegram Stars)"
    )
    await context.bot.send_invoice(
        chat_id=chat_id,
        title=title,
        description=description,
        payload=STARS_PAYLOAD,
        currency="XTR",  # Telegram Stars
        prices=[LabeledPrice(title, STARS_PRICE)],
    )


async def precheckout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.pre_checkout_query
    if query.invoice_payload != STARS_PAYLOAD:
        await query.answer(ok=False, error_message="Неизвестный платёж. / Unknown payment.")
        return
    await query.answer(ok=True)


async def successful_payment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    lang = _lang(user.id)
    _extend_subscription(user.id, STARS_SUBSCRIPTION_BONUS)
    _persist()
    days, hours = _remaining_days_hours(user.id)
    if lang == "en":
        text = (
            f"✅ Payment received! +{STARS_SUBSCRIPTION_BONUS.days} days added.\n"
            f"Subscription now: {days}d {hours}h left."
        )
    else:
        text = (
            f"✅ Оплата прошла! Добавлено +{STARS_SUBSCRIPTION_BONUS.days} дн.\n"
            f"Подписка теперь: {days} дн. {hours} ч."
        )
    await update.message.reply_text(text, reply_markup=main_menu_keyboard(lang))


async def _clear_promo_wait_on_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Если пользователь ввёл любую команду вместо промокода -- отменяем ожидание.

    Работает в отдельной группе обработчиков (см. main()), поэтому не мешает
    обычным CommandHandler'ам ниже -- она просто выполняется первой.
    """
    _clear_promo_wait(context)


async def promo_code_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ловит текст, который пользователь шлёт после нажатия кнопки «Промокод»."""
    if not context.user_data.get("awaiting_promo"):
        return

    user = update.effective_user
    lang = _lang(user.id)
    code = (update.message.text or "").strip().upper()[:32]

    if not code:
        # Пустой ввод -- остаёмся в режиме ожидания, даём попробовать ещё раз.
        await update.message.reply_text(PROMO_ERRORS["empty"][lang], reply_markup=back_keyboard(lang))
        return

    context.user_data.pop("awaiting_promo", None)
    result_text = _redeem_promo_code(user.id, code, lang)
    await update.message.reply_text(result_text, reply_markup=main_menu_keyboard(lang))


async def promo_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    lang = _lang(user.id)
    if not context.args:
        # /promo без аргумента -- ведём себя как кнопка «Промокод»: просим
        # прислать код следующим сообщением.
        context.user_data["awaiting_promo"] = True
        await update.message.reply_text(PROMO_PROMPT_TEXT[lang], reply_markup=back_keyboard(lang))
        return
    code = context.args[0].strip().upper()[:32]
    await update.message.reply_text(
        _redeem_promo_code(user.id, code, lang), reply_markup=main_menu_keyboard(lang),
    )


async def top_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    lang = _lang(user.id)
    await send_top_image(chat.id, context, lang)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    lang = _lang(user.id)
    await update.message.reply_text(CONNECT_TUTORIAL_TEXT[lang], reply_markup=main_menu_keyboard(lang))


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    lang = _lang(user.id)
    await send_subscription_image(user.id, chat.id, context, lang)


async def invite_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    lang = _lang(user.id)
    link = await _get_ref_link(user.id, context)
    await update.message.reply_text(
        await format_invite(user.id, context, lang), reply_markup=invite_keyboard(link, lang),
    )


# ---------------------------------------------------------------------------
# Админ-панель: управление подписками пользователей и промокодами.
# Доступ -- только для user_id из ADMIN_IDS (env ADMIN_USER_IDS + /addadmin).
# ---------------------------------------------------------------------------

ADMIN_HELP_TEXT = (
    "🛠 Админ-панель MemTrace\n\n"
    "/grant <user_id> <дни> — продлить подписку пользователю\n"
    "/revoke <user_id> — немедленно завершить подписку\n"
    "/finduser <user_id> — показать статус пользователя\n"
    "/stats — общая статистика бота\n\n"
    "/addpromo <КОД> <дни> <лимит активаций> — создать промокод\n"
    "/delpromo <КОД> — удалить промокод\n"
    "/listpromo — список всех промокодов\n\n"
    "/addadmin <user_id> — выдать доступ к админке\n"
    "/deladmin <user_id> — забрать доступ к админке\n"
    "/admins — список текущих администраторов"
)


async def _require_admin(update: Update) -> bool:
    user = update.effective_user
    if not user or not _is_admin(user.id):
        await update.message.reply_text("⛔ Команда доступна только администраторам.")
        return False
    return True


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update):
        return
    await update.message.reply_text(ADMIN_HELP_TEXT)


def _parse_user_id(raw: str) -> int | None:
    raw = raw.strip().lstrip("@")
    return int(raw) if raw.isdigit() else None


async def grant_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update):
        return
    if len(context.args) < 2:
        await update.message.reply_text("Использование: /grant <user_id> <дни>")
        return
    target_id = _parse_user_id(context.args[0])
    try:
        days = int(context.args[1])
    except ValueError:
        target_id = None
    if target_id is None:
        await update.message.reply_text("user_id и количество дней должны быть числами.")
        return

    _ensure_subscription(target_id)
    _extend_subscription(target_id, timedelta(days=days))
    _persist()
    d, h = _remaining_days_hours(target_id)
    await update.message.reply_text(
        f"✅ Пользователю {target_id} добавлено {days} дн. Текущая подписка: {d} дн. {h} ч."
    )
    owner_chat = OWNER_CHATS.get(target_id)
    if owner_chat:
        try:
            lang = _lang(target_id)
            note = (
                f"🎁 Администратор продлил твою подписку на {days} дн.!"
                if lang == "ru"
                else f"🎁 An admin extended your subscription by {days} day(s)!"
            )
            await context.bot.send_message(chat_id=owner_chat, text=note)
        except Exception:
            pass


async def revoke_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update):
        return
    if not context.args:
        await update.message.reply_text("Использование: /revoke <user_id>")
        return
    target_id = _parse_user_id(context.args[0])
    if target_id is None:
        await update.message.reply_text("user_id должен быть числом.")
        return
    SUBSCRIPTIONS[target_id] = datetime.now(timezone.utc)
    _persist()
    await update.message.reply_text(f"✅ Подписка пользователя {target_id} завершена.")


async def finduser_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update):
        return
    if not context.args:
        await update.message.reply_text("Использование: /finduser <user_id>")
        return
    target_id = _parse_user_id(context.args[0])
    if target_id is None:
        await update.message.reply_text("user_id должен быть числом.")
        return
    if target_id not in SUBSCRIPTIONS:
        await update.message.reply_text("Такого пользователя нет в базе.")
        return
    d, h = _remaining_days_hours(target_id)
    name = USER_NAMES.get(target_id, "?")
    username = USER_USERNAMES.get(target_id)
    invited = INVITED_COUNT.get(target_id, 0)
    referrer = REFERRED_BY.get(target_id)
    lang = USER_LANG.get(target_id, "-")
    active = "активна ✅" if _is_active(target_id) else "неактивна ❌"
    lines = [
        f"👤 {name}" + (f" (@{username})" if username else ""),
        f"ID: {target_id}",
        f"Подписка: {active}, осталось {d} дн. {h} ч.",
        f"Приглашено друзей: {invited}",
        f"Язык: {lang}",
    ]
    if referrer:
        lines.append(f"Приглашён пользователем: {referrer}")
    await update.message.reply_text("\n".join(lines))


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update):
        return
    total_users = len(SUBSCRIPTIONS)
    active_users = sum(1 for uid in SUBSCRIPTIONS if _is_active(uid))
    total_invited = sum(INVITED_COUNT.values())
    total_promo_activations = sum(len(codes) for codes in PROMO_USED_BY.values())
    lines = [
        "📊 Статистика MemTrace",
        f"Всего пользователей: {total_users}",
        f"Активных подписок: {active_users}",
        f"Суммарно приглашений: {total_invited}",
        f"Активаций промокодов: {total_promo_activations}",
        f"Промокодов заведено: {len(PROMO_CODES)}",
        f"Администраторов: {len(ADMIN_IDS)}",
    ]
    await update.message.reply_text("\n".join(lines))


async def addpromo_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update):
        return
    if len(context.args) < 3:
        await update.message.reply_text("Использование: /addpromo <КОД> <дни> <лимит_активаций>")
        return
    code = context.args[0].strip().upper()[:32]
    try:
        bonus_days = int(context.args[1])
        max_uses = int(context.args[2])
    except ValueError:
        await update.message.reply_text("Дни и лимит активаций должны быть числами.")
        return
    PROMO_CODES[code] = {"bonus_days": bonus_days, "max_uses": max_uses, "used": 0}
    _persist()
    await update.message.reply_text(f"✅ Промокод {code}: +{bonus_days} дн., лимит {max_uses} активаций.")


async def delpromo_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update):
        return
    if not context.args:
        await update.message.reply_text("Использование: /delpromo <КОД>")
        return
    code = context.args[0].strip().upper()
    if PROMO_CODES.pop(code, None) is None:
        await update.message.reply_text("Такого промокода нет.")
        return
    _persist()
    await update.message.reply_text(f"✅ Промокод {code} удалён.")


async def listpromo_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update):
        return
    if not PROMO_CODES:
        await update.message.reply_text("Промокодов пока нет.")
        return
    lines = ["🎟 Промокоды:"]
    for code, data in PROMO_CODES.items():
        lines.append(f"{code} — +{data['bonus_days']} дн., {data['used']}/{data['max_uses']} активаций")
    await update.message.reply_text("\n".join(lines))


async def addadmin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update):
        return
    if not context.args:
        await update.message.reply_text("Использование: /addadmin <user_id>")
        return
    target_id = _parse_user_id(context.args[0])
    if target_id is None:
        await update.message.reply_text("user_id должен быть числом.")
        return
    ADMIN_IDS.add(target_id)
    _persist()
    await update.message.reply_text(f"✅ {target_id} теперь администратор.")


async def deladmin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update):
        return
    if not context.args:
        await update.message.reply_text("Использование: /deladmin <user_id>")
        return
    target_id = _parse_user_id(context.args[0])
    if target_id is None:
        await update.message.reply_text("user_id должен быть числом.")
        return
    user = update.effective_user
    if target_id == user.id:
        await update.message.reply_text("Нельзя забрать доступ у самого себя этой командой.")
        return
    ADMIN_IDS.discard(target_id)
    _persist()
    await update.message.reply_text(f"✅ {target_id} больше не администратор.")


async def admins_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update):
        return
    if not ADMIN_IDS:
        await update.message.reply_text("Список администраторов пуст.")
        return
    await update.message.reply_text("Администраторы:\n" + "\n".join(str(uid) for uid in sorted(ADMIN_IDS)))


# ---------------------------------------------------------------------------
# Chat Automation: обработка business-апдейтов
# ---------------------------------------------------------------------------

async def handle_business_connection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    bc = update.business_connection
    if bc is None:
        return
    logger.info(
        "Business connection %s: user=%s can_reply=%s is_enabled=%s",
        bc.id, bc.user.id, bc.can_reply, bc.is_enabled,
    )
    if bc.is_enabled:
        # Запоминаем, какому клиенту принадлежит это business-подключение --
        # без этого некому будет адресовать уведомления об удалении/изменении.
        BUSINESS_CONNECTION_OWNER[bc.id] = bc.user.id
    else:
        # Клиент отключил бота от Chat Automation -- прекращаем слать ему апдейты.
        BUSINESS_CONNECTION_OWNER.pop(bc.id, None)
    _persist()


# Тип медиа в сообщении -> имя соответствующего метода Bot API (send_photo,
# send_video, ...) и атрибут update.message, где лежит его file_id.
_MEDIA_ATTRS = ("photo", "video", "voice", "video_note", "animation", "sticker", "document", "audio")


def _extract_media(msg) -> tuple[str | None, str | None]:
    """Возвращает (тип_медиа, file_id) первого найденного вложения в сообщении."""
    for attr in _MEDIA_ATTRS:
        value = getattr(msg, attr, None)
        if value:
            file_id = value[-1].file_id if attr == "photo" else value.file_id
            return attr, file_id
    return None, None


def _esc(value) -> str:
    """Экранирует текст для безопасной вставки в сообщение с parse_mode=HTML."""
    return html.escape(str(value), quote=False)


def _format_actor(name: str, username: str | None) -> str:
    """'Имя (@username)' или просто 'Имя', если username нет -- для подписи в уведомлении."""
    label = _esc(name or "неизвестно")
    if username:
        label += f" (@{_esc(username)})"
    return label


def _cache_message(msg) -> dict:
    media_type, file_id = _extract_media(msg)
    entry = {
        "text": msg.text or msg.caption,
        "media_type": media_type,
        "file_id": file_id,
        "date": msg.date,
        "sender": msg.from_user.full_name if msg.from_user else "неизвестно",
        "sender_username": msg.from_user.username if msg.from_user else None,
        "sender_id": msg.from_user.id if msg.from_user else None,
    }
    key = (msg.business_connection_id, msg.chat_id, msg.message_id)
    MESSAGE_CACHE[key] = entry
    return entry


async def handle_business_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.business_message
    if msg is None:
        return
    # Полностью пустые служебные апдейты (смена фото чата и т.п.) не кэшируем.
    if msg.text is None and msg.caption is None and _extract_media(msg)[0] is None:
        return
    _cache_message(msg)


async def handle_edited_business_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.edited_business_message
    if msg is None:
        return

    owner_user_id, owner_chat_id = await _resolve_owner(context, msg.business_connection_id)

    # Если владелец редактирует СВОЁ ЖЕ сообщение -- ему незачем присылать
    # уведомление об этом, он и так знает, что сам его поменял.
    is_owner_action = owner_user_id is not None and msg.from_user and msg.from_user.id == owner_user_id

    key = (msg.business_connection_id, msg.chat_id, msg.message_id)
    old = MESSAGE_CACHE.get(key)
    old_text = _esc(old["text"]) if old and old.get("text") else "(неизвестно, не было в кэше)"
    new_text = _esc(msg.text or msg.caption or "(без текста)")

    if owner_chat_id and not is_owner_action:
        actor = _format_actor(
            msg.from_user.full_name if msg.from_user else "неизвестно",
            msg.from_user.username if msg.from_user else None,
        )
        await context.bot.send_message(
            chat_id=owner_chat_id,
            text=(
                f"✏️ <b>{actor}</b> изменил(а) сообщение:\n\n"
                f"Было:\n<blockquote>{old_text}</blockquote>\n"
                f"Стало:\n<blockquote>{new_text}</blockquote>"
            ),
            parse_mode="HTML",
        )

    _cache_message(msg)


async def _send_cached_media(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, entry: dict, caption: str, parse_mode: str | None = None,
) -> bool:
    """Пытается переслать владельцу сохранённое медиа (фото/видео/войс/...).

    Возвращает True, если получилось. False -- если медиа не было или
    file_id уже недействителен (Telegram иногда инвалидирует file_id
    удалённых сообщений), тогда вызывающий код отправит текстовый фолбэк.
    """
    media_type, file_id = entry.get("media_type"), entry.get("file_id")
    if not media_type or not file_id:
        return False
    send_method = getattr(context.bot, f"send_{media_type}", None)
    if send_method is None:
        return False
    try:
        kwargs = {"chat_id": chat_id, media_type: file_id, "caption": caption, "parse_mode": parse_mode}
        if media_type == "sticker":
            kwargs.pop("caption", None)  # send_sticker не принимает caption
            kwargs.pop("parse_mode", None)
        await send_method(**kwargs)
        return True
    except Exception as exc:
        logger.warning("Не удалось переслать %s по сохранённому file_id: %s", media_type, exc)
        return False


async def handle_deleted_business_messages(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    deleted = update.deleted_business_messages
    if deleted is None:
        return

    owner_user_id, owner_chat_id = await _resolve_owner(context, deleted.business_connection_id)
    if not owner_chat_id:
        return

    for message_id in deleted.message_ids:
        key = (deleted.business_connection_id, deleted.chat.id, message_id)
        old = MESSAGE_CACHE.pop(key, None)

        # Если удалённое сообщение отправлял сам владелец -- не уведомляем
        # его о том, что он же его и удалил.
        if old and owner_user_id is not None and old.get("sender_id") == owner_user_id:
            continue

        text = _esc(old["text"]) if old and old.get("text") else "(текст не найден в кэше)"
        actor = _format_actor((old or {}).get("sender", "неизвестно"), (old or {}).get("sender_username"))
        caption = f"🗑 <b>{actor}</b> удалил(а) сообщение:\n\n<blockquote>{text}</blockquote>"

        sent_as_media = False
        if old:
            sent_as_media = await _send_cached_media(
                context, owner_chat_id, old, caption=caption, parse_mode="HTML",
            )
        if not sent_as_media:
            await context.bot.send_message(chat_id=owner_chat_id, text=caption, parse_mode="HTML")


async def _resolve_owner(context: ContextTypes.DEFAULT_TYPE, business_connection_id: str):
    """(owner_user_id, personal_chat_id) владельца конкретного business-подключения.

    Смотрит business_connection_id -> user_id (BUSINESS_CONNECTION_OWNER).
    Если запись не найдена в памяти (например, подключение было создано
    ДО того, как этот процесс бота запустился -- Telegram шлёт апдейт
    business_connection только один раз, при создании/изменении подключения,
    а не при каждом рестарте бота) -- добираем владельца на лету через
    getBusinessConnection(business_connection_id) и кэшируем результат,
    чтобы больше не ходить в API за тем же ID.
    """
    owner_user_id = BUSINESS_CONNECTION_OWNER.get(business_connection_id)

    if owner_user_id is None:
        try:
            bc = await context.bot.get_business_connection(business_connection_id)
            owner_user_id = bc.user.id
            BUSINESS_CONNECTION_OWNER[business_connection_id] = owner_user_id
            _persist()
            logger.info(
                "Владелец business_connection_id=%s восстановлен через getBusinessConnection: user=%s",
                business_connection_id, owner_user_id,
            )
        except Exception as exc:
            logger.warning(
                "Не удалось получить владельца business_connection_id=%s даже через API (%s) "
                "-- уведомление не отправлено.",
                business_connection_id, exc,
            )
            return None, None

    chat_id = OWNER_CHATS.get(owner_user_id)
    if chat_id is None:
        logger.warning(
            "У владельца %s нет сохранённого chat_id (не нажимал /start?) -- уведомление не отправлено.",
            owner_user_id,
        )
    return owner_user_id, chat_id


async def _post_init(application) -> None:
    """Настраивает кнопку «Menu» внизу слева со списком команд (RU и EN)."""
    await application.bot.set_my_commands(BOT_COMMANDS_RU, scope=BotCommandScopeDefault())
    await application.bot.set_my_commands(BOT_COMMANDS_RU, language_code="ru")
    await application.bot.set_my_commands(BOT_COMMANDS_EN, language_code="en")
    await application.bot.set_chat_menu_button(menu_button=MenuButtonCommands())


async def _post_shutdown(application) -> None:
    """Сохраняем состояние на выключении (деплой/рестарт/Ctrl+C)."""
    _persist()
    logger.info("Состояние сохранено, бот остановлен.")


async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Глобальный обработчик ошибок: чтобы одно необработанное исключение
    в хендлере не роняло весь процесс бота, а просто попадало в лог."""
    err_text = str(context.error)
    if "Conflict" in err_text and "getUpdates" in err_text:
        logger.critical(
            "КОНФЛИКТ ПОЛЛИНГА: ещё один процесс (или устройство) сейчас опрашивает "
            "Telegram с ТЕМ ЖЕ ТОКЕНОМ. Пока это не устранено, апдейты будут "
            "хаотично доставаться то одному процессу, то другому -- отсюда "
            "'часть функций как будто не работает'. Если токен когда-либо "
            "светился в коде/чате/репозитории -- считай его скомпрометированным, "
            "перевыпусти через @BotFather -> Revoke API Token и убедись, что "
            "запущена только ОДНА копия бота с новым токеном."
        )
        return
    logger.error("Необработанная ошибка при обработке апдейта %s", update, exc_info=context.error)


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError(
            "Не задан токен бота. Установи переменную окружения TELEGRAM_BOT_TOKEN "
            "(например: export TELEGRAM_BOT_TOKEN=... или через .env на хостинге)."
        )

    storage.init_db()
    _restore_state()

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )
    app.add_error_handler(_on_error)

    # Автосохранение раз в 5 минут -- подстраховка на случай, если процесс
    # убьют без штатного shutdown (OOM-killer, `kill -9` и т.п.).
    if app.job_queue is not None:
        app.job_queue.run_repeating(_autosave_job, interval=300, first=300)
    else:
        logger.warning(
            "JobQueue недоступен (не установлен extras 'job-queue') -- "
            "автосохранение по таймеру отключено, состояние сохраняется "
            "только по событиям и при штатном завершении. "
            "Установи: pip install \"python-telegram-bot[job-queue]\""
        )

    # Отдельная группа (-1): гасит ожидание промокода, если вместо кода
    # пользователь ввёл любую команду. Выполняется до основных хендлеров ниже.
    app.add_handler(MessageHandler(filters.COMMAND, _clear_promo_wait_on_command), group=-1)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("invite", invite_command))
    app.add_handler(CommandHandler("promo", promo_command))
    app.add_handler(CommandHandler("top", top_command))
    app.add_handler(CommandHandler("lang", lang_command))

    # --- Админ-панель (доступна только ADMIN_IDS) ---
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CommandHandler("grant", grant_command))
    app.add_handler(CommandHandler("revoke", revoke_command))
    app.add_handler(CommandHandler("finduser", finduser_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("addpromo", addpromo_command))
    app.add_handler(CommandHandler("delpromo", delpromo_command))
    app.add_handler(CommandHandler("listpromo", listpromo_command))
    app.add_handler(CommandHandler("addadmin", addadmin_command))
    app.add_handler(CommandHandler("deladmin", deladmin_command))
    app.add_handler(CommandHandler("admins", admins_command))

    onboarding_actions = {
        "check_subscription", "lang_ru", "lang_en", "policy_accept", "policy_decline", "policy_full",
    }
    app.add_handler(
        CallbackQueryHandler(
            onboarding_callback_handler,
            pattern="^(" + "|".join(onboarding_actions) + ")$",
        )
    )
    app.add_handler(CallbackQueryHandler(menu_button_handler))

    # Ввод промокода текстом после нажатия кнопки "Промокод"
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, promo_code_text_handler))

    # Оплата через Telegram Stars
    app.add_handler(PreCheckoutQueryHandler(precheckout_callback))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_handler))

    # Обрабатываем все апдейты и внутри сами проверяем нужные поля --
    # так надёжнее, если библиотека ещё не завела отдельные фильтры
    # под самые новые типы апдейтов.
    async def dispatch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        # ВРЕМЕННЫЙ диагностический лог: печатает тип КАЖДОГО входящего апдейта.
        # Если в логах хостинга ни разу не появится business_connection /
        # business_message / edited_business_message / deleted_business_messages --
        # значит Telegram их вообще не шлёт (дело не в этом коде, а в Secretary
        # Mode / аккаунте / утёкшем токене). Когда разберётесь -- можно убрать.
        logger.info("RAW UPDATE %s: %s", update.update_id, list(update.to_dict().keys()))

        if update.business_connection is not None:
            await handle_business_connection(update, context)
        elif update.business_message is not None:
            await handle_business_message(update, context)
        elif update.edited_business_message is not None:
            await handle_edited_business_message(update, context)
        elif update.deleted_business_messages is not None:
            await handle_deleted_business_messages(update, context)

    app.add_handler(TypeHandler(Update, dispatch), group=1)

    logger.info("Бот запущен...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
