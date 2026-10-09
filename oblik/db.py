"""Підключення до SQLite і міграції схеми.

Версія схеми зберігається в PRAGMA user_version. Кожен файл schema/NNN_назва.sql —
одна міграція; вони застосовуються по черзі, кожна в окремій транзакції.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from . import __version__, textutil

SCHEMA_DIR = Path(__file__).resolve().parent / "schema"
# Перший рядок міграції, яка перебудовує таблицю з посиланнями на неї.
FK_OFF_MARK = "-- foreign_keys: off"


class NewerDatabaseError(RuntimeError):
    """База створена новішою версією програми."""


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # Без WAL: база — завжди один файл, її безпечно копіювати.
    conn.execute("PRAGMA journal_mode = DELETE")
    conn.execute("PRAGMA synchronous = FULL")
    # Сортування за українською абеткою: ORDER BY name COLLATE UK
    conn.create_collation("UK", textutil.uk_collate)
    # Пошук без урахування регістру для кирилиці: ulower(name) LIKE ?
    conn.create_function("ulower", 1, textutil.lower, deterministic=True)
    return conn


def migrations() -> list[tuple[int, Path]]:
    files = sorted(SCHEMA_DIR.glob("[0-9][0-9][0-9]_*.sql"))
    return [(int(f.name[:3]), f) for f in files]


def schema_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def migrate(conn: sqlite3.Connection) -> list[int]:
    """Застосувати нові міграції. Повертає номери застосованих."""
    current = schema_version(conn)
    known = migrations()
    latest = known[-1][0] if known else 0
    if current > latest:
        raise NewerDatabaseError(
            f"Базу створено новішою версією програми (схема {current}, "
            f"ця програма знає до {latest}). Оновіть програму."
        )
    applied = []
    for num, path in known:
        if num <= current:
            continue
        sql = path.read_text(encoding="utf-8")
        # Перебудова таблиці, на яку посилаються інші (порядок SQLite «12 кроків»):
        # перевірку зв'язків вимикаємо на час міграції, а потім перевіряємо всю базу.
        rebuild = sql.lstrip().startswith(FK_OFF_MARK)
        if rebuild:
            conn.execute("PRAGMA foreign_keys = OFF")
        try:
            # Транзакція лишається відкритою, поки не перевіримо зв'язки.
            conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {num};")
            if rebuild:
                broken = conn.execute("PRAGMA foreign_key_check").fetchall()
                if broken:
                    raise sqlite3.IntegrityError(
                        f"Міграція {num}: порушено зв'язки між таблицями {[tuple(r) for r in broken[:5]]}")
            conn.execute("COMMIT")
        except Exception:
            conn.rollback()
            raise
        finally:
            if rebuild:
                conn.execute("PRAGMA foreign_keys = ON")
        applied.append(num)
    return applied


def init_meta(conn: sqlite3.Connection, db_uuid: str) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    with conn:
        conn.executemany(
            "INSERT INTO meta(key, value) VALUES (?, ?)",
            [
                ("db_uuid", db_uuid),
                ("change_seq", "0"),
                ("created_at", now),
                ("created_by_version", __version__),
            ],
        )


def bump_change_seq(conn: sqlite3.Connection) -> None:
    """Лічильник змін: за ним визначаємо, чи є незбережені на флешку зміни."""
    conn.execute(
        "UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key = 'change_seq'"
    )
