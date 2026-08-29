"""
Read-only хелпер для команды /stats: открывает SQLite avito-парсера
в режиме read-only и читает сводку. Бот и парсер шарят volume data/.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger("avito_bot.db")

# SQLite URI для read-only: режим, без записи на диск.
_RO_URI = "file:{}?mode=ro"


def _connect_ro(path: Path) -> sqlite3.Connection:
    uri = _RO_URI.format(path.resolve().as_posix())
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def read_stats(db_path: Path) -> dict[str, Any] | None:
    """
    Возвращает сводку как в app/db.py:Database.stats(), либо None, если
    базы ещё нет (парсер ни разу не проходил).
    """
    if not db_path.exists():
        return None
    try:
        with _connect_ro(db_path) as conn:
            row = conn.execute(
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
            runs_row = conn.execute(
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
            }
    except sqlite3.OperationalError as exc:
        logger.warning("Не удалось прочитать %s: %s", db_path, exc)
        return None


def read_recent_runs(db_path: Path, limit: int = 5) -> list[dict[str, Any]]:
    if not db_path.exists():
        return []
    try:
        with _connect_ro(db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]
    except sqlite3.OperationalError as exc:
        logger.warning("Не удалось прочитать runs: %s", exc)
        return []


def format_stats(stats: dict[str, Any], recent: list[dict[str, Any]]) -> str:
    lines = [
        "📊 <b>Avito-мониторинг: статистика</b>\n",
        f"Всего объявлений: <b>{stats['total']}</b>",
        f"  активных: {stats['active']}",
        f"  ушедших: {stats['ended']}",
        f"  новых за 24ч: {stats['new_24h']}",
        f"  последний новый: {stats['latest_first_seen'] or '—'}\n",
        f"Проходов за 24ч: <b>{stats['runs_24h']}</b>",
        f"  ok: {stats['runs_ok_24h']}",
        f"  blocked: {stats['runs_blocked_24h']}",
    ]
    if recent:
        lines.append("\n<b>Последние проходы:</b>")
        for r in recent:
            msg = (r.get("message") or "").strip()
            tail = f" — {msg[:60]}" if msg else ""
            lines.append(
                f"  {r['finished_at']} [{r['status']}] "
                f"новых={r['new_count']}{tail}"
            )
    return "\n".join(lines)
