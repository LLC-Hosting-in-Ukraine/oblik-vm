"""Місця, між якими рухається майно.

Місце створюється автоматично при першому зверненні:
  * підрозділ (тільки з позначкою «Місце обліку») — склад, підрозділ, служба;
  * особа — майно, видане військовослужбовцю в користування. Воно рахується і за
    особою, і в залишках того місця обліку, яке його видало: видане в користування
    з обліку підрозділу не знімається (п. 11, 16 розд. V Інструкції). Тому в однієї
    особи може бути кілька місць — по одному на кожне місце обліку, що їй видавало
    (locations.unit_id для kind = 'person');
  * контрагент — зовнішня сторона (вища частина, постачальник, інша частина);
  * службові — «Початкові залишки», «Списано».
"""
from __future__ import annotations

import sqlite3

from .textutil import person_short

INTERNAL_KINDS = ("unit", "person")   # тут є залишки
EXTERNAL_KINDS = ("counterparty", "system")

OPENING = "opening"
WRITTEN_OFF = "written_off"
OBTAINED = "obtained"         # оприбутковано за актом якісного стану (об'єднання, розбирання)


class LocationError(ValueError):
    pass


def _ensure(conn: sqlite3.Connection, column: str, kind: str, ref_id: int) -> int:
    row = conn.execute(f"SELECT id FROM locations WHERE kind = ? AND {column} = ?",
                       (kind, ref_id)).fetchone()
    if row:
        return row["id"]
    cur = conn.execute(f"INSERT INTO locations(kind, {column}) VALUES (?, ?)", (kind, ref_id))
    return cur.lastrowid


def for_unit(conn: sqlite3.Connection, unit_id: int) -> int:
    row = conn.execute("SELECT name, is_accounting FROM units WHERE id = ?", (unit_id,)).fetchone()
    if row is None:
        raise LocationError("Підрозділ не знайдено")
    if not row["is_accounting"]:
        raise LocationError(
            f"«{row['name']}» не позначено як місце обліку (довідник «Структура частини»)")
    return _ensure(conn, "unit_id", "unit", unit_id)


def for_person(conn: sqlite3.Connection, person_id: int, owner_unit_id: int | None = None) -> int:
    """Місце «видане особі» за місцем обліку owner_unit_id (хто видав).
    Не вказано — за замовчуванням (default_owner)."""
    if conn.execute("SELECT 1 FROM persons WHERE id = ?", (person_id,)).fetchone() is None:
        raise LocationError("Особу не знайдено")
    if owner_unit_id is None:
        owner_unit_id = default_owner(conn, person_id)
    elif conn.execute("SELECT 1 FROM units WHERE id = ? AND is_accounting = 1",
                      (owner_unit_id,)).fetchone() is None:
        raise LocationError("Видане особі може рахуватися лише за місцем обліку")
    row = conn.execute("SELECT id FROM locations WHERE kind = 'person' AND person_id = ? "
                       "AND unit_id IS ?", (person_id, owner_unit_id)).fetchone()
    if row:
        return row["id"]
    return conn.execute("INSERT INTO locations(kind, person_id, unit_id) VALUES ('person', ?, ?)",
                        (person_id, owner_unit_id)).lastrowid


def default_owner(conn: sqlite3.Connection, person_id: int) -> int | None:
    """За яким місцем обліку рахувати видане особі, якщо невідомо, хто видав
    (початкові залишки, надходження прямо особі): найближче вгору по структурі,
    а якщо такого немає і місце обліку в частині одне — воно."""
    unit = accounting_unit_of_person(conn, person_id)
    if unit is not None:
        return unit
    rows = conn.execute("SELECT id FROM units WHERE is_accounting = 1 LIMIT 2").fetchall()
    return rows[0]["id"] if len(rows) == 1 else None


def owner(conn: sqlite3.Connection, location_id: int) -> int | None:
    """Місце обліку, до якого належить місце: сам підрозділ або той, хто видав особі."""
    loc = get(conn, location_id)
    return loc["unit_id"] if loc["kind"] in INTERNAL_KINDS else None


def person_locations(conn: sqlite3.Connection, person_id: int) -> list[int]:
    return [r["id"] for r in conn.execute(
        "SELECT id FROM locations WHERE kind = 'person' AND person_id = ? ORDER BY id", (person_id,))]


