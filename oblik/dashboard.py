"""Головна сторінка: основні показники обліку і нагадування «що не зроблено».

Усе рахується з бази на сьогодні; нічого не змінює.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta

from . import balances, onboarding, orders, refs, settings
from .documents import fmt_date
from .reports import CLASS_TITLES
from .textutil import QTY_SCALE, format_money, uk_sort_key

STALE_DRAFT_DAYS = 3
ORDER_SOON_DAYS = 3   # за скільки днів попереджати, що строк дії наряду спливає


@dataclass
class Reminder:
    level: str          # critical / warning / info
    text: str
    link: str | None = None   # endpoint
    link_args: dict | None = None
    link_text: str = "Відкрити"


def _bars(items: list[tuple[str, int]], total: int) -> list[dict]:
    """Смуги «частка від загальної суми» — від більшої до меншої."""
    items = sorted(items, key=lambda x: (-x[1], uk_sort_key(x[0])))
    top = max((v for _, v in items), default=0) or 1
    return [{"title": t, "value": format_money(v), "width": round(100 * v / top, 1),
             "share": round(100 * v / total) if total else 0} for t, v in items if v]


def metrics(conn: sqlite3.Connection, today: date | None = None) -> dict:
    today = today or date.today()
    on = today.isoformat()
    rows = balances.balances(conn, None, on)
    kinds = {r["id"]: (r["kind"], r["unit_id"]) for r in conn.execute(
        "SELECT id, kind, unit_id FROM locations WHERE kind IN ('unit', 'person')")}
    noms = {r["id"]: r for r in conn.execute("SELECT id, accounting_class FROM nomenclature")}

    total = issued = 0
    issued_persons: set[int] = set()
    by_unit: dict[int, int] = {}
    by_class: dict[str, int] = {}
    items = 0
    names: set[int] = set()
    for r in rows:
        if r["qty_m"] <= 0:
            continue
        kind, unit = kinds.get(r["location_id"], ("", None))
        total += r["value_kop"]
        names.add(r["nomenclature_id"])
        if r["item_id"]:
            items += r["qty_m"] // QTY_SCALE
        if kind == "person":
            issued += r["value_kop"]
            issued_persons.add(r["location_id"])
        if unit is not None:
            by_unit[unit] = by_unit.get(unit, 0) + r["value_kop"]
        cls = noms[r["nomenclature_id"]]["accounting_class"]
        by_class[cls] = by_class.get(cls, 0) + r["value_kop"]

    persons = {conn.execute("SELECT person_id FROM locations WHERE id = ?", (loc,)).fetchone()[0]
               for loc in issued_persons}
    year = today.year
    docs = dict(conn.execute("SELECT status, COUNT(*) FROM documents WHERE reg_year = ? GROUP BY status",
                             (year,)).fetchall())
    month_start = today.replace(day=1).isoformat()
    month_docs = conn.execute("SELECT COUNT(*) FROM documents WHERE status = 'posted' AND op_date >= ? "
                              "AND op_date <= ?", (month_start, on)).fetchone()[0]
    labels = refs.Labels(conn)
    unit_bars = _bars([(labels.get("units", u), v) for u, v in by_unit.items()], total)
    return {
        "today": today,
        "total": format_money(total), "total_kop": total,
        "names": len(names), "serial_units": items,
        "issued": format_money(issued), "issued_persons": len(persons),
        "issued_share": round(100 * issued / total) if total else 0,
        "posted": docs.get("posted", 0), "drafts": docs.get("draft", 0), "year": year,
        "month_docs": month_docs,
        "by_unit": unit_bars if len(unit_bars) > 1 else [],
        "by_class": _bars([(CLASS_TITLES.get(c, c), v) for c, v in by_class.items()], total),
    }


def reminders(conn: sqlite3.Connection, storage=None, today: date | None = None) -> list[Reminder]:
    today = today or date.today()
    result: list[Reminder] = []
    if storage is not None and storage.unsynced:
        result.append(Reminder("critical", "Флешку не знайдено — зміни поки що лише на цьому ПК. "
                                           "Поверніть флешку, не вимикаючи програму."))
    if not settings.is_complete(conn):
        result.append(Reminder("critical", "Не заповнено налаштування: реквізити частини і хто веде облік. "
                                           "Без них документи й звіти будуть без шапки.",
                               "refs.settings_view", link_text="Заповнити"))
    if not onboarding.is_started(conn):
        result.append(Reminder("warning", "Облік ще не розпочато: пройдіть кроки «Початок роботи» — "
                                          "головне, внести початкові залишки.",
                               "main.start", link_text="Почати"))

    no_mvo = conn.execute("SELECT COUNT(*) FROM units WHERE is_accounting = 1 AND is_active = 1 "
                          "AND mvo_person_id IS NULL").fetchone()[0]
    if no_mvo:
        result.append(Reminder("warning", f"Місць обліку без МВО: {no_mvo}.",
                               "refs.list_view", {"kind": "units"}))

    old = (today - timedelta(days=STALE_DRAFT_DAYS)).isoformat()
    stale = conn.execute("SELECT COUNT(*) FROM documents WHERE status = 'draft' AND created_at < ?",
                         (old,)).fetchone()[0]
    drafts = conn.execute("SELECT COUNT(*) FROM documents WHERE status = 'draft'").fetchone()[0]
    if stale:
        result.append(Reminder("warning", f"Чернеток, яким більше {STALE_DRAFT_DAYS} днів: {stale}. "
                                          "Проведіть їх або анулюйте — інакше залишки не відповідають дійсності.",
                               "documents.index", {"status": "draft", "year": today.year}))
    elif drafts:
        result.append(Reminder("info", f"Чернеток: {drafts} — ще не впливають на залишки.",
                               "documents.index", {"status": "draft", "year": today.year}))

    wrong = conn.execute(
        "SELECT COUNT(*) FROM documents d JOIN locations l ON l.id = d.to_location_id "
        "WHERE d.doc_type = 'opening' AND d.status = 'posted' AND d.reversed_by_id IS NULL "
        "AND l.kind = 'person' AND l.person_id IN "
        "(SELECT mvo_person_id FROM units WHERE is_accounting = 1)").fetchone()[0]
    if wrong:
        result.append(Reminder("warning", f"Початкові залишки внесено на МВО як на особу (документів: {wrong}). "
                                          "Майно під звітом має рахуватися на місці обліку.",
                               "documents.index", {"type": "opening", "year": today.year}))

    late = orders.overdue(conn, today)
    if late:
        result.append(Reminder("warning", f"Нарядів, строк дії яких минув, а виконання не відмічено: {len(late)}. "
                                          "Якщо майно вже видано — оформіть документ за нарядом або "
                                          "відмітьте виконання на сторінці наряду.",
                               "documents.index", {"exec": "overdue"}, link_text="Переглянути"))
    soon = orders.overdue(conn, today, soon_days=ORDER_SOON_DAYS)
    if soon:
        result.append(Reminder("info", f"Строк дії невиконаних нарядів спливає найближчими днями: {len(soon)} "
                                       f"(найближчий — до {fmt_date(soon[0]['valid_until'])}).",
                               "documents.index", {"exec": "open"}, link_text="Переглянути"))

    s = settings.load(conn)
    signers = {p for p in (s.get("approver_person_id"), s.get("service_head_person_id")) if p}
    signers |= {r[0] for r in conn.execute(
        "SELECT mvo_person_id FROM units WHERE is_accounting = 1 AND mvo_person_id IS NOT NULL")}
    if signers:
        q = ",".join("?" * len(signers))
        incomplete = conn.execute(f"SELECT COUNT(*) FROM persons WHERE id IN ({q}) "
                                  "AND (rank IS NULL OR rank = '' OR position IS NULL OR position = '')",
                                  list(signers)).fetchone()[0]
        if incomplete:
            result.append(Reminder("info", f"У тих, хто підписує документи (МВО, командир, начальник служби), "
                                           f"не вказано звання чи посаду: {incomplete}. Вони потрібні для підписів.",
                                   "refs.list_view", {"kind": "persons"}))

    # Кінець / початок місяця — час підбити підсумки (дод. 1, п. 7 розд. II).
    if onboarding.is_started(conn) and (today.day >= 25 or today.day <= 5):
        start = today.replace(day=1)
        if today.day <= 5:   # на початку місяця — за попередній
            end = start - timedelta(days=1)
            start = end.replace(day=1)
        else:
            end = today
        result.append(Reminder("info", "Кінець звітного періоду: складіть і підпишіть узагальнюючу "
                                       "відомість (дод. 1) за місяць.", "reports.summary",
                               {"from": start.isoformat(), "to": end.isoformat()}, link_text="Скласти"))
    order = {"critical": 0, "warning": 1, "info": 2}
    result.sort(key=lambda r: order[r.level])
    return result
