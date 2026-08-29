"""
Оркестрация одного прохода мониторинга: загрузка выдачи → diff с базой → запись.

Логика досрочного останова: выдача Avito отсортирована по дате (свежие сверху),
поэтому если целая страница состоит только из уже известных id — новых
объявлений на следующих страницах не будет, их не запрашиваем.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from avito_parser import AvitoParser, Listing
from db import Database

logger = logging.getLogger("avito_parser.watcher")

# Тип хука уведомлений: получает список новых объявлений после записи в базу.
OnNewListings = Callable[[list[Listing]], None]


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class RunResult:
    status: str  # ok | blocked | error | dry-run-ok
    pages_fetched: int = 0
    seen_total: int = 0
    new_count: int = 0
    message: str = ""


def _listing_int_id(listing: Listing) -> int | None:
    """Числовой id объявления; без него карточку нельзя надёжно дедуплицировать."""
    if listing.id and listing.id.isdigit():
        return int(listing.id)
    return None


def run_once(
    parser: AvitoParser,
    db: Database,
    search_url: str,
    max_pages: int = 3,
    dry_run: bool = False,
    via_api: bool = True,
    consider_ended_after_hours: float = 48.0,
    on_new_listings: OnNewListings | None = None,
) -> RunResult:
    """
    Один проход мониторинга.

    via_api=True: выдача берётся из внутреннего JSON-эндпоинта (единственный
    способ получить фильтры вида f=ASgB...); False — парсинг SSR HTML.
    dry_run=True: полный проход с diff'ом против базы, но без записи в неё.
    consider_ended_after_hours: лот, не виденный дольше этого порога,
      помечается ушедшим (status='ended').
    on_new_listings: хук вызывается один раз после записи новых объявлений;
      исключения хука логируются, но не валят мониторинг.
    """
    started_at = _utcnow_iso()
    result = RunResult(status="ok")
    known = db.known_ids()

    try:
        new_listings: list[Listing] = []
        touched_ids: set[int] = set()
        seen_ids_this_run: set[int] = set()

        for page in range(1, max_pages + 1):
            logger.info("Мониторинг: страница %s из %s", page, max_pages)
            if via_api:
                listings = parser.get_api_items(search_url, page=page)
            else:
                params = {"p": page} if page > 1 else None
                html = parser.get_html(search_url, params=params)
                listings = list(parser.parse_search_page(html))
            result.pages_fetched += 1
            if not listings:
                logger.info("Страница %s без объявлений — выдача кончилась.", page)
                break

            page_ids: set[int] = set()
            for item in listings:
                item_id = _listing_int_id(item)
                if item_id is None:
                    logger.warning("Карточка без числового id, пропускаю: %r", item.title)
                    continue
                if item_id in seen_ids_this_run:
                    continue  # дубль внутри выдачи
                seen_ids_this_run.add(item_id)
                page_ids.add(item_id)
                if item_id in known:
                    touched_ids.add(item_id)
                else:
                    new_listings.append(item)

            # Все id страницы уже известны → дальше свежих нет.
            if page_ids and page_ids <= known:
                logger.info("Страница %s целиком из известных объявлений — стоп.", page)
                break

            if page < max_pages:
                parser.pause()

        result.seen_total = len(seen_ids_this_run)
        result.new_count = len(new_listings)

        if dry_run:
            result.status = "dry-run-ok"
            logger.info(
                "DRY-RUN: увидено %s, новых было бы %s (записей в базу нет).",
                result.seen_total,
                result.new_count,
            )
        else:
            ts = _utcnow_iso()
            db.apply_diff(new_listings, touched_ids, ts)
            ended = db.mark_ended(consider_ended_after_hours, ts)
            logger.info(
                "Записано новых: %s, всего увидено за проход: %s, помечено ушедших: %s.",
                result.new_count,
                result.seen_total,
                ended,
            )

            if new_listings and on_new_listings is not None:
                try:
                    on_new_listings(new_listings)
                except Exception:  # noqa: BLE001
                    logger.exception("Хук уведомлений упал (не критично)")

    except RuntimeError as exc:
        # Исчерпание ретраев в _get. Различаем блокировку и прочие ошибки,
        # чтобы журнал runs показывал, когда пора обновлять cookies.txt.
        msg = str(exc)
        if "Заблокировано" in msg or "блокировку" in msg:
            result.status = "blocked"
            result.message = f"{msg} Похоже, куки устарели или IP ограничен — обновите cookies.txt."
        else:
            result.status = "error"
            result.message = msg
        logger.error("Проход завершился ошибкой (%s): %s", result.status, result.message)

    if not dry_run:
        db.log_run(
            started_at=started_at,
            status=result.status,
            pages_fetched=result.pages_fetched,
            seen_total=result.seen_total,
            new_count=result.new_count,
            message=result.message,
        )

    return result
