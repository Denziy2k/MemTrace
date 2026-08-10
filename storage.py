"""
storage.py — простое сохранение состояния бота (SQLite) между рестартами.

Без этого модуля весь прогресс (кто на какой подписке, кто кого пригласил,
кто какой промокод активировал, кто на каком языке) живёт только в памяти
процесса и обнуляется при каждом деплое/рестарте -- недопустимо для боевого
запуска, где люди реально платят Stars за подписку.

Дизайн специально простой (не ORM, не миграции): один файл SQLite,
одна таблица key-value, в каждой строке -- JSON-снимок одного из
глобальных словарей бота. Этого достаточно для нагрузки одного бота
на одном процессе; если проект вырастет до нескольких инстансов/шардов,
стоит переезжать на Postgres.

Использование (см. интеграцию в chat_automation_bot.py):
    storage.init_db()
    state = storage.load_all()      # при старте
    ...
    storage.save_all(current_state)  # периодически и после важных изменений
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

DB_PATH = os.environ.get("MEMTRACE_DB_PATH", "memtrace.sqlite3")

# Все ключи состояния, которые умеет сохранять/восстанавливать этот модуль.
STATE_KEYS = (
    "subscriptions",            # {user_id: iso-дата истечения}
    "invited_count",            # {user_id: int}
    "referred_by",               # {user_id: referrer_id}
    "user_names",                 # {user_id: имя}
    "user_usernames",            # {user_id: username без @}
    "owner_chats",                # {user_id: personal chat_id}
    "business_connection_owner",  # {business_connection_id: user_id}
    "user_lang",                  # {user_id: "ru"/"en"}
    "policy_accepted",            # {user_id: bool}
    "promo_used_by",              # {user_id: [codes...]}
    "promo_codes_used",           # {code: сколько раз уже использован}
    "promo_codes_meta",           # {code: {"bonus_days": int, "max_uses": int}} -- сами коды, не только счётчик
    "admin_ids",                   # [user_id, ...] -- кому разрешена админ-панель, помимо ADMIN_USER_IDS из env
)

_lock = threading.Lock()  # sqlite3-соединение из разных корутин по очереди


def init_db(path: str | None = None) -> None:
    global DB_PATH
    if path:
        DB_PATH = path
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.commit()
    logger.info("Хранилище состояния: %s", os.path.abspath(DB_PATH))


def load_all() -> dict:
    """Возвращает {ключ: dict}. Отсутствующие в БД ключи -- пустые dict."""
    result = {key: {} for key in STATE_KEYS}
    try:
        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute("SELECT key, value FROM state").fetchall()
        for key, value in rows:
            if key in result:
                try:
                    result[key] = json.loads(value)
                except json.JSONDecodeError:
                    logger.warning("Битый JSON в storage для ключа %s, использую пустой словарь.", key)
    except sqlite3.Error as exc:
        logger.warning("Не удалось прочитать storage (%s), стартую с чистого состояния.", exc)
    return result


def save_all(state: dict) -> None:
    """Перезаписывает все переданные ключи одной транзакцией."""
    with _lock:
        try:
            with sqlite3.connect(DB_PATH) as conn:
                for key in STATE_KEYS:
                    if key not in state:
                        continue
                    conn.execute(
                        "INSERT INTO state (key, value) VALUES (?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                        (key, json.dumps(state[key])),
                    )
                conn.commit()
        except sqlite3.Error as exc:
            logger.error("Не удалось сохранить состояние в storage: %s", exc)


# ---------------------------------------------------------------------------
# Хелперы сериализации: datetime <-> ISO-строка, int-ключи <-> строки
# (JSON не умеет ни то, ни другое напрямую).
# ---------------------------------------------------------------------------

def dt_to_str(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def str_to_dt(s: str) -> datetime:
    return datetime.fromisoformat(s)