def _many_accounting_units(conn) -> bool:
    return conn.execute("SELECT COUNT(*) FROM units WHERE is_accounting = 1").fetchone()[0] > 1


def for_counterparty(conn: sqlite3.Connection, counterparty_id: int) -> int:
    if conn.execute("SELECT 1 FROM counterparties WHERE id = ?", (counterparty_id,)).fetchone() is None:
        raise LocationError("Контрагента не знайдено")
    return _ensure(conn, "counterparty_id", "counterparty", counterparty_id)


def system(conn: sqlite3.Connection, code: str) -> int:
    row = conn.execute("SELECT id FROM locations WHERE system_code = ?", (code,)).fetchone()
    if row is None:
        raise LocationError(f"Службове місце «{code}» не знайдено")
    return row["id"]


def get(conn: sqlite3.Connection, location_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM locations WHERE id = ?", (location_id,)).fetchone()
    if row is None:
        raise LocationError(f"Місце #{location_id} не знайдено")
    return row


def kind(conn: sqlite3.Connection, location_id: int) -> str:
    return get(conn, location_id)["kind"]


def title(conn: sqlite3.Connection, location_id: int, with_owner: bool = True) -> str:
    """Назва місця. with_owner=False — без «(видано: …)», для паперових документів."""
    loc = get(conn, location_id)
    if loc["kind"] == "unit":
        u = conn.execute("SELECT name, short_name FROM units WHERE id = ?", (loc["unit_id"],)).fetchone()
        return u["name"]
    if loc["kind"] == "person":
        p = conn.execute("SELECT * FROM persons WHERE id = ?", (loc["person_id"],)).fetchone()
        name = person_short(p["last_name"], p["first_name"], p["middle_name"])
        name = f"{p['rank']} {name}" if p["rank"] else name
        if with_owner and loc["unit_id"] and _many_accounting_units(conn):
            u = conn.execute("SELECT name, short_name FROM units WHERE id = ?", (loc["unit_id"],)).fetchone()
            name += f" (видано: {u['short_name'] or u['name']})"
        return name
    if loc["kind"] == "counterparty":
        return conn.execute("SELECT name FROM counterparties WHERE id = ?",
                            (loc["counterparty_id"],)).fetchone()["name"]
    return loc["system_name"]


def accounting_unit_of_person(conn: sqlite3.Connection, person_id: int) -> int | None:
    """Найближчий вгору по структурі підрозділ-місце обліку, до якого належить особа."""
    row = conn.execute("SELECT unit_id FROM persons WHERE id = ?", (person_id,)).fetchone()
    unit_id, seen = (row["unit_id"] if row else None), set()
    while unit_id is not None and unit_id not in seen:
        seen.add(unit_id)
        u = conn.execute("SELECT parent_id, is_accounting FROM units WHERE id = ?", (unit_id,)).fetchone()
        if u is None:
            return None
        if u["is_accounting"]:
            return unit_id
        unit_id = u["parent_id"]
    return None


def subtree_units(conn: sqlite3.Connection, unit_id: int) -> list[int]:
    """Підрозділ і всі підлеглі (на будь-якій глибині)."""
    units, frontier = [unit_id], [unit_id]
    while frontier:
        children = [r["id"] for r in conn.execute(
            f"SELECT id FROM units WHERE parent_id IN ({','.join('?' * len(frontier))})", frontier)]
        children = [c for c in children if c not in units]
        units.extend(children)
        frontier = children
    return units


def scope_for_unit(conn: sqlite3.Connection, unit_id: int, include_subunits: bool = False) -> list[int]:
    """Місця, залишки яких входять у залишки підрозділу: сам підрозділ + видане ним особам
    (+ підлеглі місця обліку з їхнім виданим, якщо треба)."""
    units = subtree_units(conn, unit_id) if include_subunits else [unit_id]
    accounting_units = {u for u in units if conn.execute(
        "SELECT is_accounting FROM units WHERE id = ?", (u,)).fetchone()["is_accounting"]}

    # Підрозділ + видане особам саме цим підрозділом (хоч би де ці особи числилися).
    return [loc["id"] for loc in conn.execute(
        "SELECT id, unit_id FROM locations WHERE kind IN ('unit', 'person') ORDER BY id")
        if loc["unit_id"] in accounting_units]
