"""Довідники: опис полів і спільна логіка (список, пошук, збереження, видалення, журнал).

Кожен довідник описується об'єктом Ref зі списком полів Field. Веб-інтерфейс
(web/refs.py) будує форми й таблиці з цих описів, тому новий довідник або нове
поле додається тут, без окремих HTML-сторінок.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from typing import Callable

from . import audit
from .textutil import format_money, format_qty, parse_money, parse_qty, person_short

# --- Варіанти вибору ----------------------------------------------------------

UNIT_KINDS = [
    ("military_unit", "Військова частина"),
    ("service", "Служба"),
    ("subunit", "Підрозділ"),
    ("warehouse", "Склад (сховище)"),
    ("other", "Інше"),
]

ROLES = [
    ("mvo", "МВО"),
    ("recipient", "Отримувач"),
    ("commission_head", "Голова комісії"),
    ("commission_member", "Член комісії"),
    ("approver", "Затверджує документи (командир)"),
    ("service_head", "Начальник служби"),
    ("finance", "Фінансово-економічний орган"),
]

COUNTERPARTY_KINDS = [
    ("higher_unit", "Вища військова частина / орган управління"),
    ("military_unit", "Інша військова частина"),
    ("supplier", "Постачальник (за договором)"),
    ("charity", "Благодійна допомога"),
    ("international", "Міжнародна технічна допомога"),
    ("authority", "Орган влади, територіальна громада"),
    ("other", "Інше"),
]

ACCOUNTING_CLASSES = [
    ("fixed", "Основні засоби"),
    ("low_value", "Малоцінні необоротні (МНМА)"),
    ("inventory", "Запаси (витратні, ЗІП, ПММ…)"),
    ("mshp", "МШП (малоцінні швидкозношувані)"),
    ("off_balance", "Забалансове"),
]

RANKS = [
    "солдат", "старший солдат", "молодший сержант", "сержант", "старший сержант",
    "головний сержант", "штаб-сержант", "майстер-сержант", "старший майстер-сержант",
    "головний майстер-сержант", "молодший лейтенант", "лейтенант", "старший лейтенант",
    "капітан", "майор", "підполковник", "полковник", "бригадний генерал",
    "генерал-майор", "генерал-лейтенант", "генерал",
    "матрос", "старший матрос", "капітан-лейтенант", "капітан 3 рангу",
    "капітан 2 рангу", "капітан 1 рангу",
    "працівник ЗСУ",
]

UOMS = ["шт.", "к-т", "од.", "пара", "м", "м²", "м³", "кг", "т", "л", "уп.", "рул.", "арк."]


class ValidationError(Exception):
    def __init__(self, errors: dict[str, str]):
        super().__init__("; ".join(errors.values()))
        self.errors = errors


class InUseError(Exception):
    """Запис використовується в інших записах і не може бути видалений."""


# --- Опис полів ---------------------------------------------------------------

@dataclass
class Field:
    name: str
    label: str
    kind: str = "text"  # text, textarea, int, money, bool, choice, fk, roles, components
    required: bool = False
    choices: list[tuple[str, str]] | None = None
    fk: str | None = None              # вид довідника, на який посилається поле
    suggest: list[str] | None = None   # підказки для введення (datalist)
    help: str = ""
    in_list: bool = False
    default: object = None
    term: str = ""                     # термін довідки для значка «?» (oblik/help.py)

    @property
    def stored(self) -> bool:
        """Чи є поле стовпцем таблиці (а не окремою таблицею)."""
        return self.kind not in ("roles", "components")


@dataclass
class Ref:
    kind: str           # адреса в інтерфейсі й назва в журналі змін
    table: str
    title: str          # «Підрозділи»
    item_title: str     # «підрозділ» (у знахідному відмінку: «Додати підрозділ»)
    fields: list[Field]
    order_sql: str
    search_sql: list[str]
    label: Callable[[sqlite3.Row, "Labels"], str]
    description: str = ""
    validate: Callable[[sqlite3.Connection, int | None, dict, dict], None] | None = None

    def field(self, name: str) -> Field:
        return next(f for f in self.fields if f.name == name)

    @property
    def stored_fields(self) -> list[Field]:
        return [f for f in self.fields if f.stored]

    @property
    def list_fields(self) -> list[Field]:
        return [f for f in self.fields if f.in_list]


class Labels:
    """Кеш підписів записів довідників (для полів-посилань)."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self._maps: dict[str, dict[int, str]] = {}

    def map(self, kind: str) -> dict[int, str]:
        if kind not in self._maps:
            ref = REFS[kind]
            rows = self.conn.execute(f"SELECT * FROM {ref.table}").fetchall()
            self._maps[kind] = {}  # захист від рекурсії
            self._maps[kind] = {r["id"]: ref.label(r, self) for r in rows}
        return self._maps[kind]

    def get(self, kind: str, id_: int | None) -> str:
        if id_ is None:
            return ""
        return self.map(kind).get(id_, f"#{id_}")


