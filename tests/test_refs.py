"""Довідники, налаштування, журнал змін, допоміжні функції."""
from __future__ import annotations

import json
import sqlite3

import pytest
from werkzeug.datastructures import MultiDict

from oblik import db, refs, settings, textutil
from oblik.refs import REFS, InUseError, ValidationError


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.migrate(c)
    db.init_meta(c, "test")
    c.execute("INSERT INTO settings(key, value) VALUES ('operator_name', 'Тестовий О.')")
    yield c
    c.close()


def add(conn, ref_kind, **values):
    ref = REFS[ref_kind]
    full = {f.name: f.default for f in ref.fields}
    full.update(values)
    return refs.save(conn, ref, None, full)


def person(conn, last="Петренко", first="Петро", **kw):
    return add(conn, "persons", last_name=last, first_name=first, **kw)


# --- textutil -----------------------------------------------------------------

@pytest.mark.parametrize("text, kop", [
    ("1 234,56", 123456), ("1234.5", 123450), ("0,01", 1), ("10 639 451,00", 1063945100),
    ("27 599 926,04", 2759992604), ("", None),
])
def test_parse_money(text, kop):
    assert textutil.parse_money(text) == kop


def test_money_roundtrip():
    assert textutil.format_money(1063945100) == "10 639 451,00"
    assert textutil.parse_money(textutil.format_money(123456)) == 123456


@pytest.mark.parametrize("text", ["-5", "abc", "1,2,3"])
def test_parse_money_rejects(text):
    with pytest.raises(ValueError):
        textutil.parse_money(text)


def test_qty():
    assert textutil.parse_qty("2,5") == 2500
    assert textutil.format_qty(2500) == "2,5"
    assert textutil.format_qty(3000) == "3"
    assert textutil.format_qty(1830125) == "1830,125"
    with pytest.raises(ValueError):
        textutil.parse_qty("0,0001")


def test_ukrainian_sort_order():
    words = ["Яблуко", "Ґанок", "Гайка", "Єнот", "Енергія", "Їжак", "Іскра", "Инший", "Апостроф'я"]
    assert sorted(words, key=textutil.uk_sort_key) == [
        "Апостроф'я", "Гайка", "Ґанок", "Енергія", "Єнот", "Инший", "Іскра", "Їжак", "Яблуко"]


def test_person_names():
    assert textutil.person_short("Петренко", "Петро", "Петрович") == "Петренко П.П."
    assert textutil.person_signature("Петренко", "Петро") == "Петро ПЕТРЕНКО"


# --- Довідники ----------------------------------------------------------------

def test_create_update_writes_audit(conn):
    pid = person(conn, rank="сержант")
    refs.save(conn, REFS["persons"], pid, {**refs.get(conn, REFS["persons"], pid), "rank": "старший сержант"})

    rows = conn.execute(
        "SELECT action, operator, summary, details FROM audit_log WHERE entity='persons' ORDER BY id"
    ).fetchall()
    assert [r["action"] for r in rows] == ["create", "update"]
    assert rows[0]["operator"] == "Тестовий О."
    changes = json.loads(rows[1]["details"])
    assert changes == [{"field": "rank", "label": "Військове звання",
                        "old": "сержант", "new": "старший сержант"}]


def test_update_without_changes_is_not_logged(conn):
    pid = person(conn)
    refs.save(conn, REFS["persons"], pid, refs.get(conn, REFS["persons"], pid))
    assert conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 1


def test_roles_saved_and_listed(conn):
    pid = person(conn, roles=["recipient", "mvo"])
    rec = refs.get(conn, REFS["persons"], pid)
    assert rec["roles"] == ["mvo", "recipient"]
    labels = refs.Labels(conn)
    assert refs.display(REFS["persons"].field("roles"), rec["roles"], labels) == "МВО, Отримувач"


def test_search_is_case_insensitive_for_cyrillic(conn):
    person(conn, "Шевченко", "Тарас")
    person(conn, "Коваль", "Іван")
    found = refs.list_rows(conn, REFS["persons"], "шевч")
    assert [r["last_name"] for r in found] == ["Шевченко"]
    found = refs.list_rows(conn, REFS["persons"], "ІВАН коваль")
    assert [r["last_name"] for r in found] == ["Коваль"]


def test_list_sorted_by_ukrainian_alphabet(conn):
    for name in ["Єрмак", "Гнатюк", "Ґудзь", "Іванов", "Ткач"]:
        person(conn, name, "А")
    assert [r["last_name"] for r in refs.list_rows(conn, REFS["persons"])] == [
        "Гнатюк", "Ґудзь", "Єрмак", "Іванов", "Ткач"]


def test_inactive_hidden_from_lists_and_options(conn):
    pid = person(conn)
    refs.set_active(conn, REFS["persons"], pid, False)
    assert refs.list_rows(conn, REFS["persons"]) == []
    assert len(refs.list_rows(conn, REFS["persons"], show_inactive=True)) == 1
    assert refs.options(conn, "persons") == []
    # Але поточне значення поля лишається у варіантах вибору.
    assert [o[0] for o in refs.options(conn, "persons", current=pid)] == [pid]


def test_required_fields(conn):
    with pytest.raises(ValidationError) as exc:
        add(conn, "persons", last_name="", first_name=None)
    assert set(exc.value.errors) == {"last_name", "first_name"}


