"""Перший запуск: правила використання і покрокова «навчалка» (з чого почати облік).

Прийняття правил зберігається в самій базі (таблиця settings), тому повторно питається
лише для нової бази або коли змінилася редакція правил (TERMS_VERSION).
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from . import audit, settings

TERMS_VERSION = 1
_TERMS_KEY = "terms_accepted"


# --- Правила використання ---------------------------------------------------------

def terms_accepted(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (_TERMS_KEY,)).fetchone()
    if row is None or not row["value"]:
        return None
    try:
        data = json.loads(row["value"])
    except ValueError:
        return None
    return data if data.get("version") == TERMS_VERSION else None


def accept_terms(conn: sqlite3.Connection) -> None:
    value = json.dumps({"version": TERMS_VERSION,
                        "at": datetime.now().isoformat(timespec="seconds")})
    conn.execute("INSERT INTO settings(key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (_TERMS_KEY, value))
    audit.write(conn, "accept", "settings", None,
                f"Прийнято правила використання (редакція {TERMS_VERSION})")


# --- Кроки «з чого почати» ----------------------------------------------------------

@dataclass
class Step:
    code: str
    title: str
    done: bool
    optional: bool = False
    main: bool = False      # головний крок — початкові залишки


def _count(conn, sql: str) -> int:
    return conn.execute(sql).fetchone()[0]


def steps(conn: sqlite3.Connection) -> list[Step]:
    return [
        Step("settings", "Реквізити частини і хто веде облік", settings.is_complete(conn)),
        Step("persons", "Люди: ви (МВО), командир, начальник служби, члени комісії",
             _count(conn, "SELECT COUNT(*) FROM persons") > 0),
        Step("units", "Структура: де лежить майно і хто за нього відповідає",
             _count(conn, "SELECT COUNT(*) FROM units WHERE is_accounting = 1 "
                          "AND mvo_person_id IS NOT NULL") > 0),
        Step("nomenclature", "Номенклатура (можна пропустити)",
             _count(conn, "SELECT COUNT(*) FROM nomenclature") > 0, optional=True),
        Step("opening", "Початкові залишки — головний крок",
             _count(conn, "SELECT COUNT(*) FROM documents WHERE doc_type = 'opening' "
                          "AND status = 'posted'") > 0, main=True),
        Step("daily", "Щоденна робота: надходження, видача, звіти",
             _count(conn, "SELECT COUNT(*) FROM documents WHERE doc_type <> 'opening' "
                          "AND status = 'posted'") > 0),
    ]


def is_started(conn: sqlite3.Connection) -> bool:
    """Обов'язкові кроки до початкових залишків включно виконано."""
    return all(s.done for s in steps(conn) if not s.optional and s.code != "daily")
