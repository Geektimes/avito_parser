"""
CLI для запуска парсера Avito.

Два режима:

1. Мониторинг (запись новых объявлений в SQLite):

    python main.py --once                                   # один проход
    python main.py --once --dry-run                         # без записи в базу
    python main.py --watch --interval 3600                  # встроенный цикл

2. Разовая выгрузка выдачи в JSON/CSV (как раньше):

    python main.py --category rossiya --query "ноутбук" --pages 2 \
        --pmin 10000 --pmax 50000 --output /data/output/laptops.json
"""

from __future__ import annotations

import argparse
import logging
import random
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from avito_parser import AvitoParser, save_to_csv, save_to_json

# URL выдачи по умолчанию для режима мониторинга: ноутбуки Волгограда,
# диагональ 15"+, цена 25 000–100 000 (фильтры зашиты в саму ссылку).
DEFAULT_SEARCH_URL = (
    "https://www.avito.ru/volgograd/noutbuki/diagonal_15inch_or_more-"
    "ASgBAgICAUSGoRTO5I4D?context=H4sIAAAAAAAA_wEmANn_YToxOntzOjE6InkiO3M6MTY6ImNhT2haMHRYYWtBamV5dTIiO32uOD3XJgAAAA"
    "&f=ASgBAgECAUSGoRTO5I4DAkXGmgwaeyJmcm9tIjoyNTAwMCwidG8iOjEwMDAwMH2coRQVeyJmcm9tIjoxNiwidG8iOm51bGx9"
    "&localPriority=1&s=104"
)


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Парсер объявлений Avito.ru")
    # --- режим мониторинга -------------------------------------------- #
    p.add_argument(
        "--once",
        action="store_true",
        help="Режим мониторинга: один проход с записью новых объявлений в SQLite",
    )
    p.add_argument(
        "--watch",
        action="store_true",
        help="Режим мониторинга: встроенный цикл каждые --interval секунд",
    )
    p.add_argument(
        "--interval",
        type=int,
        default=3600,
        help="Интервал цикла --watch, сек (по умолчанию 3600)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Полный проход без записи в базу (только с --once/--watch)",
    )
    p.add_argument(
        "--url",
        default=DEFAULT_SEARCH_URL,
        help="Полный URL выдачи Avito для мониторинга (фильтры внутри ссылки)",
    )
    p.add_argument(
        "--db",
        default="data/avito.db",
        help="Путь к файлу базы SQLite (по умолчанию data/avito.db)",
    )
    p.add_argument(
        "--max-pages-per-run",
        type=int,
        default=3,
        help="Максимум страниц выдачи за один проход мониторинга (по умолчанию 3)",
    )
    p.add_argument(
        "--via",
        choices=("api", "html"),
        default="api",
        help=(
            "Источник данных мониторинга: api — JSON-эндпоинт SPA (работает для "
            "любых фильтров, в т.ч. f=ASgB...); html — парсинг SSR-страниц "
            "(не работает с f=фильтрами)"
        ),
    )
    p.add_argument(
        "--jitter",
        type=int,
        default=300,
        help=(
            "Случайная задержка перед проходом, сек 0..--jitter (анти-робот; "
            "0 — выключить). По умолчанию 300."
        ),
    )
    p.add_argument(
        "--stats",
        action="store_true",
        help="Показать сводку по базе и журнал последних проходов, затем выйти",
    )
    p.add_argument(
        "--consider-ended-after",
        type=float,
        default=48.0,
        help="Лот, не виденный в выдаче дольше N часов, помечается ушедшим (48)",
    )
    p.add_argument(
        "--monitor-config",
        default="monitor.ini",
        help="Файл конфига уведомлений (по умолчанию monitor.ini; отсутствие = выключены)",
    )
    # --- разовая выгрузка (легаси-режим) ------------------------------- #
    p.add_argument(
        "--category",
        default="rossiya",
        help='Путь категории/региона как в URL Avito, напр. "moskva/kvartiry" (по умолчанию "rossiya")',
    )
    p.add_argument("--query", default=None, help="Поисковая фраза (?q=...)")
    p.add_argument("--pmin", default=None, help="Минимальная цена")
    p.add_argument("--pmax", default=None, help="Максимальная цена")
    p.add_argument("--pages", type=int, default=1, help="Сколько страниц выдачи обойти")
    p.add_argument(
        "--output",
        default="/data/output/result.json",
        help="Путь к выходному файлу (.json или .csv)",
    )
    p.add_argument(
        "--proxies-file",
        default=None,
        help="Путь к файлу со списком прокси (по одному на строку, формат http://user:pass@host:port)",
    )
    p.add_argument(
        "--cookies-file",
        default=None,
        help=(
            "Файл с куками браузера (строка заголовка Cookie, копируется из "
            "DevTools -> Network -> запрос к avito.ru -> Request Headers -> cookie). "
            "Резко повышает шанс пройти антибот без прокси."
        ),
    )
    p.add_argument("--min-delay", type=float, default=2.0, help="Мин. пауза между запросами, сек")
    p.add_argument("--max-delay", type=float, default=5.0, help="Макс. пауза между запросами, сек")
    p.add_argument("--verbose", action="store_true", help="Подробный лог (DEBUG)")
    p.add_argument(
        "--debug-dump-dir",
        default=None,
        help="Папка, куда сохранять сырой HTML при подозрении на блокировку (для диагностики)",
    )
    p.add_argument(
        "--no-warm-up",
        action="store_true",
        help="Не заходить сначала на главную страницу avito.ru перед поиском",
    )
    return p


def setup_logging(verbose: bool, log_dir: str = "logs") -> None:
    """Консоль + ротируемый файл logs/avito.log (5 МБ x 3 копии)."""
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        Path(log_dir) / "avito.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    logging.basicConfig(
        level=level,
        format=fmt,
        handlers=[logging.StreamHandler(), file_handler],
    )


