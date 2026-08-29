"""
Доставка уведомлений о новых объявлениях.

Транспорт один — HTTP-мост до avito-bot (long polling + aiohttp внутри одной
docker-сети). Парсер шлёт POST {"text": ...} на /notify, бот рассылает
сообщение всем ADMIN_IDS через Bot API (@Avito_notebot).

Конфиг monitor.ini:
    [notify]
    url = http://avito-bot:8080          ; мост до бота
    location_filter = Волгоград, Волгоградская область

Пачки по ≤10 лотов, между сообщениями пауза 1 сек.
"""

from __future__ import annotations

import configparser
import json
import logging
import time
import urllib.request
from pathlib import Path

from avito_parser import Listing

logger = logging.getLogger("avito_parser.notify")

BATCH_SIZE = 10
PAUSE_BETWEEN_BATCHES_SEC = 1.0


def load_config(path: str | Path = "monitor.ini") -> dict:
    """Читает monitor.ini; отсутствие файла или секций = уведомления выключены."""
    cfg_path = Path(path)
    if not cfg_path.exists():
        logger.info("%s не найден — уведомления отключены.", cfg_path)
        return {}
    cp = configparser.ConfigParser()
    cp.read(cfg_path, encoding="utf-8")
    return {
        "url": cp.get("notify", "url", fallback="").strip(),
        "location_filter": cp.get("notify", "location_filter", fallback="").strip(),
    }


def make_notifier(config: dict):
    """
    Фабрика хука для run_once(on_new_listings=...).
    Если url не задан, возвращает None (уведомления выключены).
    """
    bridge_url = config.get("url") or ""
    if not bridge_url:
        return None

    def notify(new_listings: list[Listing]) -> None:
        selected = _filter_by_location(
            new_listings, str(config.get("location_filter") or "")
        )
        if not selected:
            logger.info("Уведомления: после фильтра локации не осталось лотов.")
            return

        batches = [
            selected[i : i + BATCH_SIZE] for i in range(0, len(selected), BATCH_SIZE)
        ]
        logger.info(
            "Уведомление: %s из %s новых проходят фильтр «%s», пачек: %s.",
            len(selected),
            len(new_listings),
            config.get("location_filter") or "(без фильтра)",
            len(batches),
        )
        for batch in batches:
            text = _format_batch(batch)
            _send_via_bridge(bridge_url, text)
            time.sleep(PAUSE_BETWEEN_BATCHES_SEC)

    return notify


def _filter_by_location(listings: list[Listing], location_filter: str) -> list[Listing]:
    """Фильтр по локации: список вариантов через запятую, логика «ИЛИ»."""
    variants = [v.strip().lower() for v in location_filter.split(",") if v.strip()]
    if not variants:
        return list(listings)
    matched = []
    for l in listings:
        loc = (l.location or "").lower()
        if not loc:
            continue
        if any(v in loc for v in variants):
            matched.append(l)
    return matched


def _format_batch(batch: list[Listing]) -> str:
    lines = [f"🔔 Новые объявления ({len(batch)}):"]
    for l in batch:
        price = f" — {l.price_raw:,}".replace(",", " ") + " ₽" if l.price_raw else ""
        loc = f" ({l.location})" if l.location else ""
        lines.append(f"\n{l.title}{price}{loc}\n{l.url}")
    return "\n".join(lines)


def _send_via_bridge(base_url: str, text: str) -> None:
    """POST {"text": ...} на /notify avito-бота."""
    url = base_url.rstrip("/") + "/notify"
    payload = json.dumps({"text": text}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            body = r.read().decode("utf-8", "replace")
            if r.status != 200:
                logger.warning("Бот ответил %s: %s", r.status, body[:200])
    except Exception:
        logger.exception("Не удалось отправить уведомление через мост %s", url)
