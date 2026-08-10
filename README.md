# MemTrace — деплой

## 1. Установка

```bash
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

## 2. Токен бота

**Никогда не вписывай токен в код.** Только через переменную окружения:

```bash
export TELEGRAM_BOT_TOKEN="твой_токен_от_BotFather"
```

Или создай `.env` рядом со скриптом (см. `.env.example`) и подгружай его
средствами своего хостинга/systemd/Docker.

> ⚠️ Если токен из старой версии этого проекта (`8892317832:AAHj...`) когда-либо
> лежал в коде, который ты кому-то показывал/заливал в репозиторий — считай
> его скомпрометированным и перевыпусти через @BotFather → `/revoke`.

## 3. Файлы, которые должны лежать рядом со скриптом

```
memtrace/
├── chat_automation_bot.py
├── image_gen.py
├── storage.py
├── requirements.txt
├── main_menu.png
└── assets/
    ├── header.png
    └── fonts/
        ├── DejaVuSans.ttf
        └── DejaVuSans-Bold.ttf
```

База данных `memtrace.sqlite3` создастся автоматически при первом запуске
рядом со скриптом (путь можно переопределить переменной `MEMTRACE_DB_PATH`).

## 4. Настройка в @BotFather

1. `/mybots` → выбрать бота → **Bot Settings → Secretary Mode → Enable**
   (это отдельная настройка, без неё Chat Automation не заработает).
2. Убедиться, что у бота включены платежи Stars (обычно включены по
   умолчанию для новых ботов).

## 5. Запуск

```bash
python chat_automation_bot.py
```

Бот работает через long polling — отдельный вебхук/HTTPS-сертификат не нужен,
достаточно, чтобы процесс был постоянно запущен на сервере.

### Как держать процесс живым на хостинге

**Вариант А — systemd (VPS)**

```ini
# /etc/systemd/system/memtrace.service
[Unit]
Description=MemTrace Telegram Bot
After=network.target

[Service]
WorkingDirectory=/opt/memtrace
EnvironmentFile=/opt/memtrace/.env
ExecStart=/opt/memtrace/venv/bin/python chat_automation_bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now memtrace
journalctl -u memtrace -f     # логи
```

**Вариант B — Docker**

```bash
docker build -t memtrace .
docker run -d --name memtrace \
  --env-file .env \
  -v $(pwd)/data:/app/data \
  -e MEMTRACE_DB_PATH=/app/data/memtrace.sqlite3 \
  --restart unless-stopped \
  memtrace
```

(См. `Dockerfile` рядом.)

## 6. Что сохраняется между рестартами

Подписки, рефералы, промокоды, язык, согласие с политикой и привязка
business-подключений к владельцам — в `memtrace.sqlite3` (модуль `storage.py`).
Сохранение происходит сразу после важных действий (оплата, промокод,
реферал, /start) плюс каждые 5 минут как подстраховка, и один раз при
штатном завершении процесса.

Кэш последних сообщений (`MESSAGE_CACHE`, нужен только чтобы показать
«было / стало» при правке) **намеренно не сохраняется** — это ожидаемо
временные данные, которые не критично терять при рестарте.

## 7. Перед тем как продавать — обязательно проверь сам

- [ ] Токен бота НЕ хранится в git-репозитории (`.gitignore` → `.env`).
- [ ] `REQUIRED_CHANNEL` / `REQUIRED_CHANNEL_URL` в коде указывают на твой
      реальный канал, а не на `@cachedmemory` из демо.
- [ ] Текст политики конфиденциальности (`PRIVACY_POLICY_FULL_TEXT` в
      `chat_automation_bot.py`) — это шаблон. Замени контакт поддержки
      в разделе 5 на реальный и, если продаёшь платно, дай юристу
      свериться с законами твоей юрисдикции (GDPR/152-ФЗ и т.п.).
- [ ] `STARS_PRICE` и `PROMO_CODES` — актуальные цифры для запуска.
- [ ] Протестировал онбординг с чистого пользователя (новый Telegram-аккаунт
      или `/start` после `POLICY_ACCEPTED`/`USER_LANG` пустых).
- [ ] Протестировал Chat Automation минимум на двух РАЗНЫХ Telegram-аккаунтах
      одновременно — чтобы убедиться, что уведомления не путаются между
      клиентами (это чинили — см. `BUSINESS_CONNECTION_OWNER` в коде).

## 8. Известные ограничения (осознанно не в MVP)

- Приём одноразовых (view-once) фото/видео через reply — заявлено в
  тексте `/help`, но не реализовано в коде. Поведение view-once в Bot API
  для business-чатов нужно тестировать на живом аккаунте перед тем, как
  писать реализацию.
- `MESSAGE_CACHE` не имеет верхнего предела размера — при очень большом
  потоке сообщений имеет смысл добавить TTL/эвикцию по времени.
- SQLite достаточно для одного процесса/сервера. Если вырастешь до
  нескольких инстансов бота за балансировщиком — переезжай на Postgres.
