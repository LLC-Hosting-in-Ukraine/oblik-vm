"""Журнал змін (audit_log): хто, коли і що змінив."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime

ACTIONS = {
    "create": "Створено",
    "update": "Змінено",
    "delete": "Видалено",
    "deactivate": "Зроблено неактивним",
    "activate": "Знову активний",
    "post": "Проведено",
    "reverse": "Сторновано",
    "cancel": "Анульовано",
    "mark": "Відмічено виконання",
    "unmark": "Відмітку видалено",
}


def operator(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = 'operator_name'").fetchone()
    return row[0] if row and row[0] else None


def write(
    conn: sqlite3.Connection,
    action: str,
    entity: str,
    entity_id: int | None,
    summary: str,
    changes: list[dict] | None = None,
) -> None:
    """Записати подію. changes: [{"field", "label", "old", "new"}, ...] — уже у вигляді тексту."""
    conn.execute(
        "INSERT INTO audit_log(ts, operator, action, entity, entity_id, summary, details) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            datetime.now().isoformat(timespec="seconds"),
            operator(conn),
            action,
            entity,
            entity_id,
            summary,
            json.dumps(changes, ensure_ascii=False) if changes else None,
        ),
    )


def diff(old: dict[str, str], new: dict[str, str], labels: dict[str, str]) -> list[dict]:
    """Різниця між двома наборами вже відформатованих значень."""
    changes = []
    for key, label in labels.items():
        before, after = old.get(key, ""), new.get(key, "")
        if before != after:
            changes.append({"field": key, "label": label, "old": before, "new": after})
    return changes