# --- Підписи записів ----------------------------------------------------------

def _person_label(r, _labels) -> str:
    name = person_short(r["last_name"], r["first_name"], r["middle_name"])
    return f"{r['rank']} {name}" if r["rank"] else name


def _unit_label(r, _labels) -> str:
    return f"{r['name']} ({r['short_name']})" if r["short_name"] else r["name"]


def _nomenclature_label(r, _labels) -> str:
    return f"{r['name']} [{r['code']}]" if r["code"] else r["name"]


def _item_label(r, labels: Labels) -> str:
    name = labels.get("nomenclature", r["nomenclature_id"])
    numbers = []
    if r["serial_no"]:
        numbers.append(f"зав. № {r['serial_no']}")
    if r["inventory_no"]:
        numbers.append(f"інв. № {r['inventory_no']}")
    return f"{name}, {', '.join(numbers)}" if numbers else name


# --- Перевірки ----------------------------------------------------------------

_EDRPOU_RE = re.compile(r"^\d{8}(\d{2})?$")


def check_edrpou(values: dict, errors: dict, name: str = "edrpou") -> None:
    value = values.get(name)
    if value and not _EDRPOU_RE.match(value):
        errors[name] = "Код ЄДРПОУ — 8 цифр (або 10 для ФОП)"


def _validate_unit(conn, id_, values, errors):
    check_edrpou(values, errors)
    parent = values.get("parent_id")
    if id_ is not None and parent is not None:
        # Батьківський підрозділ не може бути цим самим або вкладеним у нього.
        seen, current = set(), parent
        while current is not None and current not in seen:
            if current == id_:
                errors["parent_id"] = "Не можна підпорядкувати підрозділ самому собі або своєму підлеглому"
                break
            seen.add(current)
            row = conn.execute("SELECT parent_id FROM units WHERE id = ?", (current,)).fetchone()
            current = row["parent_id"] if row else None
    if values.get("is_accounting") and not values.get("mvo_person_id"):
        errors["mvo_person_id"] = "Для місця обліку вкажіть МВО"
    if id_ is not None and not values.get("is_accounting"):
        used = conn.execute(
            "SELECT 1 FROM locations l JOIN movements m "
            "ON m.from_location_id = l.id OR m.to_location_id = l.id "
            "WHERE l.unit_id = ? LIMIT 1", (id_,)).fetchone()
        if used:
            errors["is_accounting"] = (
                "За цим місцем обліку вже є рух майна — зняти позначку не можна")


def _validate_counterparty(conn, id_, values, errors):
    check_edrpou(values, errors)


def _validate_nomenclature(conn, id_, values, errors):
    if id_ is not None and not values.get("serial_tracked"):
        count = conn.execute(
            "SELECT COUNT(*) FROM items WHERE nomenclature_id = ?", (id_,)
        ).fetchone()[0]
        if count:
            errors["serial_tracked"] = (
                f"Є {count} поштучних одиниць цієї номенклатури — вимкнути поштучний облік не можна"
            )


def _validate_item(conn, id_, values, errors):
    nom_id = values.get("nomenclature_id")
    if nom_id is not None:
        row = conn.execute(
            "SELECT serial_tracked FROM nomenclature WHERE id = ?", (nom_id,)
        ).fetchone()
        if row and not row["serial_tracked"]:
            errors["nomenclature_id"] = (
                "Для цієї номенклатури не ввімкнено поштучний облік (див. довідник «Номенклатура»)"
            )
    inv = values.get("inventory_no")
    if inv:
        clash = conn.execute(
            "SELECT id FROM items WHERE inventory_no = ? AND id IS NOT ?", (inv, id_)
        ).fetchone()
        if clash:
            errors["inventory_no"] = "Одиниця з таким інвентарним номером уже є"
    if not values.get("serial_no") and not inv:
        errors["serial_no"] = "Вкажіть заводський або інвентарний номер"


