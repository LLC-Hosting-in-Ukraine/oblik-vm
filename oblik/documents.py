"""Документи руху майна: чернетка → проведено (або анульовано); сторно.

Правила:
  * майно рухається тільки через проведені документи;
  * при проведенні кожен рядок стає записом у журналі руху «звідки → куди»;
  * проведений документ не змінюється (це перевіряє і програма, і сама база);
  * помилку в проведеному документі виправляє сторнувальний документ —
    дзеркальний рух, після якого можна оформити правильний документ;
  * чернетку не видаляють, а анулюють, щоб у реєстраційних номерах не було пропусків.

Функції не фіксують транзакцію — це робить той, хто викликає.
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime

from . import audit, balances, locations
from .balances import Key
from .textutil import format_money, format_qty

CATEGORY_TITLES = {1: "I", 2: "II", 3: "III", 4: "IV", 5: "V"}


@dataclass(frozen=True)
class DocType:
    code: str
    title: str
    from_kinds: tuple[str, ...]
    to_kinds: tuple[str, ...]
    from_system: str | None = None
    to_system: str | None = None


DOC_TYPES: dict[str, DocType] = {t.code: t for t in [
    DocType("opening", "Введення початкових залишків", ("system",), ("unit", "person"),
            from_system=locations.OPENING),
    DocType("receipt", "Надходження", ("counterparty",), ("unit", "person")),
    DocType("transfer", "Переміщення (видача, повернення)", ("unit", "person"), ("unit", "person")),
    DocType("dispatch", "Передача за межі частини", ("unit", "person"), ("counterparty",)),
    DocType("writeoff", "Списання", ("unit", "person"), ("system",), to_system=locations.WRITTEN_OFF),
    DocType("storno", "Сторно", ("unit", "person", "counterparty", "system"),
            ("unit", "person", "counterparty", "system")),
    # Розпорядчий документ: майна не рухає (п. 6–7 розд. III Інструкції).
    DocType("order", "Наряд на видавання (приймання)", ("unit", "counterparty"), ("unit", "counterparty")),
    # Дод. 1 до Порядку списання (наказ МОУ № 81): списати (рядки без напряму → «Списано») і
    # оприбуткувати (рядки direction = 'in' ← «Оприбутковано за актом») на тому самому місці обліку.
    DocType("condition", "Акт якісного (технічного) стану", ("unit",), ("system",),
            to_system=locations.WRITTEN_OFF),
]}

# Документи, у яких бувають рядки «оприбуткувати» (direction = 'in') — і їх сторно.
TWO_WAY_TYPES = {"condition"}

# Типи, що не рухають майно (лише розпорядження; рух — за документами, оформленими на їх підставі).
NO_MOVEMENT_TYPES = {"order"}

# Документи, які оформлюють за нарядом (`order_id`): рух майна між сторонами наряду.
LINKABLE_TYPES = ("receipt", "transfer", "dispatch")

# Додаткові реквізити наряду (дод. 5) — зберігаються в extra_json.
ORDER_EXTRA = {
    "transport_kind": "Вид транспорту",
    "transport_no": "Номер транспорту",
    "transport_doc_name": "Найменування транспортного документа",
    "transport_doc_no": "Номер транспортного документа",
    "dispatch_order": "Порядок відправлення",
    "guard_term": "Строк прибуття варти з приймальником",
    "guard_unit": "Від військової частини (варта)",
}

# Реквізити акта якісного (технічного) стану (дод. 1 до Порядку списання) — у extra_json.
CONDITION_EXTRA = {
    "group_name": "Найменування майна (у заголовку акта)",
    "commission_conclusion": "Висновок комісії",
    "senior_conclusion": "Висновок старшого начальника",
}
# Особи акта (id з довідника «Особи») — теж у extra_json.
CONDITION_PERSONS = {
    "commission_head": "Голова комісії",
    "senior_person": "Старший начальник (підписує висновок)",
}

STATUS_TITLES = {"draft": "Чернетка", "posted": "Проведено", "cancelled": "Анульовано"}

HEADER_LABELS = {
    "doc_no": "Номер документа",
    "reg_no": "Реєстраційний номер",
    "doc_date": "Дата документа",
    "op_date": "Дата операції",
    "valid_until": "Дійсний до",
    "from_location_id": "Звідки",
    "to_location_id": "Куди",
    "basis": "Підстава",
    "service_id": "Служба",
    "recipient_person_id": "Відповідальний одержувач",
    "note": "Примітка",
    "order_id": "За нарядом",
    "extra_json": "Додаткові реквізити",
}
HEADER_FIELDS = list(HEADER_LABELS)


class DocumentError(Exception):
    """Помилки документа. errors — список зрозумілих користувачу повідомлень."""

    def __init__(self, errors: list[str] | str):
        self.errors = [errors] if isinstance(errors, str) else list(errors)
        super().__init__("\n".join(self.errors))


def _today() -> str:
    return date.today().isoformat()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _check_date(value: str | None, label: str, errors: list[str], required: bool = True) -> None:
    if not value:
        if required:
            errors.append(f"Не вказано {label}")
        return
    try:
        date.fromisoformat(value)
    except ValueError:
        errors.append(f"Невірна {label}: «{value}»")


def fmt_date(value: str | None) -> str:
    return date.fromisoformat(value).strftime("%d.%m.%Y") if value else ""


# --- Читання ------------------------------------------------------------------

def get(conn: sqlite3.Connection, doc_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
    if row is None:
        raise DocumentError("Документ не знайдено")
    return row


def lines(conn: sqlite3.Connection, doc_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM document_lines WHERE document_id = ? ORDER BY line_no", (doc_id,)
    ).fetchall()


def label(conn: sqlite3.Connection, doc_id: int) -> str:
    d = get(conn, doc_id)
    return f"{DOC_TYPES[d['doc_type']].title} № {d['doc_no']} від {fmt_date(d['doc_date'])}"


# --- Чернетка -----------------------------------------------------------------

def next_reg_no(conn: sqlite3.Connection, year: int) -> int:
    return conn.execute(
        "SELECT COALESCE(MAX(reg_no), 0) + 1 FROM documents WHERE reg_year = ?", (year,)
    ).fetchone()[0]


def create_draft(
    conn: sqlite3.Connection,
    doc_type: str,
    from_location_id: int,
    to_location_id: int,
    doc_date: str | None = None,
    op_date: str | None = None,
    **header,
) -> int:
    """Створити чернетку. Реєстраційний номер — наступний у межах року дати документа."""
    if doc_type not in DOC_TYPES or doc_type == "storno":
        raise DocumentError(f"Невідомий тип документа: {doc_type}")
    doc_date = doc_date or _today()
    errors: list[str] = []
    _check_date(doc_date, "дата документа", errors)
    _check_date(op_date, "дата операції", errors, required=False)
    if errors:
        raise DocumentError(errors)
    year = date.fromisoformat(doc_date).year
    reg_no = next_reg_no(conn, year)
    cur = conn.execute(
        "INSERT INTO documents(doc_type, reg_year, reg_no, doc_no, doc_date, op_date, "
        "from_location_id, to_location_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (doc_type, year, reg_no, str(reg_no), doc_date, op_date or doc_date,
         from_location_id, to_location_id, _now()),
    )
    doc_id = cur.lastrowid
    if header:
        _apply_header(conn, doc_id, header)
    audit.write(conn, "create", "documents", doc_id, label(conn, doc_id))
    return doc_id


def update_draft(conn: sqlite3.Connection, doc_id: int, **header) -> None:
    """Змінити реквізити чернетки (у т.ч. номер — «за необхідності правити»)."""
    doc = get(conn, doc_id)
    if doc["status"] != "draft":
        raise DocumentError("Змінювати можна тільки чернетку")
    before = dict(doc)
    _apply_header(conn, doc_id, header)
    after = dict(get(conn, doc_id))
    def shown(key, value):
        if value is None:
            return ""
        if key in ("from_location_id", "to_location_id"):
            return locations.title(conn, value)
        if key in ("doc_date", "op_date", "valid_until"):
            return fmt_date(value)
        if key == "order_id":
            return label(conn, value)
        return str(value)

    changes = [{"field": k, "label": HEADER_LABELS[k],
                "old": shown(k, before[k]), "new": shown(k, after[k])}
               for k in HEADER_FIELDS if before[k] != after[k]]
    if changes:
        audit.write(conn, "update", "documents", doc_id, label(conn, doc_id), changes)


def _apply_header(conn: sqlite3.Connection, doc_id: int, header: dict) -> None:
    unknown = set(header) - set(HEADER_FIELDS)
    if unknown:
        raise DocumentError(f"Невідомі реквізити: {', '.join(sorted(unknown))}")
    doc = get(conn, doc_id)
    errors: list[str] = []
    if "doc_date" in header:
        _check_date(header["doc_date"], "дата документа", errors)
    if "op_date" in header:
        _check_date(header["op_date"], "дата операції", errors)
    if header.get("valid_until"):
        _check_date(header["valid_until"], "дата «дійсний до»", errors)
    if "reg_no" in header:
        reg_no = header["reg_no"]
        if not isinstance(reg_no, int) or reg_no <= 0:
            errors.append("Реєстраційний номер має бути додатним цілим числом")
        else:
            year = date.fromisoformat(header.get("doc_date") or doc["doc_date"]).year
            clash = conn.execute(
                "SELECT doc_no FROM documents WHERE reg_year = ? AND reg_no = ? AND id <> ?",
                (year, reg_no, doc_id)).fetchone()
            if clash:
                errors.append(f"Реєстраційний номер {reg_no} за {year} рік уже зайнятий")
    if "doc_no" in header and not str(header["doc_no"] or "").strip():
        errors.append("Номер документа не може бути порожнім")
    if isinstance(header.get("extra_json"), dict):
        header = {**header, "extra_json": json.dumps(header["extra_json"], ensure_ascii=False)}
    if errors:
        raise DocumentError(errors)

    if "doc_date" in header:
        new_year = date.fromisoformat(header["doc_date"]).year
        if new_year != doc["reg_year"] and "reg_no" not in header:
            # Документ переїхав в інший рік — отримує наступний номер того року.
            header = {**header, "reg_no": next_reg_no(conn, new_year)}
            if doc["doc_no"] == str(doc["reg_no"]):
                header.setdefault("doc_no", str(header["reg_no"]))
        header = {**header, "reg_year": new_year}
    cols = list(header)
    conn.execute(f"UPDATE documents SET {', '.join(f'{c} = ?' for c in cols)} WHERE id = ?",
                 [header[c] for c in cols] + [doc_id])


def set_lines(conn: sqlite3.Connection, doc_id: int, new_lines: list[dict]) -> None:
    """Замінити всі рядки чернетки.

    Рядок: nomenclature_id, qty_m, price_kop, [item_id, category, qty_requested_m, batch_no, note,
    direction ('in' — оприбуткувати, лише в акті якісного стану), years_norm, years_fact].
    """
    if get(conn, doc_id)["status"] != "draft":
        raise DocumentError("Рядки можна змінювати тільки в чернетці")
    conn.execute("DELETE FROM document_lines WHERE document_id = ?", (doc_id,))
    conn.executemany(
        "INSERT INTO document_lines(document_id, line_no, nomenclature_id, item_id, qty_requested_m, "
        "qty_m, price_kop, category, batch_no, note, direction, years_norm, years_fact) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(doc_id, n, ln["nomenclature_id"], ln.get("item_id"), ln.get("qty_requested_m"),
          ln["qty_m"], ln["price_kop"], ln.get("category"), ln.get("batch_no"), ln.get("note"),
          ln.get("direction"), ln.get("years_norm"), ln.get("years_fact"))
         for n, ln in enumerate(new_lines, start=1)],
    )


def cancel(conn: sqlite3.Connection, doc_id: int) -> None:
    """Анулювати чернетку (номер лишається в реєстрі з позначкою «анульовано»)."""
    if get(conn, doc_id)["status"] != "draft":
        raise DocumentError("Анулювати можна тільки чернетку")
    conn.execute("UPDATE documents SET status = 'cancelled' WHERE id = ?", (doc_id,))
    audit.write(conn, "cancel", "documents", doc_id, label(conn, doc_id))


# --- Перевірка ----------------------------------------------------------------

def _nom(conn, nomenclature_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM nomenclature WHERE id = ?", (nomenclature_id,)).fetchone()


def _key_title(conn, key: Key) -> str:
    nom = _nom(conn, key.nomenclature_id)
    parts = [f"«{nom['name']}»"]
    if key.item_id is not None:
        item = conn.execute("SELECT serial_no, inventory_no FROM items WHERE id = ?",
                            (key.item_id,)).fetchone()
        parts.append(f"№ {item['inventory_no'] or item['serial_no']}")
    if key.category is not None:
        parts.append(f"кат. {CATEGORY_TITLES[key.category]}")
    parts.append(f"ціна {format_money(key.price_kop)}")
    return ", ".join(parts)


def validate(conn: sqlite3.Connection, doc_id: int) -> list[str]:
    """Усі причини, з яких документ не можна провести (порожній список — можна)."""
    doc = get(conn, doc_id)
    errors: list[str] = []
    if doc["status"] != "draft":
        return [f"Документ уже має статус «{STATUS_TITLES[doc['status']]}»"]

    dtype = DOC_TYPES[doc["doc_type"]]
    _check_date(doc["op_date"], "дата операції", errors)
    _check_date(doc["doc_date"], "дата документа", errors)
    if errors:
        return errors
    op_date = doc["op_date"]

    src, dst = locations.get(conn, doc["from_location_id"]), locations.get(conn, doc["to_location_id"])
    if src["kind"] not in dtype.from_kinds or (dtype.from_system and src["system_code"] != dtype.from_system):
        errors.append(f"«{dtype.title}»: невірне місце «Звідки» ({locations.title(conn, src['id'])})")
    if dst["kind"] not in dtype.to_kinds or (dtype.to_system and dst["system_code"] != dtype.to_system):
        errors.append(f"«{dtype.title}»: невірне місце «Куди» ({locations.title(conn, dst['id'])})")

    errors += check_order_link(conn, doc)
    doc_lines = lines(conn, doc_id)
    if not doc_lines:
        errors.append("У документі немає жодного рядка")
    if doc["doc_type"] in NO_MOVEMENT_TYPES:
        return errors + _validate_order_lines(conn, doc_lines)

    two_way = doc["doc_type"] in TWO_WAY_TYPES or (
        doc["reversal_of_id"] is not None and get(conn, doc["reversal_of_id"])["doc_type"] in TWO_WAY_TYPES)
    if not two_way and any(ln["direction"] for ln in doc_lines):
        errors.append("Рядки «оприбуткувати» бувають лише в акті якісного (технічного) стану")
    if doc["doc_type"] == "condition" and doc_lines and all(ln["direction"] for ln in doc_lines):
        errors.append("В акті якісного стану немає майна, що списується (графи 1–10)")

    need: dict[tuple[int, Key], int] = defaultdict(int)       # (звідки, ключ) → скільки
    incoming: dict[tuple[int, Key], int] = {}                  # поштучне, що надходить ззовні
    first_line: dict[tuple[int, Key], int] = {}
    seen_items: set[int] = set()
    for ln in doc_lines:
        n = ln["line_no"]
        nom = _nom(conn, ln["nomenclature_id"])
        if nom is None:
            errors.append(f"Рядок {n}: номенклатуру не знайдено")
            continue
        if nom["serial_tracked"]:
            if ln["item_id"] is None:
                errors.append(f"Рядок {n}: «{nom['name']}» обліковується поштучно — оберіть одиницю")
                continue
            item = conn.execute("SELECT * FROM items WHERE id = ?", (ln["item_id"],)).fetchone()
            if item is None or item["nomenclature_id"] != nom["id"]:
                errors.append(f"Рядок {n}: одиниця не відповідає найменуванню «{nom['name']}»")
                continue
            if ln["qty_m"] != 1000:
                errors.append(f"Рядок {n}: для поштучної одиниці кількість має бути 1")
            if item["id"] in seen_items:
                errors.append(f"Рядок {n}: одиниця вже є в іншому рядку цього документа")
            seen_items.add(item["id"])
        elif ln["item_id"] is not None:
            errors.append(f"Рядок {n}: «{nom['name']}» не обліковується поштучно — одиницю не вказують")
        if nom["is_categorized"] and ln["category"] is None:
            errors.append(f"Рядок {n}: вкажіть категорію для «{nom['name']}»")
        if not nom["is_categorized"] and ln["category"] is not None:
            errors.append(f"Рядок {n}: «{nom['name']}» обліковується без категорій")
        key = Key(ln["nomenclature_id"], ln["item_id"], ln["category"], ln["price_kop"])
        src_id, dst_id = route(conn, doc, ln)
        if locations.get(conn, src_id)["kind"] in locations.INTERNAL_KINDS:
            need[(src_id, key)] += ln["qty_m"]
            first_line.setdefault((src_id, key), n)
        elif locations.get(conn, dst_id)["kind"] in locations.INTERNAL_KINDS and key.item_id is not None:
            incoming[(dst_id, key)] = n

    if errors:
        return errors

    for (src_id, key), qty in need.items():
        n = first_line[(src_id, key)]
        have = balances.available(conn, src_id, key, op_date)
        if have < qty:
            errors.append(
                f"Рядок {n}: {_key_title(conn, key)} — у «{locations.title(conn, src_id)}» на {fmt_date(op_date)} "
                f"(з урахуванням пізніших документів) доступно {format_qty(max(have, 0))}, "
                f"потрібно {format_qty(qty)}")
    for (_, key), n in incoming.items():
        # Поштучна одиниця надходить ззовні — її не повинно вже бути на обліку.
        pos = balances.item_position(conn, key.item_id, op_date)
        if pos is not None:
            errors.append(f"Рядок {n}: {_key_title(conn, key)} уже на обліку в "
                          f"«{locations.title(conn, pos['location_id'])}»")
        elif balances.item_has_movements_after(conn, key.item_id, op_date):
            errors.append(f"Рядок {n}: по одиниці {_key_title(conn, key)} є рух після "
                          f"{fmt_date(op_date)} — оформіть документ пізнішою датою")
    return errors


def route(conn: sqlite3.Connection, doc, ln) -> tuple[int, int]:
    """Звідки → куди рухається рядок. Звичайно — як у шапці документа. Рядок «оприбуткувати»
    (direction = 'in', акт якісного стану) — навпаки, і замість «Списано» — «Оприбутковано за
    актом»: в акті він надходить на місце обліку, а в його сторно — повертається туди."""
    a, b = doc["from_location_id"], doc["to_location_id"]
    if not ln["direction"]:
        return a, b
    written_off, obtained = locations.system(conn, locations.WRITTEN_OFF), locations.system(conn, locations.OBTAINED)
    swap = lambda x: obtained if x == written_off else x  # noqa: E731
    return swap(b), swap(a)


def _validate_order_lines(conn, doc_lines) -> list[str]:
    """Наряд: рядки — що видати; залишків не перевіряємо (майно ще не рухається),
    поштучні одиниці не вказують (номери — в акті, оформленому за нарядом)."""
    errors = []
    for ln in doc_lines:
        n = ln["line_no"]
        nom = _nom(conn, ln["nomenclature_id"])
        if nom is None:
            errors.append(f"Рядок {n}: номенклатуру не знайдено")
            continue
        if ln["item_id"] is not None:
            errors.append(f"Рядок {n}: у наряді поштучні одиниці не вказують — лише найменування й кількість")
        if nom["is_categorized"] and ln["category"] is None:
            errors.append(f"Рядок {n}: вкажіть категорію для «{nom['name']}»")
    return errors


def check_order_link(conn: sqlite3.Connection, doc) -> list[str]:
    """Помилки посилання на наряд — з ними документ не провести. Розбіжності з нарядом
    (кількість, сторони, строк) — лише попередження: `orders.warnings`."""
    if doc["order_id"] is None:
        return []
    if doc["doc_type"] not in LINKABLE_TYPES:
        return [f"«{DOC_TYPES[doc['doc_type']].title}» не оформлюють за нарядом"]
    order = conn.execute("SELECT * FROM documents WHERE id = ?", (doc["order_id"],)).fetchone()
    if order is None or order["doc_type"] != "order":
        return ["Наряд, за яким оформлено документ, не знайдено"]
    if order["status"] != "posted":
        return [f"Наряд № {order['doc_no']} ще не підписано (не проведено) — "
                "за чернеткою чи анульованим нарядом майно не видають"]
    return []


def extra(doc) -> dict:
    """Усі додаткові реквізити документа (extra_json)."""
    try:
        data = json.loads(doc["extra_json"] or "{}")
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


def condition_extra(doc) -> dict:
    """Реквізити акта якісного стану: тексти, голова й члени комісії, старший начальник."""
    data = extra(doc)
    result = {k: data.get(k, "") for k in CONDITION_EXTRA}
    for k in CONDITION_PERSONS:
        result[k] = data.get(k) if isinstance(data.get(k), int) else None
    members = data.get("commission_members")
    result["commission_members"] = [m for m in members if isinstance(m, int)] if isinstance(members, list) else []
    return result


def order_extra(doc) -> dict:
    """Додаткові реквізити наряду з extra_json."""
    data = extra(doc)
    return {k: data.get(k, "") for k in ORDER_EXTRA}


# --- Примірники ---------------------------------------------------------------

def _copy_party(conn, location_id: int, other_internal: bool) -> str | None:
    """Кому примірник за стороною документа: контрагент — його назва; своє місце — наша частина
    (а якщо обидві сторони свої — саме місце: склад, підрозділ, особа)."""
    loc = locations.get(conn, location_id)
    if loc["kind"] == "counterparty":
        return locations.title(conn, location_id)
    if loc["kind"] in locations.INTERNAL_KINDS:
        if not other_internal:
            from . import settings
            s = settings.load(conn)
            own = s.get("subunit_name") or s.get("unit_name")
            if own:
                return own
        return locations.title(conn, location_id, with_owner=False)
    return None   # службові місця (початкові залишки, списано)


def default_copies(conn: sqlite3.Connection, doc) -> list[str]:
    """За замовчуванням — 2 примірники: тому, хто передає (вантажовідправнику), і тому,
    хто приймає (вантажоодержувачу)."""
    src_int = locations.kind(conn, doc["from_location_id"]) in locations.INTERNAL_KINDS
    dst_int = locations.kind(conn, doc["to_location_id"]) in locations.INTERNAL_KINDS
    parties = [_copy_party(conn, doc["from_location_id"], dst_int),
               _copy_party(conn, doc["to_location_id"], src_int)]
    result = []
    for p in parties:
        if p and p not in result:
            result.append(p)
    return result


def copies(conn: sqlite3.Connection, doc) -> list[str]:
    """Кому примірники: список, заданий користувачем, або за замовчуванням."""
    custom = extra(doc).get("copies")
    if isinstance(custom, list) and custom:
        return [str(c) for c in custom]
    return default_copies(conn, doc)


def copies_text(names: list[str]) -> str:
    """«Виконано у 2 примірниках:» / «Примірник № 1 – до …;» / «Примірник № 2 – до ….» — по рядках."""
    if not names:
        return ""
    n = len(names)
    lines = [f"Виконано у {n} {'примірнику' if n == 1 else 'примірниках'}:"]
    for i, name in enumerate(names, 1):
        # «до …» не дописуємо: назви не відмінюються автоматично («до в/ч А0000» впише користувач).
        lines.append(f"Примірник № {i} – {name}{'.' if i == n else ';'}")
    return "\n".join(lines)


# --- Проведення й сторно ------------------------------------------------------

def post(conn: sqlite3.Connection, doc_id: int) -> None:
    errors = validate(conn, doc_id)
    if errors:
        raise DocumentError(errors)
    doc = get(conn, doc_id)
    if doc["doc_type"] in NO_MOVEMENT_TYPES:
        # Наряд: «проведено» = підписано й зареєстровано; руху немає.
        conn.execute("UPDATE documents SET status = 'posted', posted_at = ? WHERE id = ?", (_now(), doc_id))
        audit.write(conn, "post", "documents", doc_id, label(conn, doc_id))
        return
    conn.execute("SAVEPOINT post_doc")
    try:
        conn.executemany(
            "INSERT INTO movements(document_id, line_id, op_date, from_location_id, to_location_id, "
            "nomenclature_id, item_id, category, price_kop, qty_m) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(doc_id, ln["id"], doc["op_date"], *route(conn, doc, ln),
              ln["nomenclature_id"], ln["item_id"], ln["category"], ln["price_kop"], ln["qty_m"])
             for ln in lines(conn, doc_id)],
        )
        conn.execute("UPDATE documents SET status = 'posted', posted_at = ? WHERE id = ?",
                     (_now(), doc_id))
        if doc["reversal_of_id"] is not None:
            conn.execute("UPDATE documents SET reversed_by_id = ? WHERE id = ?",
                         (doc_id, doc["reversal_of_id"]))
    except Exception:
        conn.execute("ROLLBACK TO post_doc")
        conn.execute("RELEASE post_doc")
        raise
    conn.execute("RELEASE post_doc")
    audit.write(conn, "post", "documents", doc_id, label(conn, doc_id))


def reverse(conn: sqlite3.Connection, doc_id: int, on_date: str | None = None,
            basis: str | None = None) -> int:
    """Сторнувати проведений документ: створити й провести дзеркальний документ.

    on_date — дата сторно (за замовчуванням сьогодні), не раніше дати операції оригіналу.
    """
    doc = get(conn, doc_id)
    if doc["status"] != "posted":
        raise DocumentError("Сторнувати можна тільки проведений документ")
    if doc["doc_type"] == "storno":
        raise DocumentError("Сторнувальний документ не сторнується. "
                            "Оформіть потрібний рух новим документом.")
    if doc["doc_type"] in NO_MOVEMENT_TYPES:
        raise DocumentError("Наряд майна не рухав — сторнувати нічого. Якщо його скасовано, "
                            "оформіть новий наряд; невиконаний наряд втрачає силу після строку дії.")
    if doc["reversed_by_id"] is not None:
        raise DocumentError(f"Документ уже сторновано: {label(conn, doc['reversed_by_id'])}")
    on_date = on_date or _today()
    errors: list[str] = []
    _check_date(on_date, "дата сторно", errors)
    if not errors and on_date < doc["op_date"]:
        errors.append(f"Дата сторно не може бути раніше дати операції "
                      f"документа ({fmt_date(doc['op_date'])})")
    if errors:
        raise DocumentError(errors)

    year = date.fromisoformat(on_date).year
    reg_no = next_reg_no(conn, year)
    cur = conn.execute(
        "INSERT INTO documents(doc_type, reg_year, reg_no, doc_no, doc_date, op_date, "
        "from_location_id, to_location_id, basis, service_id, reversal_of_id, created_at) "
        "VALUES ('storno', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (year, reg_no, str(reg_no), on_date, on_date,
         doc["to_location_id"], doc["from_location_id"],
         basis or f"Сторно: {label(conn, doc_id)}", doc["service_id"], doc_id, _now()),
    )
    storno_id = cur.lastrowid
    set_lines(conn, storno_id, [dict(ln) for ln in lines(conn, doc_id)])
    errors = validate(conn, storno_id)
    if errors:
        # Чернетку сторно не лишаємо: рух уже пішов далі, спершу сторнуйте пізніші документи.
        conn.execute("DELETE FROM documents WHERE id = ?", (storno_id,))
        raise DocumentError(
            ["Сторно неможливе: майно з цього документа вже рушило далі. "
             "Спершу сторнуйте пізніші документи."] + errors)
    audit.write(conn, "create", "documents", storno_id, label(conn, storno_id))
    post(conn, storno_id)
    audit.write(conn, "reverse", "documents", doc_id, label(conn, doc_id),
                [{"field": "reversed_by_id", "label": "Сторновано документом",
                  "old": "", "new": label(conn, storno_id)}])
    return storno_id
