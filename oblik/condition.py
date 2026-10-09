"""Акт якісного (технічного) стану: склад (комплектність) оприбуткованої системи зі списаного.

Коли кілька одиниць об'єднують в одну систему, списані одиниці стають її складовими. Програма
переносить їх у «Склад (комплектність)» картки системи: назва, кількість, одиниця виміру,
заводський (інвентарний) номер, категорія — у примітці.

© 2026 Yevhenii Hosting by LLC Hosting in Ukraine
"""
from __future__ import annotations

import sqlite3

from . import documents, refs
from .documents import CATEGORY_TITLES, DocumentError


def components_from_act(conn: sqlite3.Connection, doc_id: int) -> list[dict]:
    """Складові — рядки «Списати» акта, по порядку."""
    result = []
    for ln in documents.lines(conn, doc_id):
        if ln["direction"]:
            continue
        nom = conn.execute("SELECT name, uom FROM nomenclature WHERE id = ?", (ln["nomenclature_id"],)).fetchone()
        item = (conn.execute("SELECT serial_no, inventory_no FROM items WHERE id = ?", (ln["item_id"],)).fetchone()
                if ln["item_id"] else None)
        number = ""
        if item is not None:
            number = item["serial_no"] or (f"інв. № {item['inventory_no']}" if item["inventory_no"] else "")
        cat = CATEGORY_TITLES.get(ln["category"])
        result.append({"name": nom["name"], "qty_m": ln["qty_m"], "uom": nom["uom"],
                       "serial_no": number or None, "note": f"кат. {cat}" if cat else None})
    return result


def obtained_items(conn: sqlite3.Connection, doc_id: int) -> list[sqlite3.Row]:
    """Поштучні одиниці, оприбутковані актом (системи), з кількістю складових."""
    return conn.execute(
        "SELECT i.id, (SELECT COUNT(*) FROM item_components c WHERE c.item_id = i.id) AS n_components "
        "FROM document_lines l JOIN items i ON i.id = l.item_id "
        "WHERE l.document_id = ? AND l.direction = 'in' ORDER BY l.line_no", (doc_id,)).fetchall()


def fill_components(conn: sqlite3.Connection, doc_id: int, item_id: int) -> int:
    """Записати в склад одиниці item_id списане за актом (замість попереднього складу).
    Зміна потрапляє в журнал змін одиниці. Повертає кількість складових."""
    doc = documents.get(conn, doc_id)
    if doc["doc_type"] != "condition" or doc["status"] != "posted":
        raise DocumentError("Склад заповнюється з проведеного (затвердженого) акта якісного стану")
    if item_id not in [r["id"] for r in obtained_items(conn, doc_id)]:
        raise DocumentError("Ця одиниця не оприбуткована цим актом")
    ref = refs.REFS["items"]
    values = refs.get(conn, ref, item_id)
    values["components"] = components_from_act(conn, doc_id)
    refs.save(conn, ref, item_id, values)
    return len(values["components"])


def fill_after_post(conn: sqlite3.Connection, doc_id: int) -> tuple[int, int] | None:
    """Після проведення: якщо оприбутковано рівно одну систему і її склад порожній — заповнити.
    Повертає (id одиниці, кількість складових) або None."""
    items = obtained_items(conn, doc_id)
    if len(items) != 1 or items[0]["n_components"]:
        return None
    return items[0]["id"], fill_components(conn, doc_id, items[0]["id"])