def load_proxies(path: str | None) -> list[str]:
    if not path:
        return []
    file_path = Path(path)
    if not file_path.exists():
        logging.warning("Файл прокси %s не найден, работаю без прокси", path)
        return []
    lines = [line.strip() for line in file_path.read_text(encoding="utf-8").splitlines()]
    return [line for line in lines if line and not line.startswith("#")]


def load_cookies(path: str | None) -> str | None:
    if not path:
        return None
    file_path = Path(path)
    if not file_path.exists():
        logging.warning("Файл кук %s не найден, работаю без кук", path)
        return None
    text = file_path.read_text(encoding="utf-8-sig").strip().lstrip("\ufeff")
    if not text:
        logging.warning("Файл кук %s пустой", path)
        return None
    return text


def main() -> int:
    args = build_arg_parser().parse_args()

    setup_logging(args.verbose)

    if args.stats:
        return show_stats(args)

    if args.once or args.watch:
        return run_monitor(args)

    return run_export(args)


def build_parser_client(args: argparse.Namespace) -> AvitoParser:
    return AvitoParser(
        proxies=load_proxies(args.proxies_file),
        min_delay=args.min_delay,
        max_delay=args.max_delay,
        cookies=load_cookies(args.cookies_file),
        debug_dump_dir=args.debug_dump_dir,
        warm_up=not args.no_warm_up,
    )


def _sleep_interruptible(seconds: float) -> None:
    """Долгий сон короткими отрезками, чтобы Ctrl+C срабатывал мгновенно."""
    deadline = time.monotonic() + seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(1.0, remaining))


def run_monitor(args: argparse.Namespace) -> int:
    from db import Database
    from notify_telegram import load_config, make_notifier
    from watcher import run_once

    client = build_parser_client(args)
    database = Database(args.db)
    notifier = make_notifier(load_config(args.monitor_config))
    if notifier is not None:
        logging.getLogger("avito_parser.monitor").info(
            "Уведомления Telegram включены (фильтр локации: «%s»).",
            load_config(args.monitor_config).get("location_filter") or "—",
        )
    exit_code = 0
    cycle = 0
    logger = logging.getLogger("avito_parser.monitor")

    try:
        while True:
            if args.jitter > 0 and not args.dry_run:
                delay = random.uniform(0, args.jitter)
                logger.info("Jitter: случайная задержка %.0f сек перед проходом.", delay)
                _sleep_interruptible(delay)

            cycle += 1
            result = run_once(
                parser=client,
                db=database,
                search_url=args.url,
                max_pages=args.max_pages_per_run,
                dry_run=args.dry_run,
                via_api=args.via == "api",
                consider_ended_after_hours=args.consider_ended_after,
                on_new_listings=notifier,
            )
            stats = database.stats() if not args.dry_run else {"total": "—"}
            logger.info(
                "[цикл %s] статус=%s, страниц=%s, увидено=%s, новых=%s, в базе=%s.",
                cycle,
                result.status,
                result.pages_fetched,
                result.seen_total,
                result.new_count,
                stats["total"],
            )
            if result.status == "error":
                exit_code = 1

            # Здоровье мониторинга: несколько проходов подряд неуспешны —
            # это почти наверняка протухшие куки.
            if (
                stats.get("blocked_streak", 0) >= 3
                and result.status != "ok"
                and not args.dry_run
            ):
                logger.warning(
                    "Неудачных проходов подряд: %s. Похоже, cookies.txt устарел "
                    "или IP ограничен — обновите куки в браузере!",
                    stats["blocked_streak"],
                )

            if not args.watch:
                return exit_code

            next_at = time.strftime(
                "%H:%M:%S", time.localtime(time.time() + args.interval)
            )
            logger.info("Цикл %s завершён, следующий ~%s (Ctrl+C — стоп).", cycle, next_at)
            _sleep_interruptible(args.interval)
    except KeyboardInterrupt:
        logger.info("Остановлено вручную (Ctrl+C). Циклов выполнено: %s.", cycle)
    finally:
        database.close()


def show_stats(args: argparse.Namespace) -> int:
    from db import Database

    database = Database(args.db)
    try:
        s = database.stats()
        print(
            f"Всего: {s['total']} | Активных: {s['active']} | Ушедших: {s['ended']} | "
            f"Новых за 24ч: {s['new_24h']}"
        )
        print(
            f"Проходов за 24ч: {s['runs_24h']} "
            f"(ok {s['runs_ok_24h']} / blocked {s['runs_blocked_24h']}, "
            f"неудачных подряд: {s['blocked_streak']}), "
            f"последний новый лот: {s['latest_first_seen']}"
        )
        print("\nПоследние проходы:")
        for row in database.recent_runs(5):
            print(
                f"  {row['finished_at']} [{row['status']}] страниц={row['pages_fetched']} "
                f"увидено={row['seen_total']} новых={row['new_count']} {row['message'] or ''}"
            )
    finally:
        database.close()
    return 0


def run_export(args: argparse.Namespace) -> int:
    extra_params = {}
    if args.pmin:
        extra_params["pmin"] = args.pmin
    if args.pmax:
        extra_params["pmax"] = args.pmax

    parser = build_parser_client(args)

    listings = list(
        parser.search(
            query=args.query,
            category_path=args.category,
            extra_params=extra_params,
            max_pages=args.pages,
        )
    )

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if output_path.suffix.lower() == ".csv":
        save_to_csv(listings, output_path)
    else:
        save_to_json(listings, output_path)

    logging.getLogger("avito_parser").info(
        "Готово. Найдено объявлений: %s. Результат: %s", len(listings), output_path
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
