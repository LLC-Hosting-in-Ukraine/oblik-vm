"""Виконання нарядів: що видано за нарядом і за якими документами (графи 8–9 дод. 5).

Наряд майна не рухає (п. 7 розд. III Інструкції): рух оформлюють накладна чи акт, у яких
`order_id` посилається на наряд. Виконане рахується з проведених і не сторнованих документів.
Порядку «контролю виконання» Інструкція не встановлює — це допоміжна функція програми.

© 2026 Yevhenii Hosting by LLC Hosting in Ukraine
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date

from . import audit, balances, documents, locations
from .documents import DocumentError, fmt_date
from .textutil import format_qty

STATE_TITLES = {"none": "Не виконано", "partial": "Виконано частково", "done": "Виконано"}

# Коротка назва паперового документа для графи 9 наряду.
_PAPER_TITLES = {"receipt": "Акт приймання-передачі", "transfer": "Накладна", "dispatch": "Накладна"}


@dataclass
class LineExecution:
    line: sqlite3.Row                 # рядок наряду
    ordered_m: int
    fact_m: int = 0
    docs: list[tuple[int | None, str]] = field(default_factory=list)   # (id, «Накладна № 5 від …»); None — ручна відмітка

    @property
    def remaining_m(self) -> int:
        return max(self.ordered_m - self.fact_m, 0)


@dataclass
class Execution:
    lines: list[LineExecution]
    state: str                        # none | partial | done
    expired: bool                     # строк дії минув, а наряд не виконано
    extra: list[tuple[str, int, str]]  # рядки документів, яких немає в наряді: (документ, № рядка, назва)

    @property
    def state_title(self) -> str:
        return STATE_TITLES[self.state]


def doc_type_for(conn: sqlite3.Connection, order) -> str | None:
    """Яким документом виконати наряд у програмі. None — наряд виконують інші частини між собою."""
    src = locations.get(conn, order["from_location_id"])["kind"]
    dst = locations.get(conn, order["to_location_id"])["kind"]
    return {("unit", "unit"): "transfer", ("unit", "counterparty"): "dispatch",
            ("counterparty", "unit"): "receipt"}.get((src, dst))


def paper_ref(doc) -> str:
    """«Накладна № 5 від 01.10.2026» — для графи 9 наряду."""
    return f"{_PAPER_TITLES.get(doc['doc_type'], 'Документ')} № {doc['doc_no']} від {fmt_date(doc['doc_date'])}"


def linked_docs(conn: sqlite3.Connection, order_id: int) -> list[sqlite3.Row]:
    """Усі документи за нарядом (і чернетки, і анульовані) — для переліку на сторінці наряду."""
    return conn.execute("SELECT * FROM documents WHERE order_id = ? ORDER BY doc_date, reg_no",
                        (order_id,)).fetchall()


def _counted_docs(conn, order_id: int, exclude_id: int | None = None) -> list[sqlite3.Row]:
    """Документи, що зараховуються у виконання: проведені й не сторновані."""
    return conn.execute(
        "SELECT * FROM documents WHERE order_id = ? AND status = 'posted' AND reversed_by_id IS NULL "
        "AND id IS NOT ? ORDER BY op_date, reg_no", (order_id, exclude_id)).fetchall()


def _find_line(result: list[LineExecution], ln) -> LineExecution | None:
    """Рядок наряду для рядка документа: те саме найменування (і категорія, якщо вказана);
    серед кількох — спершу з тією самою ціною, далі той, де ще лишилося видати."""
    same = [x for x in result if x.line["nomenclature_id"] == ln["nomenclature_id"]
            and (x.line["category"] is None or ln["category"] is None
                 or x.line["category"] == ln["category"])]
    if not same:
        return None
    exact = [x for x in same if x.line["price_kop"] == ln["price_kop"]]
    for group in (exact, same):
        for x in group:
            if x.remaining_m > 0:
                return x
    return (exact or same)[0]


def _allocate(conn, result: list[LineExecution], doc, extra: list) -> None:
    ref = paper_ref(doc)
    for ln in documents.lines(conn, doc["id"]):
        target = _find_line(result, ln)
        if target is None:
            name = conn.execute("SELECT name FROM nomenclature WHERE id = ?",
                                (ln["nomenclature_id"],)).fetchone()[0]
            extra.append((ref, ln["line_no"], name))
            continue
        target.fact_m += ln["qty_m"]
        if (doc["id"], ref) not in target.docs:
            target.docs.append((doc["id"], ref))


def execution(conn: sqlite3.Connection, order_id: int, today: date | None = None,
              exclude_id: int | None = None) -> Execution:
    """Виконання наряду по рядках. exclude_id — не враховувати цей документ."""
    order = documents.get(conn, order_id)
    result = [LineExecution(ln, ln["qty_m"]) for ln in documents.lines(conn, order_id)]
    extra: list = []
    for doc in _counted_docs(conn, order_id, exclude_id):
        _allocate(conn, result, doc, extra)
    by_line = {x.line["id"]: x for x in result}
    for m in marks(conn, order_id):
        x = by_line[m["line_id"]]
        x.fact_m += m["qty_m"]
        if (None, m["doc_ref"]) not in x.docs:
            x.docs.append((None, m["doc_ref"]))
    if result and all(x.remaining_m == 0 for x in result):
        state = "done"
    elif any(x.fact_m for x in result):
        state = "partial"
    else:
        state = "none"
    today = today or date.today()
    expired = (order["status"] == "posted" and state != "done" and bool(order["valid_until"])
               and order["valid_until"] < today.isoformat())
    return Execution(result, state, expired, extra)


# --- Ручні відмітки -------------------------------------------------------------

def marks(conn: sqlite3.Connection, order_id: int) -> list[sqlite3.Row]:
    """Ручні відмітки про виконання (наряд виконано поза програмою — напр., іншими частинами)."""
    return conn.execute("SELECT m.*, l.line_no FROM order_marks m JOIN document_lines l ON l.id = m.line_id "
                        "WHERE m.order_id = ? ORDER BY m.mark_date, m.id", (order_id,)).fetchall()


def _mark_summary(conn, m) -> list[dict]:
    return [{"field": "mark", "label": f"Відмітка про виконання, рядок {m['line_no']}",
             "old": "", "new": f"{format_qty(m['qty_m'])} — {m['doc_ref']}"}]


def add_mark(conn: sqlite3.Connection, order_id: int, line_id: int, qty_m: int, doc_ref: str,
             mark_date: str | None = None, note: str | None = None) -> int:
    """Відмітити, що за рядком наряду видано qty_m за документом doc_ref."""
    order = documents.get(conn, order_id)
    errors = []
    if order["doc_type"] != "order" or order["status"] != "posted":
        errors.append("Відмітку про виконання можна зробити лише в підписаному (проведеному) наряді")
    line = conn.execute("SELECT * FROM document_lines WHERE id = ? AND document_id = ?",
                        (line_id, order_id)).fetchone()
    if line is None:
        errors.append("Оберіть рядок наряду")
    if not qty_m or qty_m <= 0:
        errors.append("Вкажіть, скільки фактично видано (більше нуля)")
    doc_ref = " ".join((doc_ref or "").split())
    if not doc_ref:
        errors.append("Вкажіть документ, за яким видано: назву, номер і дату (напр., «Акт № 12 від 05.10.2026»)")
    mark_date = mark_date or date.today().isoformat()
    try:
        date.fromisoformat(mark_date)
    except ValueError:
        errors.append(f"Невірна дата відмітки: «{mark_date}»")
    if errors:
        raise DocumentError(errors)
    cur = conn.execute("INSERT INTO order_marks(order_id, line_id, qty_m, doc_ref, mark_date, note, created_at) "
                       "VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (order_id, line_id, qty_m, doc_ref, mark_date, (note or "").strip() or None,
                        documents._now()))
    m = conn.execute("SELECT m.*, l.line_no FROM order_marks m JOIN document_lines l ON l.id = m.line_id "
                     "WHERE m.id = ?", (cur.lastrowid,)).fetchone()
    audit.write(conn, "mark", "documents", order_id, documents.label(conn, order_id), _mark_summary(conn, m))
    return cur.lastrowid


def delete_mark(conn: sqlite3.Connection, mark_id: int) -> int:
    """Видалити помилкову відмітку (у журналі змін лишається запис). Повертає id наряду."""
    m = conn.execute("SELECT m.*, l.line_no FROM order_marks m JOIN document_lines l ON l.id = m.line_id "
                     "WHERE m.id = ?", (mark_id,)).fetchone()
    if m is None:
        raise DocumentError("Відмітку не знайдено")
    conn.execute("DELETE FROM order_marks WHERE id = ?", (mark_id,))
    changes = [{**c, "old": c["new"], "new": "видалено"} for c in _mark_summary(conn, m)]
    audit.write(conn, "unmark", "documents", m["order_id"], documents.label(conn, m["order_id"]), changes)
    return m["order_id"]


def overdue(conn: sqlite3.Connection, today: date | None = None, soon_days: int = 0) -> list[sqlite3.Row]:
    """Підписані невиконані наряди, строк дії яких минув (soon_days > 0 — спливає протягом стількох днів)."""
    today = today or date.today()
    limit = date.fromordinal(today.toordinal() + soon_days).isoformat()
    if soon_days:   # ще діє, але останній день — не пізніше ніж через soon_days
        where, params = "valid_until >= ? AND valid_until <= ?", (today.isoformat(), limit)
    else:           # строк дії минув
        where, params = "valid_until < ?", (today.isoformat(),)
    rows = conn.execute("SELECT * FROM documents WHERE doc_type = 'order' AND status = 'posted' "
                        f"AND valid_until IS NOT NULL AND {where} ORDER BY valid_until", params).fetchall()
    result = []
    for r in rows:
        if execution(conn, r["id"], today=today).state != "done":
            result.append(r)
    return result


def print_columns(conn: sqlite3.Connection, order_id: int) -> list[tuple[str, str]]:
    """Графи 8–9 для друку наряду по рядках: (фактично видано, за якими документами)."""
    ex = execution(conn, order_id)
    return [(format_qty(x.fact_m) if x.fact_m else "", "; ".join(ref for _, ref in x.docs))
            for x in ex.lines]


def order_options(conn: sqlite3.Connection, current: int | None = None) -> list[tuple[int, str]]:
    """Наряди для вибору в документі: підписані (проведені) й ще не виконані повністю + поточний."""
    rows = conn.execute("SELECT * FROM documents WHERE doc_type = 'order' AND status = 'posted' "
                        "ORDER BY doc_date DESC, reg_no DESC").fetchall()
    result = []
    for r in rows:
        if r["id"] != current and execution(conn, r["id"]).state == "done":
            continue
        result.append((r["id"], f"№ {r['doc_no']} від {fmt_date(r['doc_date'])}: "
                                f"{locations.title(conn, r['from_location_id'], with_owner=False)} → "
                                f"{locations.title(conn, r['to_location_id'], with_owner=False)}"))
    return result


def warnings(conn: sqlite3.Connection, doc_id: int) -> list[str]:
    """Попередження для документа за нарядом (провести можна). Порожньо — усе збігається."""
    doc = documents.get(conn, doc_id)
    if doc["order_id"] is None or documents.check_order_link(conn, doc):
        return []
    order = documents.get(conn, doc["order_id"])
    result = []
    if (doc["from_location_id"], doc["to_location_id"]) != (order["from_location_id"], order["to_location_id"]):
        result.append(
            f"Сторони не збігаються з нарядом: у наряді "
            f"{locations.title(conn, order['from_location_id'])} → {locations.title(conn, order['to_location_id'])}")
    if order["valid_until"] and doc["op_date"] > order["valid_until"]:
        result.append(f"Строк дії наряду закінчився {fmt_date(order['valid_until'])}. Майно, яке вже "
                      "фактично прибуло до вантажоодержувача, приймають і без продовження строку "
                      "(п. 9 розд. III Інструкції); в інших випадках потрібен новий наряд.")
    ex = execution(conn, order["id"], exclude_id=doc_id)
    before = {id(x): x.fact_m for x in ex.lines}
    extra: list = []
    _allocate(conn, ex.lines, doc, extra)
    result += [f"Рядок {n}: «{name}» — немає в наряді" for _, n, name in extra]
    for x in ex.lines:
        added = x.fact_m - before[id(x)]
        if added and x.fact_m > x.ordered_m:
            left = max(x.ordered_m - before[id(x)], 0)
            name = conn.execute("SELECT name, uom FROM nomenclature WHERE id = ?",
                                (x.line["nomenclature_id"],)).fetchone()
            result.append(f"«{name['name']}»: за нарядом лишилося видати {format_qty(left)} {name['uom']}, "
                          f"у документі — {format_qty(added)} {name['uom']}")
    return result


# --- Чернетка за нарядом --------------------------------------------------------

def _outgoing_lines(conn, from_loc: int, on_date: str, ln, need_m: int, serial: bool) -> list[dict]:
    """Рядки видачі з наявного залишку: потрібне найменування (категорія), спершу за ціною наряду.
    Чого не вистачає — окремим рядком за ціною наряду (програма покаже, що залишку немає)."""
    stock = [r for r in balances.balances(conn, [from_loc], on_date, nomenclature_id=ln["nomenclature_id"])
             if r["qty_m"] > 0 and (ln["category"] is None or r["category"] == ln["category"])]
    stock.sort(key=lambda r: (r["price_kop"] != ln["price_kop"], r["item_id"] or 0))
    result = []
    for r in stock:
        if need_m <= 0:
            break
        take = 1000 if serial else min(r["qty_m"], need_m)
        result.append({"nomenclature_id": ln["nomenclature_id"], "item_id": r["item_id"],
                       "category": r["category"], "price_kop": r["price_kop"], "qty_m": take})
        need_m -= take
    if need_m > 0:
        result.append({"nomenclature_id": ln["nomenclature_id"], "item_id": None,
                       "category": ln["category"], "price_kop": ln["price_kop"], "qty_m": need_m})
    return result


def create_draft(conn: sqlite3.Connection, order_id: int, on_date: str | None = None) -> int:
    """Чернетка накладної / акта за нарядом: ті самі сторони, рядки — що ще лишилося видати.
    Поштучне майно, яке видаємо, програма підбирає з залишку (одиниці можна замінити)."""
    order = documents.get(conn, order_id)
    if order["doc_type"] != "order" or order["status"] != "posted":
        raise DocumentError("Оформити документ можна лише за підписаним (проведеним) нарядом")
    doc_type = doc_type_for(conn, order)
    if doc_type is None:
        raise DocumentError("Цей наряд виконують інші частини між собою — документ про рух "
                            "у вашій програмі не оформлюється.")
    on_date = on_date or date.today().isoformat()
    ex = execution(conn, order_id)
    if ex.state == "done":
        raise DocumentError("Наряд уже виконано повністю")
    outgoing = locations.get(conn, order["from_location_id"])["kind"] == "unit"
    new_lines: list[dict] = []
    for x in ex.lines:
        if not x.remaining_m:
            continue
        serial = conn.execute("SELECT serial_tracked FROM nomenclature WHERE id = ?",
                              (x.line["nomenclature_id"],)).fetchone()[0]
        if outgoing:
            new_lines += _outgoing_lines(conn, order["from_location_id"], on_date, x.line,
                                         x.remaining_m, serial)
        else:
            base = {"nomenclature_id": x.line["nomenclature_id"], "item_id": None,
                    "category": x.line["category"], "price_kop": x.line["price_kop"]}
            if serial:   # поштучне — по рядку на одиницю: лишиться вписати номери
                new_lines += [{**base, "qty_m": 1000} for _ in range(x.remaining_m // 1000)]
            else:
                new_lines.append({**base, "qty_m": x.remaining_m})
    doc_id = documents.create_draft(
        conn, doc_type, order["from_location_id"], order["to_location_id"],
        doc_date=on_date, op_date=on_date, order_id=order_id,
        basis=f"Наряд № {order['doc_no']} від {fmt_date(order['doc_date'])}",
        service_id=order["service_id"], recipient_person_id=order["recipient_person_id"])
    documents.set_lines(conn, doc_id, new_lines)
    return doc_id
