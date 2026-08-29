"""
Avito-бот уведомлений (@Avito_notebot).

Два сервиса в одном процессе:
1. Telegram long polling через aiogram — команды /start, /help, /id, /stats.
2. aiohttp-сервер на 0.0.0.0:8080 — POST /notify от avito-парсера.

Конфиг через env (см. .env.example):
  BOT_TOKEN   — токен от @BotFather
  ADMIN_IDS   — список chat_id через запятую, кому слать уведомления
  DB_PATH     — путь к SQLite парсера (для /stats), по умолчанию data/avito.db
  NOTIFY_PORT — порт aiohttp, по умолчанию 8080
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

from aiohttp import web
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command

from db_reader import format_stats, read_recent_runs, read_stats
from notify_server import build_app

logger = logging.getLogger("avito_bot")


def _admin_ids() -> list[int]:
    raw = os.getenv("ADMIN_IDS", "")
    ids: list[int] = []
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            ids.append(int(part))
    return ids


def _setup_logging() -> None:
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )


def _build_dispatcher(bot: Bot) -> Dispatcher:
    dp = Dispatcher()
    db_path = Path(os.getenv("DB_PATH", "data/avito.db"))

    @dp.message(Command("start"))
    async def cmd_start(message: types.Message) -> None:
        await message.answer(
            "👋 <b>Avito-бот уведомлений</b>\n\n"
            "Сюда приходят новые объявления из avito-парсера.\n"
            "Команды: /help, /id, /stats",
            parse_mode="HTML",
        )

    @dp.message(Command("help"))
    async def cmd_help(message: types.Message) -> None:
        await message.answer(
            "<b>Команды:</b>\n"
            "/start — приветствие\n"
            "/id — ваш chat_id (для ADMIN_IDS)\n"
            "/stats — сводка по базе парсера\n"
            "/health — состояние бота\n\n"
            "Уведомления приходят автоматически при появлении "
            "новых объявлений, прошедших фильтр локации.",
            parse_mode="HTML",
        )

    @dp.message(Command("id"))
    async def cmd_id(message: types.Message) -> None:
        await message.answer(f"chat_id: <code>{message.chat.id}</code>", parse_mode="HTML")

    @dp.message(Command("health"))
    async def cmd_health(message: types.Message) -> None:
        await message.answer("✅ бот работает")

    @dp.message(Command("stats"))
    async def cmd_stats(message: types.Message) -> None:
        stats = read_stats(db_path)
        if stats is None:
            await message.answer(
                f"База {db_path} не найдена — парсер ещё ни разу не проходил."
            )
            return
        recent = read_recent_runs(db_path, limit=5)
        await message.answer(format_stats(stats, recent), parse_mode="HTML")

    return dp


async def main() -> None:
    _setup_logging()

    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("BOT_TOKEN не задан")

    admin_ids = _admin_ids()
    if not admin_ids:
        raise RuntimeError("ADMIN_IDS пуст — некому слать уведомления")

    bot = Bot(token=token, parse_mode=None)
    dp = _build_dispatcher(bot)

    notify_app = build_app(bot, admin_ids)
    runner = web.AppRunner(notify_app)
    await runner.setup()

    port = int(os.getenv("NOTIFY_PORT", "8080"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(
        "Notify API на 0.0.0.0:%s, ADMIN_IDS=%s", port, admin_ids
    )

    logger.info("Старт long polling…")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Остановлено.")