# --- Довідники ----------------------------------------------------------------

REFS: dict[str, Ref] = {}


def _register(ref: Ref) -> Ref:
    REFS[ref.kind] = ref
    return ref


_register(Ref(
    kind="units",
    table="units",
    title="Структура частини",
    item_title="підрозділ",
    description="Військова частина, служби, підрозділи, склади. "
                "Позначте «Місце обліку» там, де ведеться облік майна і є МВО.",
    fields=[
        Field("name", "Найменування", required=True, in_list=True),
        Field("short_name", "Умовне найменування", in_list=True, term="Умовне найменування",
              help="Наприклад, А0000. Вказується в документах замість дійсного найменування."),
        Field("kind", "Тип", "choice", required=True, choices=UNIT_KINDS, in_list=True,
              default="subunit"),
        Field("parent_id", "Підпорядковується", "fk", fk="units", in_list=True),
        Field("is_accounting", "Місце обліку майна", "bool", in_list=True, term="Місце обліку",
              help="Тут ведеться окрема книга обліку, є МВО, рахуються залишки."),
        Field("mvo_person_id", "МВО", "fk", fk="persons", in_list=True, term="МВО"),
        Field("edrpou", "Код ЄДРПОУ", help="Для військової частини."),
        Field("note", "Примітка", "textarea"),
    ],
    order_sql="name COLLATE UK",
    search_sql=["name", "short_name"],
    label=_unit_label,
    validate=_validate_unit,
))

_register(Ref(
    kind="persons",
    table="persons",
    title="Особи",
    item_title="особу",
    description="Військовослужбовці та працівники: МВО, отримувачі, члени комісій, підписанти.",
    fields=[
        Field("last_name", "Прізвище", required=True, in_list=True),
        Field("first_name", "Власне ім'я", required=True, in_list=True),
        Field("middle_name", "По батькові"),
        Field("rank", "Військове звання", suggest=RANKS, in_list=True),
        Field("position", "Посада", "textarea", in_list=True),
        Field("unit_id", "Підрозділ", "fk", fk="units", in_list=True),
        Field("roles", "Ролі", "roles", choices=ROLES, in_list=True),
        Field("note", "Примітка", "textarea"),
    ],
    order_sql="last_name COLLATE UK, first_name COLLATE UK",
    search_sql=["last_name", "first_name", "middle_name", "position"],
    label=_person_label,
))

_register(Ref(
    kind="counterparties",
    table="counterparties",
    title="Джерела та контрагенти",
    item_title="контрагента",
    description="Звідки надходить майно і кому передається за межі частини: "
                "вища частина, інші частини, постачальники, благодійники.",
    fields=[
        Field("name", "Найменування", required=True, in_list=True),
        Field("kind", "Тип", "choice", required=True, choices=COUNTERPARTY_KINDS, in_list=True,
              default="military_unit"),
        Field("edrpou", "Код ЄДРПОУ", in_list=True),
        Field("address", "Адреса", "textarea"),
        Field("note", "Примітка", "textarea"),
    ],
    order_sql="name COLLATE UK",
    search_sql=["name", "edrpou"],
    label=lambda r, _l: r["name"],
    validate=_validate_counterparty,
))

_register(Ref(
    kind="services",
    table="services",
    title="Служби забезпечення",
    item_title="службу",
    description="Служби, за номенклатурою яких обліковується майно (ТЗО, зв'язку, речова тощо).",
    fields=[
        Field("name", "Найменування", required=True, in_list=True),
        Field("short_name", "Скорочено", in_list=True),
        Field("note", "Примітка", "textarea"),
    ],
    order_sql="name COLLATE UK",
    search_sql=["name", "short_name"],
    label=lambda r, _l: r["short_name"] or r["name"],
))

