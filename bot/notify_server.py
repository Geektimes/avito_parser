"""
HTTP-эндпоинт /notify для приёма сообщений от avito-парсера.

Парсер шлёт POST {"text": "..."}; бот рассылает текст всем ADMIN_IDS
через Bot API. Порт наружу не пробрасывается, слушает только docker-сеть.
"""

from __future__ import annotations

import logging
from typing import Iterable

from aiohttp import web
from aiogram import Bot

logger = logging.getLogger("avito_bot.notify")

MAX_TEXT_LEN = 4000  # телеграм-лимит 4096, с запасом


async def handle_notify(request: web.Request) -> web.Response:
    bot: Bot = request.app["bot"]
    admin_ids: list[int] = request.app["admin_ids"]

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "bad json"}, status=400)

    text = str(data.get("text") or "").strip()
    if not text:
        return web.json_response({"error": "empty text"}, status=400)
    if len(text) > MAX_TEXT_LEN:
        text = text[:MAX_TEXT_LEN]

    sent_to: list[int] = []
    failed: list[int] = []
    for chat_id in admin_ids:
        try:
            await bot.send_message(chat_id, text, disable_web_page_preview=True)
            sent_to.append(chat_id)
        except Exception:
            logger.exception("Не доставлено %s", chat_id)
            failed.append(chat_id)

    if not sent_to and failed:
        return web.json_response(
            {"error": "delivery failed", "failed": failed}, status=502
        )
    return web.json_response({"sent_to": sent_to, "failed": failed})


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "admins": len(request.app["admin_ids"])})


def build_app(bot: Bot, admin_ids: Iterable[int]) -> web.Application:
    app = web.Application()
    app["bot"] = bot
    app["admin_ids"] = list(admin_ids)
    if not app["admin_ids"]:
        raise ValueError("ADMIN_IDS пуст — некому слать уведомления")
    app.router.add_post("/notify", handle_notify)
    app.router.add_get("/health", handle_health)
    return app