def test_edrpou_format(conn):
    with pytest.raises(ValidationError) as exc:
        add(conn, "counterparties", name="ТОВ", edrpou="123")
    assert "edrpou" in exc.value.errors
    add(conn, "counterparties", name="ТОВ", edrpou="12345678")


def test_accounting_place_requires_mvo(conn):
    with pytest.raises(ValidationError) as exc:
        add(conn, "units", name="Склад ТЗО", kind="warehouse", is_accounting=1)
    assert "mvo_person_id" in exc.value.errors
    add(conn, "units", name="Склад ТЗО", kind="warehouse", is_accounting=1,
        mvo_person_id=person(conn))


def test_unit_cycle_is_rejected(conn):
    a = add(conn, "units", name="Частина", kind="military_unit")
    b = add(conn, "units", name="Служба", kind="service", parent_id=a)
    c = add(conn, "units", name="Склад", kind="warehouse", parent_id=b)
    with pytest.raises(ValidationError) as exc:
        refs.save(conn, REFS["units"], a, {**refs.get(conn, REFS["units"], a), "parent_id": c})
    assert "parent_id" in exc.value.errors


def test_delete_in_use_is_refused(conn):
    pid = person(conn)
    uid = add(conn, "units", name="Склад", kind="warehouse", is_accounting=1, mvo_person_id=pid)
    with pytest.raises(InUseError):
        refs.delete(conn, REFS["persons"], pid)
    assert refs.get(conn, REFS["persons"], pid) is not None
    refs.delete(conn, REFS["units"], uid)
    refs.delete(conn, REFS["persons"], pid)
    actions = [r[0] for r in conn.execute("SELECT action FROM audit_log ORDER BY id")]
    assert actions[-2:] == ["delete", "delete"]


def test_item_requires_serial_tracked_nomenclature(conn):
    nom = add(conn, "nomenclature", name="Кабель", uom="м")
    with pytest.raises(ValidationError) as exc:
        add(conn, "items", nomenclature_id=nom, serial_no="1")
    assert "nomenclature_id" in exc.value.errors


def test_item_numbers_and_components(conn):
    nom = add(conn, "nomenclature", name="Система відеоспостереження", uom="к-т",
              accounting_class="fixed", serial_tracked=1)
    with pytest.raises(ValidationError) as exc:
        add(conn, "items", nomenclature_id=nom)
    assert "serial_no" in exc.value.errors

    comps = [{"name": "Відеокамера", "qty_m": 9000, "uom": "шт."},
             {"name": "Кабель", "qty_m": 800500, "uom": "м"}]
    iid = add(conn, "items", nomenclature_id=nom, inventory_no="1014-001",
              initial_cost_kop=1063945100, components=comps)
    rec = refs.get(conn, REFS["items"], iid)
    assert [c["name"] for c in rec["components"]] == ["Відеокамера", "Кабель"]
    assert refs.Labels(conn).get("items", iid) == "Система відеоспостереження, інв. № 1014-001"

    with pytest.raises(ValidationError) as exc:
        add(conn, "items", nomenclature_id=nom, inventory_no="1014-001")
    assert "inventory_no" in exc.value.errors

    # Вимкнути поштучний облік, коли одиниці вже є, не можна.
    with pytest.raises(ValidationError):
        refs.save(conn, REFS["nomenclature"], nom,
                  {**refs.get(conn, REFS["nomenclature"], nom), "serial_tracked": 0})


def test_parse_form_components_and_money(conn):
    form = MultiDict([
        ("nomenclature_id", ""), ("initial_cost_kop", "1 234,5"),
        ("comp_name", "Датчик"), ("comp_qty", "26"), ("comp_uom", "шт."),
        ("comp_serial", ""), ("comp_note", ""),
        ("comp_name", ""), ("comp_qty", ""), ("comp_uom", ""), ("comp_serial", ""), ("comp_note", ""),
    ])
    values, errors = refs.parse_form(conn, REFS["items"], form)
    assert errors == {"nomenclature_id": "Обов'язкове поле"}
    assert values["initial_cost_kop"] == 123450
    assert values["components"] == [
        {"name": "Датчик", "qty_m": 26000, "uom": "шт.", "serial_no": None, "note": None}]


def test_audit_log_is_immutable(conn):
    person(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE audit_log SET operator = 'хтось'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM audit_log")


def test_system_locations_seeded(conn):
    codes = [r[0] for r in conn.execute("SELECT system_code FROM locations ORDER BY id")]
    assert codes == ["opening", "written_off", "obtained"]


# --- Налаштування -------------------------------------------------------------

def test_settings_save_and_complete(conn):
    assert not settings.is_complete(conn)
    pid = person(conn, roles=["approver"])
    values = {f.name: None for f in settings.FIELDS}
    values.update(unit_name="Військова частина А0000", unit_code="А0000",
                  operator_name="Тестовий О.", approver_person_id=pid)
    settings.save(conn, values)
    assert settings.is_complete(conn)
    loaded = settings.load(conn)
    assert loaded["approver_person_id"] == pid and loaded["unit_code"] == "А0000"
    last = conn.execute("SELECT entity, details FROM audit_log ORDER BY id DESC").fetchone()
    assert last["entity"] == "settings"
    assert any(c["field"] == "approver_person_id" and c["new"] == "Петренко П."
               for c in json.loads(last["details"]))