_register(Ref(
    kind="nomenclature",
    table="nomenclature",
    title="Номенклатура",
    item_title="найменування",
    description="Найменування військового майна з кодом номенклатури та одиницею виміру.",
    fields=[
        Field("name", "Найменування", required=True, in_list=True, term="Номенклатура",
              help="Як у класифікаторі (каталозі) предметів постачання; індекс, номер креслення."),
        Field("code", "Код номенклатури", in_list=True, term="Код номенклатури"),
        Field("nato_code", "Номенклатурний номер НАТО"),
        Field("uom", "Одиниця виміру", required=True, suggest=UOMS, in_list=True, default="шт."),
        Field("accounting_class", "Клас майна", "choice", required=True,
              choices=ACCOUNTING_CLASSES, in_list=True, default="inventory",
              help="Визначає форму акта: для основних засобів і МНМА — дод. 23, "
                   "для запасів і МШП — дод. 24."),
        Field("account", "Субрахунок", help="Для бухгалтерії, необов'язково. Заповнюється при імпорті."),
        Field("service_id", "Служба", "fk", fk="services", in_list=True),
        Field("is_categorized", "Облік за категоріями (I–V)", "bool", default=1, term="Категорія"),
        Field("serial_tracked", "Поштучний облік за номерами", "bool", in_list=True, term="Поштучний облік",
              help="Кожна одиниця має свій заводський / інвентарний номер."),
        Field("note", "Примітка", "textarea"),
    ],
    order_sql="name COLLATE UK",
    search_sql=["name", "code", "nato_code"],
    label=_nomenclature_label,
    validate=_validate_nomenclature,
))

_register(Ref(
    kind="items",
    table="items",
    title="Поштучні одиниці",
    item_title="одиницю",
    description="Зразки з заводськими / інвентарними номерами. Місце й категорія одиниці "
                "визначаються документами руху.",
    fields=[
        Field("nomenclature_id", "Найменування", "fk", fk="nomenclature", required=True, in_list=True),
        Field("serial_no", "Заводський номер", in_list=True),
        Field("inventory_no", "Інвентарний номер", in_list=True),
        Field("passport_no", "Номер паспорта (формуляра)"),
        Field("manufacturer", "Завод-виробник"),
        Field("year_made", "Рік випуску", "int", in_list=True),
        Field("initial_cost_kop", "Первісна вартість, грн", "money", in_list=True),
        Field("components", "Склад (комплектність)", "components", term="Комплектність",
              help="Складові частини системи / комплекту, якщо є."),
        Field("note", "Примітка", "textarea"),
    ],
    order_sql="(SELECT name FROM nomenclature n WHERE n.id = items.nomenclature_id) COLLATE UK, "
              "inventory_no, serial_no",
    search_sql=["serial_no", "inventory_no", "passport_no",
                "(SELECT name FROM nomenclature n WHERE n.id = items.nomenclature_id)"],
    label=_item_label,
    validate=_validate_item,
))


# --- Форматування значень -----------------------------------------------------

def display(f: Field, value, labels: Labels) -> str:
    """Значення поля у вигляді тексту (для таблиць і журналу змін)."""
    if f.kind == "bool":
        return "так" if value else "ні"
    if value is None or value == "" or value == []:
        return ""
    if f.kind == "choice":
        return dict(f.choices or []).get(value, str(value))
    if f.kind == "fk":
        return labels.get(f.fk, value)
    if f.kind == "money":
        return format_money(value) + " грн"
    if f.kind == "roles":
        names = dict(f.choices or [])
        return ", ".join(names.get(r, r) for r in value)
    if f.kind == "components":
        return "; ".join(
            f"{c['name']} — {format_qty(c['qty_m'])} {c.get('uom') or ''}".strip() for c in value
        )
    return str(value)


def form_value(f: Field, value) -> str:
    """Значення для поля введення у формі."""
    if value is None:
        return ""
    if f.kind == "money":
        return format_money(value).replace(" ", " ")
    return str(value)


# --- Читання ------------------------------------------------------------------

def get(conn: sqlite3.Connection, ref: Ref, id_: int) -> dict | None:
    row = conn.execute(f"SELECT * FROM {ref.table} WHERE id = ?", (id_,)).fetchone()
    if row is None:
        return None
    record = dict(row)
    for f in ref.fields:
        if f.kind == "roles":
            record[f.name] = [
                r["role"] for r in conn.execute(
                    "SELECT role FROM person_roles WHERE person_id = ?", (id_,))
            ]
            order = [c for c, _ in ROLES]
            record[f.name].sort(key=order.index)
        elif f.kind == "components":
            record[f.name] = [
                dict(r) for r in conn.execute(
                    "SELECT name, qty_m, uom, serial_no, note FROM item_components "
                    "WHERE item_id = ? ORDER BY line_no", (id_,))
            ]
    return record


