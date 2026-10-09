"""Залишки й історія руху — тільки з журналу руху (movements).

Залишок рахується в розрізі ключа: номенклатура, поштучна одиниця, категорія, ціна.
Одне найменування з різними цінами або категоріями — це різні рядки залишку
(так само, як у нарядах і книгах обліку).
Дата «на DD.MM» означає кінець цього дня: враховано всі рухи з датою ≤ DD.MM.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .textutil import QTY_SCALE


@dataclass(frozen=True)
class Key:
    nomenclature_id: int
    item_id: int | None
    category: int | None
    price_kop: int


def value_kop(qty_m: int, price_kop: int) -> int:
    """Сума = кількість × ціна, з округленням до копійки."""
    return (qty_m * price_kop + QTY_SCALE // 2) // QTY_SCALE


def _in_list(column: str, values: list[int]) -> tuple[str, list]:
    return f"{column} IN ({','.join('?' * len(values))})", list(values)


def balances(
    conn: sqlite3.Connection,
    location_ids: list[int] | None = None,
    on_date: str | None = None,
    nomenclature_id: int | None = None,
    item_id: int | None = None,
) -> list[dict]:
    """Ненульові залишки по місцях. location_ids=None — усі внутрішні місця (підрозділи й особи)."""
    def side(loc_col: str, sign: str) -> tuple[str, list]:
        where, params = [], []
        if location_ids is None:
            where.append(f"{loc_col} IN (SELECT id FROM locations WHERE kind IN ('unit', 'person'))")
        else:
            if not location_ids:
                where.append("0")
            else:
                clause, p = _in_list(loc_col, location_ids)
                where.append(clause)
                params += p
        if on_date:
            where.append("op_date <= ?")
            params.append(on_date)
        if nomenclature_id is not None:
            where.append("nomenclature_id = ?")
            params.append(nomenclature_id)
        if item_id is not None:
            where.append("item_id = ?")
            params.append(item_id)
        sql = (f"SELECT {loc_col} AS location_id, nomenclature_id, item_id, category, price_kop, "
               f"{sign}qty_m AS q FROM movements WHERE " + " AND ".join(where))
        return sql, params

    sql_in, p_in = side("to_location_id", "")
    sql_out, p_out = side("from_location_id", "-")
    rows = conn.execute(
        f"SELECT location_id, nomenclature_id, item_id, category, price_kop, SUM(q) AS qty_m "
        f"FROM ({sql_in} UNION ALL {sql_out}) "
        f"GROUP BY location_id, nomenclature_id, item_id, category, price_kop "
        f"HAVING SUM(q) <> 0",
        p_in + p_out,
    ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["value_kop"] = value_kop(d["qty_m"], d["price_kop"])
        result.append(d)
    return result


def key_balance(conn: sqlite3.Connection, location_id: int, key: Key, on_date: str | None) -> int:
    params = [key.nomenclature_id, key.item_id, key.category, key.price_kop]
    date_sql = ""
    if on_date:
        date_sql = " AND op_date <= ?"
        params.append(on_date)
    match = "nomenclature_id = ? AND item_id IS ? AND category IS ? AND price_kop = ?"
    incoming = conn.execute(
        f"SELECT COALESCE(SUM(qty_m), 0) FROM movements WHERE to_location_id = ? AND {match}{date_sql}",
        [location_id, *params]).fetchone()[0]
    outgoing = conn.execute(
        f"SELECT COALESCE(SUM(qty_m), 0) FROM movements WHERE from_location_id = ? AND {match}{date_sql}",
        [location_id, *params]).fetchone()[0]
    return incoming - outgoing


def available(conn: sqlite3.Connection, location_id: int, key: Key, on_date: str) -> int:
    """Скільки можна забрати з місця на дату, щоб залишок не став від'ємним
    ні на цю дату, ні пізніше (важливо для документів заднім числом)."""
    balance = key_balance(conn, location_id, key, on_date)
    lowest = balance
    match = "nomenclature_id = ? AND item_id IS ? AND category IS ? AND price_kop = ?"
    rows = conn.execute(
        f"SELECT op_date, SUM(CASE WHEN to_location_id = ? THEN qty_m ELSE -qty_m END) AS delta "
        f"FROM movements WHERE (to_location_id = ? OR from_location_id = ?) AND {match} "
        f"AND op_date > ? GROUP BY op_date ORDER BY op_date",
        [location_id, location_id, location_id,
         key.nomenclature_id, key.item_id, key.category, key.price_kop, on_date],
    ).fetchall()
    for r in rows:
        balance += r["delta"]
        lowest = min(lowest, balance)
    return lowest


def item_position(conn: sqlite3.Connection, item_id: int, on_date: str | None = None) -> dict | None:
    """Де перебуває поштучна одиниця (місце, категорія, ціна). None — не на обліку."""
    rows = [r for r in balances(conn, None, on_date, item_id=item_id) if r["qty_m"] > 0]
    return rows[0] if rows else None


def item_has_movements_after(conn: sqlite3.Connection, item_id: int, on_date: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM movements WHERE item_id = ? AND op_date > ? LIMIT 1", (item_id, on_date)
    ).fetchone() is not None


def item_card(conn: sqlite3.Connection, item_id: int) -> list[dict]:
    """Картка руху одиниці: усі рухи в хронологічному порядку."""
    return [dict(r) for r in conn.execute(
        "SELECT m.*, d.doc_type, d.doc_no, d.doc_date, d.reg_no, d.reg_year, d.reversal_of_id "
        "FROM movements m JOIN documents d ON d.id = m.document_id "
        "WHERE m.item_id = ? ORDER BY m.op_date, d.posted_at, m.id", (item_id,))]


def location_history(conn: sqlite3.Connection, location_id: int,
                     nomenclature_id: int | None = None) -> list[dict]:
    """Усі рухи через місце (для книги обліку / картки номенклатури)."""
    params: list = [location_id, location_id]
    extra = ""
    if nomenclature_id is not None:
        extra = " AND m.nomenclature_id = ?"
        params.append(nomenclature_id)
    return [dict(r) for r in conn.execute(
        "SELECT m.*, d.doc_type, d.doc_no, d.doc_date, d.reversal_of_id, "
        "CASE WHEN m.to_location_id = ? THEN m.qty_m ELSE -m.qty_m END AS delta_m "
        "FROM movements m JOIN documents d ON d.id = m.document_id "
        f"WHERE (m.to_location_id = ? OR m.from_location_id = ?){extra} "
        "ORDER BY m.op_date, d.posted_at, m.id",
        [location_id, *params])]
