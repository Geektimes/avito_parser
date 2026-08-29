# Avito Parser + Telegram-бот (curl_cffi + Docker/Ubuntu)

Парсер объявлений с Avito.ru + Telegram-бот уведомлений в одной docker-сети.

Для обхода TLS/HTTP2-фингерпринтинга парсер использует
[curl_cffi](https://github.com/lexiforest/curl_cffi) — имитирует отпечаток
реального браузера (Chrome/Safari), в отличие от `requests`/`httpx`,
которые антибот-системы легко отличают.

## Структура

```
avito/
├── app/                       # парсер
│   ├── avito_parser.py        # логика запросов и парсинга
│   ├── db.py                  # слой SQLite (WAL, миграции)
│   ├── main.py                # CLI
│   ├── notify_telegram.py     # POST /notify в бот
│   └── watcher.py             # один проход мониторинга
├── bot/                       # Telegram-бот уведомлений
│   ├── bot.py                 # long polling + aiohttp
│   ├── notify_server.py       # POST /notify
│   ├── db_reader.py           # read-only /stats
│   ├── Dockerfile
│   └── requirements.txt
├── data/                      # SQLite (volume)
├── output/                    # результаты (volume)
├── cookies.txt                # куки Avito (в .gitignore)
├── monitor.ini                # url + фильтр локации (в .gitignore)
├── .env                       # BOT_TOKEN, ADMIN_IDS (в .gitignore)
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## Быстрый старт

### 1. Подготовка
```bash
cp .env.example .env          # затем вписать BOT_TOKEN и ADMIN_IDS
cp monitor.ini.example monitor.ini
# положить cookies.txt (см. «Куки» ниже)
```

### 2. Сборка и запуск
```bash
docker compose build
docker compose up -d
```

Оба сервиса стартуют в сети `avito_net`:
- `avito-parser` — `--watch --interval 3600` (мониторинг по расписанию)
- `avito-bot` — long polling + aiohttp `/notify` на порту 8080

### 3. Проверка
```bash
docker compose logs -f avito-bot
docker exec avito-bot curl -s http://localhost:8080/health
# {"ok": true, "admins": 1}
```

В Telegram написать боту `/start`, `/stats`, `/id`.

### 4. Ручной POST в бота (smoke test)
```bash
docker exec avito-parser \
  python -c "import urllib.request,json; \
  urllib.request.urlopen(urllib.request.Request('http://avito-bot:8080/notify', \
  data=json.dumps({'text':'smoke test'}).encode(), \
  headers={'Content-Type':'application/json'}, method='POST'), timeout=10).read()"
```

## Параметры CLI (`app/main.py`)

| Флаг              | Описание                                                            |
|-------------------|----------------------------------------------------------------------|
| `--once`          | Режим мониторинга: один проход с записью в SQLite                   |
| `--watch`         | Режим мониторинга: встроенный цикл `--interval` сек                |
| `--interval`      | Пауза между проходами, сек (по умолчанию 3600)                      |
| `--dry-run`       | Полный проход без записи в базу                                      |
| `--url`           | URL выдачи Avito (фильтры внутри ссылки)                            |
| `--pages`         | Сколько страниц выдачи обойти за проход                             |
| `--output`        | Путь к результату (`.json` / `.csv`)                                |
| `--proxies-file`  | Файл со списком прокси                                              |
| `--min-delay` / `--max-delay` | Диапазон паузы между запросами, сек (по умолчанию 2–5)  |
| `--verbose`       | Подробный лог (DEBUG)                                                |
| `--jitter`        | Случайная задержка перед проходом, сек 0..N (анти-робот)            |
| `--stats`         | Сводка по базе и последние проходы, затем выход                     |

## Как работают уведомления

```
avito-parser ──HTTP POST /notify──► avito-bot ──Bot API──► @Avito_notebot ──► ADMIN_IDS
```

1. `app/notify_telegram.py` фильтрует новые лоты по `location_filter` и
   шлёт пачки по ≤10 штук `POST /notify` с JSON `{"text": "..."}`.
2. `bot/notify_server.py` принимает запрос и рассылает текст всем
   `ADMIN_IDS` через Bot API.
3. Команда `/stats` в боте читает SQLite парсера через общий volume `data/`.

## Куки

Avito агрессивно блокирует без авторизации. Скопируйте строку
`Cookie` из DevTools → Network → запрос к `avito.ru` → Request Headers →
`cookie`, сохраните в `cookies.txt` (UTF-8, в `.gitignore`).

## Прокси

Для стабильной работы при большом объёме страниц используйте пул прокси —
Avito банит IP при подозрительной активности. Формат `proxies.txt`:

```
http://user:pass@1.2.3.4:8000
http://user:pass@5.6.7.8:8000
```

Парсер случайным образом выбирает прокси и TLS-профиль при каждой новой
сессии/ретрае.

## Важные ограничения

1. **Верстка меняется.** Селекторы в `_parse_card()` (`app/avito_parser.py`)
   рассчитаны на текущую разметку карточек объявлений
   (`data-marker="item"` и т.п.). Если Avito обновит фронтенд, селекторы
   нужно будет поправить через DevTools.
2. **Антибот-защита.** Скрипт определяет капчу/блокировку по ключевым
   словам в HTML и по кодам ответа (403/429/503), после чего меняет
   TLS-профиль/прокси и повторяет попытку.
3. **Частота запросов.** Не уменьшайте `--min-delay`/`--max-delay`
   слишком сильно — резко увеличивает риск блокировки.
4. **Правовая сторона.** Автоматический сбор данных с Avito может
   противоречить пользовательскому соглашению. Используйте в разумных
   объёмах, не создавайте чрезмерную нагрузку.