def list_rows(
    conn: sqlite3.Connection, ref: Ref, q: str = "", show_inactive: bool = False,
    limit: int = 500,
) -> list[dict]:
    where, params = [], []
    if not show_inactive:
        where.append("is_active = 1")
    if q.strip():
        for word in q.lower().split():
            where.append("(" + " OR ".join(f"ulower({expr}) LIKE ?" for expr in ref.search_sql) + ")")
            params.extend([f"%{word}%"] * len(ref.search_sql))
    sql = f"SELECT id FROM {ref.table}"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" ORDER BY {ref.order_sql} LIMIT {int(limit)}"
    return [get(conn, ref, r["id"]) for r in conn.execute(sql, params)]


def count(conn: sqlite3.Connection, ref: Ref) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {ref.table} WHERE is_active = 1").fetchone()[0]


def options(conn: sqlite3.Connection, kind: str, current: int | None = None,
            exclude: int | None = None) -> list[tuple[int, str]]:
    """Варіанти для поля-посилання: активні записи + поточне значення."""
    ref = REFS[kind]
    labels = Labels(conn)
    rows = conn.execute(
        f"SELECT id FROM {ref.table} WHERE (is_active = 1 OR id IS ?) AND id IS NOT ? "
        f"ORDER BY {ref.order_sql}",
        (current, exclude),
    ).fetchall()
    return [(r["id"], labels.get(kind, r["id"])) for r in rows]


# --- Розбір форми -------------------------------------------------------------

def parse_form(conn: sqlite3.Connection, ref: Ref, form) -> tuple[dict, dict]:
    """Значення з веб-форми → (значення для збереження, помилки)."""
    values, errors = {}, {}
    for f in ref.fields:
        try:
            values[f.name] = parse_field(conn, f, form)
        except ValueError as exc:
            errors[f.name] = str(exc)
            values[f.name] = None
            continue
        if f.required and values[f.name] in (None, "", []):
            errors[f.name] = "Обов'язкове поле"
    return values, errors


def parse_field(conn, f: Field, form):
    if f.kind == "bool":
        return 1 if form.get(f.name) else 0
    if f.kind == "roles":
        allowed = {c for c, _ in f.choices or []}
        return [r for r in form.getlist(f.name) if r in allowed]
    if f.kind == "components":
        return _parse_components(form)
    raw = (form.get(f.name) or "").strip()
    if raw == "":
        return None
    if f.kind == "int":
        if not re.fullmatch(r"-?\d+", raw):
            raise ValueError("Має бути ціле число")
        return int(raw)
    if f.kind == "money":
        try:
            return parse_money(raw)
        except ValueError as exc:
            raise ValueError(f"Невірна сума: {exc}") from exc
    if f.kind == "choice":
        if raw not in {c for c, _ in f.choices or []}:
            raise ValueError("Оберіть значення зі списку")
        return raw
    if f.kind == "fk":
        if not raw.isdigit():
            raise ValueError("Оберіть значення зі списку")
        ref = REFS[f.fk]
        if conn.execute(f"SELECT 1 FROM {ref.table} WHERE id = ?", (int(raw),)).fetchone() is None:
            raise ValueError("Запис не знайдено")
        return int(raw)
    return raw


def _parse_components(form) -> list[dict]:
    names = form.getlist("comp_name")
    qtys = form.getlist("comp_qty")
    uoms = form.getlist("comp_uom")
    serials = form.getlist("comp_serial")
    notes = form.getlist("comp_note")
    result = []
    for i, name in enumerate(names):
        name = name.strip()
        qty_raw = qtys[i].strip() if i < len(qtys) else ""
        if not name and not qty_raw:
            continue  # порожній рядок
        if not name:
            raise ValueError(f"Рядок {i + 1}: вкажіть найменування складової")
        try:
            qty = parse_qty(qty_raw or "1")
        except ValueError as exc:
            raise ValueError(f"Рядок {i + 1}: {exc}") from exc
        if not qty:
            raise ValueError(f"Рядок {i + 1}: кількість має бути більше нуля")
        result.append({
            "name": name,
            "qty_m": qty,
            "uom": (uoms[i].strip() if i < len(uoms) else "") or None,
            "serial_no": (serials[i].strip() if i < len(serials) else "") or None,
            "note": (notes[i].strip() if i < len(notes) else "") or None,
        })
    return result


