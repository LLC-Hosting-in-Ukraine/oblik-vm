"""Налаштування: реквізити військової частини та підписанти за замовчуванням."""
from __future__ import annotations

import sqlite3

from . import audit
from .refs import Field, Labels, ValidationError, check_edrpou, parse_field, display

FIELDS = [
    Field("unit_name", "Найменування військової частини (юридичної особи)", required=True,
          help="Та частина, що має свою бухгалтерію (фінансовий орган). Як у шапці документів: "
               "«Військова частина А0000»."),
    Field("unit_code", "Умовне найменування", required=True, help="Наприклад, А0000.",
          term="Умовне найменування"),
    Field("edrpou", "Код ЄДРПОУ", help="Код юридичної особи."),
    Field("subunit_name", "Частина (підрозділ), де ведеться облік",
          help="Заповнюйте, якщо ваша частина входить до складу іншої юридичної особи "
               "(«частина в частині»), наприклад: «Військова частина А1111». Пишеться в звітах і "
               "документах під назвою юридичної особи. Інакше — залиште порожнім."),
    Field("place", "Місце складання документів", help="Населений пункт."),
    Field("default_service_id", "Служба забезпечення за замовчуванням", "fk", fk="services"),
    Field("approver_person_id", "Затверджує документи (командир)", "fk", fk="persons",
          help="Хто підписує «Затверджую». У «частині в частині» — зазвичай командир вашої частини, "
               "а не юридичної особи. Можна змінити в окремому документі."),
    Field("service_head_person_id", "Начальник служби", "fk", fk="persons"),
    Field("finance_person_id", "Начальник фінансової служби", "fk", fk="persons"),
    Field("operator_name", "Хто веде облік у програмі", required=True,
          help="Прізвище та ініціали. Записується в журнал змін і як виконавець на документах "
               "(п. 9 розд. II Інструкції)."),
]

_FK_FIELDS = {f.name for f in FIELDS if f.kind == "fk"}


def load(conn: sqlite3.Connection) -> dict:
    stored = dict(conn.execute("SELECT key, value FROM settings").fetchall())
    result = {}
    for f in FIELDS:
        value = stored.get(f.name)
        if f.name in _FK_FIELDS and value is not None:
            value = int(value) if str(value).isdigit() else None
        result[f.name] = value
    return result


def is_complete(conn: sqlite3.Connection) -> bool:
    values = load(conn)
    return all(values.get(f.name) for f in FIELDS if f.required)


def parse_form(conn: sqlite3.Connection, form) -> tuple[dict, dict]:
    values, errors = {}, {}
    for f in FIELDS:
        try:
            values[f.name] = parse_field(conn, f, form)
        except ValueError as exc:
            errors[f.name] = str(exc)
            values[f.name] = None
    return values, errors


def save(conn: sqlite3.Connection, values: dict) -> None:
    errors = {f.name: "Обов'язкове поле" for f in FIELDS if f.required and not values.get(f.name)}
    check_edrpou(values, errors)
    if errors:
        raise ValidationError(errors)

    labels = Labels(conn)
    before = load(conn)
    for f in FIELDS:
        value = values.get(f.name)
        conn.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (f.name, None if value is None else str(value)),
        )
    changes = audit.diff(
        {f.name: display(f, before.get(f.name), labels) for f in FIELDS},
        {f.name: display(f, values.get(f.name), labels) for f in FIELDS},
        {f.name: f.label for f in FIELDS},
    )
    if changes:
        audit.write(conn, "update", "settings", None, "Налаштування", changes)
