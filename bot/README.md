# avito-bot

Telegram-бот уведомлений для avito-парсера (@Avito_notebot).

## Что делает
- **Long polling** через aiogram: `/start`, `/help`, `/id`, `/stats`, `/health`.
- **HTTP-эндпоинт** `POST /notify` (aiohttp, порт 8080) — принимает
  `{"text": "..."}` от парсера и шлёт всем `ADMIN_IDS` через Bot API.
- **`/stats`** читает SQLite парсера в режиме read-only через общий volume
  `data/` (см. `db_reader.py`).

## Переменные окружения
| Имя            | Обязательно | По умолчанию       | Описание                                     |
|----------------|-------------|--------------------|----------------------------------------------|
| `BOT_TOKEN`    | да          | —                  | Токен от @BotFather                          |
| `ADMIN_IDS`    | да          | —                  | Список chat_id через запятую                 |
| `DB_PATH`      | нет         | `data/avito.db`    | Путь к SQLite парсера (для `/stats`)         |
| `NOTIFY_PORT`  | нет         | `8080`             | Порт aiohttp                                 |
| `LOG_LEVEL`    | нет         | `INFO`             | Уровень логов                                |

## Локальный запуск
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export BOT_TOKEN=...
export ADMIN_IDS=123456789
python bot.py
```

## API для парсера
```bash
curl -X POST http://localhost:8080/notify \
  -H "Content-Type: application/json" \
  -d '{"text":"🔔 Новое объявление: ..."}'
```

## Healthcheck
```bash
curl http://localhost:8080/health
# {"ok": true, "admins": 1}
```