# --- Запис --------------------------------------------------------------------

def _formatted(conn, ref: Ref, record: dict | None) -> dict[str, str]:
    if record is None:
        return {}
    labels = Labels(conn)
    return {f.name: display(f, record.get(f.name), labels) for f in ref.fields}


def save(conn: sqlite3.Connection, ref: Ref, id_: int | None, values: dict) -> int:
    """Створити (id_=None) або змінити запис. Пише в журнал змін.

    Транзакцію фіксує той, хто викликає (у веб-інтерфейсі — після запиту).
    """
    values = {**values}
    for f in ref.fields:
        if f.kind == "bool":
            values[f.name] = 1 if values.get(f.name) else 0
    errors: dict[str, str] = {}
    for f in ref.fields:
        if f.required and values.get(f.name) in (None, "", []):
            errors[f.name] = "Обов'язкове поле"
    if ref.validate:
        ref.validate(conn, id_, values, errors)
    if errors:
        raise ValidationError(errors)

    before = get(conn, ref, id_) if id_ is not None else None
    if id_ is not None and before is None:
        raise ValidationError({"": "Запис не знайдено"})

    cols = [f.name for f in ref.stored_fields]
    data = [values.get(c) for c in cols]
    if id_ is None:
        placeholders = ", ".join("?" for _ in cols)
        cur = conn.execute(
            f"INSERT INTO {ref.table} ({', '.join(cols)}) VALUES ({placeholders})", data)
        id_ = cur.lastrowid
    else:
        assignments = ", ".join(f"{c} = ?" for c in cols)
        conn.execute(f"UPDATE {ref.table} SET {assignments} WHERE id = ?", [*data, id_])

    for f in ref.fields:
        if f.kind == "roles":
            conn.execute("DELETE FROM person_roles WHERE person_id = ?", (id_,))
            conn.executemany(
                "INSERT INTO person_roles(person_id, role) VALUES (?, ?)",
                [(id_, r) for r in values.get(f.name) or []],
            )
        elif f.kind == "components":
            conn.execute("DELETE FROM item_components WHERE item_id = ?", (id_,))
            conn.executemany(
                "INSERT INTO item_components(item_id, line_no, name, qty_m, uom, serial_no, note) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(id_, n, c["name"], c["qty_m"], c.get("uom"), c.get("serial_no"), c.get("note"))
                 for n, c in enumerate(values.get(f.name) or [], start=1)],
            )

    after = get(conn, ref, id_)
    title = Labels(conn).get(ref.kind, id_)
    if before is None:
        audit.write(conn, "create", ref.kind, id_, title)
    else:
        changes = audit.diff(_formatted(conn, ref, before), _formatted(conn, ref, after),
                             {f.name: f.label for f in ref.fields})
        if changes:
            audit.write(conn, "update", ref.kind, id_, title, changes)
    return id_


def set_active(conn: sqlite3.Connection, ref: Ref, id_: int, active: bool) -> None:
    conn.execute(f"UPDATE {ref.table} SET is_active = ? WHERE id = ?", (1 if active else 0, id_))
    audit.write(conn, "activate" if active else "deactivate", ref.kind, id_,
                Labels(conn).get(ref.kind, id_))


def delete(conn: sqlite3.Connection, ref: Ref, id_: int) -> None:
    """Видалити запис. Якщо на нього є посилання — InUseError (тоді краще зробити неактивним)."""
    title = Labels(conn).get(ref.kind, id_)
    snapshot = _formatted(conn, ref, get(conn, ref, id_))
    conn.execute("SAVEPOINT ref_delete")
    try:
        conn.execute(f"DELETE FROM {ref.table} WHERE id = ?", (id_,))
    except sqlite3.IntegrityError as exc:
        conn.execute("ROLLBACK TO ref_delete")
        conn.execute("RELEASE ref_delete")
        raise InUseError(
            f"«{title}» використовується в інших записах, тому видалити не можна. "
            "Зробіть запис неактивним — він зникне зі списків вибору, але історія збережеться."
        ) from exc
    conn.execute("RELEASE ref_delete")
    changes = [{"field": k, "label": ref.field(k).label, "old": v, "new": ""}
               for k, v in snapshot.items() if v]
    audit.write(conn, "delete", ref.kind, id_, title, changes)
