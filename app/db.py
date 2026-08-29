"""
Слой SQLite для мониторинга объявлений Avito.

Без ORM: stdlib sqlite3, режим WAL. Один процесс-писатель — конфликтов нет.

Схема:
- listings — карточки объявлений; PK = числовой id объявления с сайта.
  first_seen_at/last_seen_at (UTC) позволяют отличать новые лоты от уже
  известных; status='ended' + ended_at — лоты, которых давно не было в выдаче.
- runs — журнал проходов мониторинга (когда, что нашли, чем кончилось).

Миграции: PRAGMA user_version (1 — начальная схема, 2 — ended/status).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Iterable

logger = logging.getLogger("avito_parser.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    id            INTEGER PRIMARY KEY,
    title         TEXT,
    price_raw     INTEGER,
    url           TEXT,
    location      TEXT,
    seller        TEXT,
    image_url     TEXT,
    published_txt TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    details_json  TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at    TEXT,
    finished_at   TEXT,
    status        TEXT,
    pages_fetched INTEGER,
    seen_total    INTEGER,
    new_count     INTEGER,
    message       TEXT
);
"""

# v2: пометка «ушедших» объявлений
MIGRATIONS = {
    2: "ALTER TABLE listings ADD COLUMN status TEXT NOT NULL DEFAULT 'active';\n"
       "ALTER TABLE listings ADD COLUMN ended_at TEXT;",
}


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """Последовательные миграции по PRAGMA user_version."""
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        for target in sorted(MIGRATIONS):
            if version < target:
                logger.info("Миграция базы %s -> v%s", self.path, target)
                self._conn.executescript(MIGRATIONS[target])
                self._conn.execute(f"PRAGMA user_version = {target}")
                version = target

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------ #
    # listings
    # ------------------------------------------------------------------ #
    def known_ids(self) -> set[int]:
        """Все id объявлений, которые уже лежат в базе."""
        rows = self._conn.execute("SELECT id FROM listings").fetchall()
        return {r["id"] for r in rows}

    def apply_diff(
        self,
        new_listings: Iterable,
        touched_ids: Iterable[int],
        ts: str,
    ) -> int:
        """
        Атомарно: вставляет новые объявления и обновляет last_seen у известных.
        Возвращает число вставленных строк.
        """
        new_listings = list(new_listings)
        touched_ids = list(touched_ids)

        with self._conn:
            if new_listings:
                self._conn.executemany(
                    """
                    INSERT INTO listings (
                        id, title, price_raw, url, location, seller,
                        image_url, published_txt, first_seen_at, last_seen_at,
                        details_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        title=excluded.title,
                        price_raw=excluded.price_raw,
                        last_seen_at=excluded.last_seen_at,
                        status='active',
                        ended_at=NULL
                    """,
                    [
                        (
                            int(l.id),
                            l.title,
                            l.price_raw,
                            l.url,
                            l.location,
                            l.seller,
                            l.image_url,
                            getattr(l, "date_published", None),
                            ts,
                            ts,
                            json.dumps(l.extra, ensure_ascii=False) if l.extra else None,
                        )
                        for l in new_listings
                    ],
                )
            if touched_ids:
                self._conn.executemany(
                    """
                    UPDATE listings
                    SET last_seen_at = ?,
                        status = 'active',
                        ended_at = CASE WHEN status = 'ended' THEN NULL ELSE ended_at END
                    WHERE id = ?
                    """,
                    [(ts, i) for i in touched_ids],
                )
        logger.debug(
            "apply_diff: новых=%s, обновлено last_seen=%s", len(new_listings), len(touched_ids)
        )
        return len(new_listings)

    def mark_ended(self, older_than_hours: float, now_iso: str) -> int:
        """
        Помечает «ушедшими» лоты, которые не встречались в выдаче дольше порога.
        Возвращает число помеченных. Реверс (лот снова появился) обрабатывает
        apply_diff — статус возвращается в active автоматически.
        """
        hours = f"-{float(older_than_hours)} hours"
        with self._conn:
            cur = self._conn.execute(
                """
                UPDATE listings
                SET status = 'ended', ended_at = ?
                WHERE status = 'active'
                  AND last_seen_at < datetime('now', ?)
                """,
                (now_iso, hours),
            )
        count = cur.rowcount
        if count:
            logger.info("Помечено ушедших объявлений: %s (не видны >%sч).", count, older_than_hours)
        return count

    def stats(self) -> dict:
        row = self._conn.execute(
            """
            SELECT
                COUNT(*)                                            AS total,
                COALESCE(SUM(status = 'active'), 0)                 AS active,
                COALESCE(SUM(status = 'ended'), 0)                  AS ended,
                COALESCE(SUM(first_seen_at >= datetime('now','-24 hours')), 0)
                                                                    AS new_24h,
                MAX(first_seen_at)                                  AS latest_first_seen
            FROM listings
            """
        ).fetchone()

        runs_row = self._conn.execute(
            """
            SELECT
                COALESCE(SUM(started_at >= datetime('now','-24 hours')), 0) AS runs_24h,
                COALESCE(SUM(status = 'ok'
                             AND started_at >= datetime('now','-24 hours')), 0)
                                                                            AS runs_ok_24h,
                COALESCE(SUM(status = 'blocked'
                             AND started_at >= datetime('now','-24 hours')), 0)
                                                                            AS runs_blocked_24h
            FROM runs
            """
        ).fetchone()

        return {
            "total": row["total"],
            "active": row["active"],
            "ended": row["ended"],
            "new_24h": row["new_24h"],
            "latest_first_seen": row["latest_first_seen"],
            "runs_24h": runs_row["runs_24h"],
            "runs_ok_24h": runs_row["runs_ok_24h"],
            "runs_blocked_24h": runs_row["runs_blocked_24h"],
            # сколько проходов подряд последним статусом, отличным от ok
            "blocked_streak": self._blocked_streak(),
        }

    def _blocked_streak(self) -> int:
        """Сколько последних проходов подряд завершились не с 'ok'."""
        streak = 0
        for row in self._conn.execute(
            "SELECT status FROM runs ORDER BY id DESC LIMIT 10"
        ):
            if row["status"] == "ok":
                break
            streak += 1
        return streak

    # ------------------------------------------------------------------ #
    # runs
    # ------------------------------------------------------------------ #
    def log_run(
        self,
        started_at: str,
        status: str,
        pages_fetched: int,
        seen_total: int,
        new_count: int,
        message: str = "",
    ) -> None:
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO runs (
                    started_at, finished_at, status, pages_fetched,
                    seen_total, new_count, message
                ) VALUES (?, strftime('%Y-%m-%dT%H:%M:%SZ','now'), ?, ?, ?, ?, ?)
                """,
                (started_at, status, pages_fetched, seen_total, new_count, message),
            )

    def recent_runs(self, limit: int = 5) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
