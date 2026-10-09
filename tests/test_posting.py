"""Проведення, сторно, залишки, картка руху, нумерація."""
from __future__ import annotations

import sqlite3

import pytest

from oblik import balances, db, documents, locations, refs
from oblik.balances import Key
from oblik.documents import DocumentError
from oblik.refs import REFS


# --- Підготовка ---------------------------------------------------------------

def add(conn, ref_kind, **values):
    ref = REFS[ref_kind]
    full = {f.name: f.default for f in ref.fields}
    full.update(values)
    return refs.save(conn, ref, None, full)


class World:
    """Невелика частина: склад, рота, особа в роті, вища частина, три номенклатури."""

    def __init__(self, conn):
        self.conn = conn
        self.mvo = add(conn, "persons", last_name="Склад", first_name="Мво")
        self.unit = add(conn, "units", name="Частина", kind="military_unit")
        self.wh_unit = add(conn, "units", name="Склад ТЗО", kind="warehouse", parent_id=self.unit,
                           is_accounting=1, mvo_person_id=self.mvo)
        self.coy_unit = add(conn, "units", name="1 рота", kind="subunit", parent_id=self.unit,
                            is_accounting=1, mvo_person_id=self.mvo)
        self.platoon = add(conn, "units", name="1 взвод", kind="subunit", parent_id=self.coy_unit)
        self.soldier = add(conn, "persons", last_name="Боєць", first_name="Іван", unit_id=self.platoon)
        self.higher = add(conn, "counterparties", name="Вища частина", kind="higher_unit")

        self.cable = add(conn, "nomenclature", name="Кабель", uom="м", accounting_class="inventory")
        self.paper = add(conn, "nomenclature", name="Папір", uom="уп.", accounting_class="inventory",
                         is_categorized=0)
        self.camera = add(conn, "nomenclature", name="Камера", uom="шт.", accounting_class="fixed",
                          serial_tracked=1)
        self.cam1 = add(conn, "items", nomenclature_id=self.camera, serial_no="CAM-1")
        self.cam2 = add(conn, "items", nomenclature_id=self.camera, serial_no="CAM-2")

        self.wh = locations.for_unit(conn, self.wh_unit)
        self.coy = locations.for_unit(conn, self.coy_unit)
        self.person = locations.for_person(conn, self.soldier)
        self.ext = locations.for_counterparty(conn, self.higher)
        self.opening = locations.system(conn, locations.OPENING)
        self.written_off = locations.system(conn, locations.WRITTEN_OFF)

    def doc(self, doc_type, src, dst, lines, op_date="2026-10-01", post=True, **header):
        doc_id = documents.create_draft(self.conn, doc_type, src, dst, doc_date=op_date,
                                        op_date=op_date, **header)
        documents.set_lines(self.conn, doc_id, lines)
        if post:
            documents.post(self.conn, doc_id)
        return doc_id

    def qty(self, location_id, nomenclature_id, on_date=None, **key):
        rows = balances.balances(self.conn, [location_id], on_date, nomenclature_id=nomenclature_id)
        return sum(r["qty_m"] for r in rows
                   if all(r[k] == v for k, v in key.items()))


def cable(qty, price=1000, category=1, **kw):
    return {"nomenclature_id": W.cable, "qty_m": qty * 1000, "price_kop": price,
            "category": category, **kw}


W: World  # заповнюється фікстурою, щоб помічники рядків бачили id


@pytest.fixture
def w(tmp_path):
    global W
    conn = db.connect(tmp_path / "t.db")
    db.migrate(conn)
    db.init_meta(conn, "test")
    W = World(conn)
    yield W
    conn.close()


# --- Проведення ---------------------------------------------------------------

def test_receipt_creates_movements_and_balance(w):
    doc = w.doc("receipt", w.ext, w.wh, [cable(100), cable(5, price=2000)])
    assert documents.get(w.conn, doc)["status"] == "posted"
    assert w.conn.execute("SELECT COUNT(*) FROM movements WHERE document_id = ?", (doc,)).fetchone()[0] == 2
    assert w.qty(w.wh, w.cable) == 105_000
    # Різні ціни — різні рядки залишку.
    assert w.qty(w.wh, w.cable, price_kop=2000) == 5_000
    rows = balances.balances(w.conn, [w.wh])
    assert sum(r["value_kop"] for r in rows) == 100 * 1000 + 5 * 2000


def test_balance_on_date(w):
    w.doc("receipt", w.ext, w.wh, [cable(10)], op_date="2026-10-05")
    assert w.qty(w.wh, w.cable, "2026-10-04") == 0
    assert w.qty(w.wh, w.cable, "2026-10-05") == 10_000


def test_transfer_moves_between_places(w):
    w.doc("receipt", w.ext, w.wh, [cable(10)])
    w.doc("transfer", w.wh, w.coy, [cable(4)], op_date="2026-10-02")
    assert w.qty(w.wh, w.cable) == 6_000
    assert w.qty(w.coy, w.cable) == 4_000


def test_insufficient_balance_is_rejected(w):
    w.doc("receipt", w.ext, w.wh, [cable(10)])
    doc = w.doc("transfer", w.wh, w.coy, [cable(11)], op_date="2026-10-02", post=False)
    with pytest.raises(DocumentError) as exc:
        documents.post(w.conn, doc)
    assert "доступно 10, потрібно 11" in exc.value.errors[0]
    assert documents.get(w.conn, doc)["status"] == "draft"
    assert w.qty(w.coy, w.cable) == 0


def test_price_and_category_must_match_stock(w):
    w.doc("receipt", w.ext, w.wh, [cable(10, price=1000, category=1)])
    for line in (cable(1, price=999), cable(1, category=2)):
        doc = w.doc("transfer", w.wh, w.coy, [line], op_date="2026-10-02", post=False)
        with pytest.raises(DocumentError):
            documents.post(w.conn, doc)


def test_lines_of_same_key_are_summed(w):
    w.doc("receipt", w.ext, w.wh, [cable(10)])
    doc = w.doc("transfer", w.wh, w.coy, [cable(6), cable(6)], op_date="2026-10-02", post=False)
    with pytest.raises(DocumentError) as exc:
        documents.post(w.conn, doc)
    assert "потрібно 12" in exc.value.errors[0]


def test_backdated_document_cannot_break_later_balance(w):
    w.doc("receipt", w.ext, w.wh, [cable(10)], op_date="2026-10-01")
    w.doc("transfer", w.wh, w.coy, [cable(8)], op_date="2026-10-05")
    # 03.10 на складі 10, але після 05.10 лишиться 2 — забрати 5 заднім числом не можна.
    doc = w.doc("transfer", w.wh, w.coy, [cable(5)], op_date="2026-10-03", post=False)
    with pytest.raises(DocumentError) as exc:
        documents.post(w.conn, doc)
    assert "доступно 2" in exc.value.errors[0]
    w.doc("transfer", w.wh, w.coy, [cable(2)], op_date="2026-10-03")
    assert w.qty(w.wh, w.cable) == 0


def test_category_rules(w):
    w.doc("receipt", w.ext, w.wh, [{"nomenclature_id": w.paper, "qty_m": 5000, "price_kop": 100}])
    for line in ({"nomenclature_id": w.paper, "qty_m": 1000, "price_kop": 100, "category": 1},
                 cable(1, category=None)):
        doc = w.doc("receipt", w.ext, w.wh, [line], post=False)
        errors = documents.validate(w.conn, doc)
        assert any("категор" in e for e in errors)


def test_location_rules_per_document_type(w):
    cases = [
        ("receipt", w.wh, w.coy),          # надходження не зі сторони
        ("transfer", w.ext, w.wh),         # переміщення ззовні
        ("dispatch", w.wh, w.coy),         # передача за межі — всередину
        ("opening", w.ext, w.wh),          # залишки не зі службового місця
        ("writeoff", w.wh, w.opening),     # списання не в «Списано»
    ]
    for doc_type, src, dst in cases:
        doc = w.doc(doc_type, src, dst, [cable(1)], post=False)
        assert any("невірне місце" in e for e in documents.validate(w.conn, doc)), doc_type


def test_empty_document_cannot_be_posted(w):
    doc = w.doc("receipt", w.ext, w.wh, [], post=False)
    assert documents.validate(w.conn, doc) == ["У документі немає жодного рядка"]


def test_opening_and_writeoff(w):
    w.doc("opening", w.opening, w.wh, [cable(3)])
    w.doc("writeoff", w.wh, w.written_off, [cable(1)], op_date="2026-10-02")
    assert w.qty(w.wh, w.cable) == 2_000
    # Службові місця не потрапляють у залишки.
    assert all(r["location_id"] == w.wh for r in balances.balances(w.conn))


# --- Незмінність проведеного --------------------------------------------------

def test_posted_document_is_immutable(w):
    doc = w.doc("receipt", w.ext, w.wh, [cable(10)])
    with pytest.raises(DocumentError):
        documents.update_draft(w.conn, doc, basis="інше")
    with pytest.raises(DocumentError):
        documents.set_lines(w.conn, doc, [cable(1)])
    with pytest.raises(DocumentError):
        documents.post(w.conn, doc)
    with pytest.raises(DocumentError):
        documents.cancel(w.conn, doc)
    # Навіть напряму в базі.
    for sql in ("UPDATE documents SET basis = 'x' WHERE id = ?",
                "DELETE FROM documents WHERE id = ?",
                "UPDATE document_lines SET qty_m = 1 WHERE document_id = ?",
                "DELETE FROM document_lines WHERE document_id = ?",
                "UPDATE movements SET qty_m = 1 WHERE document_id = ?",
                "DELETE FROM movements WHERE document_id = ?"):
        with pytest.raises(sqlite3.IntegrityError):
            w.conn.execute(sql, (doc,))
    with pytest.raises(sqlite3.IntegrityError):
        w.conn.execute(
            "INSERT INTO movements(document_id, line_id, op_date, from_location_id, to_location_id, "
            "nomenclature_id, price_kop, qty_m) SELECT ?, id, '2026-10-01', ?, ?, ?, 1, 1000 "
            "FROM document_lines WHERE document_id = ?", (doc, w.ext, w.wh, w.cable, doc))
    assert w.qty(w.wh, w.cable) == 10_000


# --- Сторно -------------------------------------------------------------------

def test_storno_restores_balances(w):
    w.doc("receipt", w.ext, w.wh, [cable(10)])
    transfer = w.doc("transfer", w.wh, w.coy, [cable(4)], op_date="2026-10-02")
    storno = documents.reverse(w.conn, transfer, on_date="2026-10-03")

    assert w.qty(w.wh, w.cable) == 10_000
    assert w.qty(w.coy, w.cable) == 0
    # На дату між документом і сторно рух ще видно — історія чесна.
    assert w.qty(w.coy, w.cable, "2026-10-02") == 4_000

    original = documents.get(w.conn, transfer)
    s = documents.get(w.conn, storno)
    assert original["status"] == "posted" and original["reversed_by_id"] == storno
    assert s["doc_type"] == "storno" and s["status"] == "posted" and s["reversal_of_id"] == transfer
    assert (s["from_location_id"], s["to_location_id"]) == (w.coy, w.wh)


def test_storno_rules(w):
    receipt = w.doc("receipt", w.ext, w.wh, [cable(10)], op_date="2026-10-05")
    with pytest.raises(DocumentError, match="раніше дати операції"):
        documents.reverse(w.conn, receipt, on_date="2026-10-04")
    storno = documents.reverse(w.conn, receipt, on_date="2026-10-05")
    with pytest.raises(DocumentError, match="уже сторновано"):
        documents.reverse(w.conn, receipt)
    with pytest.raises(DocumentError, match="не сторнується"):
        documents.reverse(w.conn, storno)
    draft = w.doc("receipt", w.ext, w.wh, [cable(1)], post=False)
    with pytest.raises(DocumentError, match="тільки проведений"):
        documents.reverse(w.conn, draft)


def test_storno_blocked_when_goods_moved_on(w):
    receipt = w.doc("receipt", w.ext, w.wh, [cable(10)])
    w.doc("transfer", w.wh, w.coy, [cable(10)], op_date="2026-10-02")
    docs_before = w.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    with pytest.raises(DocumentError) as exc:
        documents.reverse(w.conn, receipt, on_date="2026-10-03")
    assert "вже рушило далі" in exc.value.errors[0]
    # Невдале сторно не лишає ні документа, ні пропуску в номерах.
    assert w.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == docs_before
    assert documents.get(w.conn, receipt)["reversed_by_id"] is None
    assert documents.next_reg_no(w.conn, 2026) == docs_before + 1


def test_storno_then_correct_document(w):
    """Типовий сценарій виправлення: помилилися з кількістю → сторно → правильний документ."""
    w.doc("receipt", w.ext, w.wh, [cable(10)])
    wrong = w.doc("transfer", w.wh, w.coy, [cable(7)], op_date="2026-10-02")
    documents.reverse(w.conn, wrong, on_date="2026-10-02")
    w.doc("transfer", w.wh, w.coy, [cable(5)], op_date="2026-10-02")
    assert (w.qty(w.wh, w.cable), w.qty(w.coy, w.cable)) == (5_000, 5_000)


# --- Поштучний облік ----------------------------------------------------------

def camera(item, price=500000, category=1):
    return {"nomenclature_id": W.camera, "item_id": item, "qty_m": 1000, "price_kop": price,
            "category": category}


def test_serial_item_flow_and_card(w):
    w.doc("receipt", w.ext, w.wh, [camera(w.cam1), camera(w.cam2)])
    w.doc("transfer", w.wh, w.person, [camera(w.cam1)], op_date="2026-10-03")

    pos = balances.item_position(w.conn, w.cam1)
    assert pos["location_id"] == w.person and pos["category"] == 1
    assert balances.item_position(w.conn, w.cam1, "2026-10-02")["location_id"] == w.wh
    card = balances.item_card(w.conn, w.cam1)
    assert [(c["from_location_id"], c["to_location_id"]) for c in card] == [
        (w.ext, w.wh), (w.wh, w.person)]


def test_serial_item_rules(w):
    w.doc("receipt", w.ext, w.wh, [camera(w.cam1)])
    # Та сама одиниця вдруге надійти не може.
    doc = w.doc("receipt", w.ext, w.coy, [camera(w.cam1)], op_date="2026-10-02", post=False)
    assert any("уже на обліку" in e for e in documents.validate(w.conn, doc))
    # Видати можна лише звідти, де вона є.
    doc = w.doc("transfer", w.coy, w.person, [camera(w.cam1)], op_date="2026-10-02", post=False)
    assert any("доступно 0" in e for e in documents.validate(w.conn, doc))
    # Без одиниці, з кількістю ≠ 1, двічі в документі.
    for bad, text in (({"nomenclature_id": w.camera, "qty_m": 1000, "price_kop": 1, "category": 1},
                       "оберіть одиницю"),
                      ({**camera(w.cam1), "qty_m": 2000}, "кількість має бути 1")):
        doc = w.doc("transfer", w.wh, w.coy, [bad], op_date="2026-10-02", post=False)
        assert any(text in e for e in documents.validate(w.conn, doc))
    doc = w.doc("transfer", w.wh, w.coy, [camera(w.cam1), camera(w.cam1)], op_date="2026-10-02",
                post=False)
    assert any("вже є в іншому рядку" in e for e in documents.validate(w.conn, doc))


def test_serial_item_backdated_receipt_rejected_if_moved_later(w):
    w.doc("receipt", w.ext, w.wh, [camera(w.cam1)], op_date="2026-10-05")
    w.doc("dispatch", w.wh, w.ext, [camera(w.cam1)], op_date="2026-10-06")
    doc = w.doc("receipt", w.ext, w.coy, [camera(w.cam1)], op_date="2026-10-04", post=False)
    assert any("є рух після" in e for e in documents.validate(w.conn, doc))


# --- Залишки по підрозділу з особами -------------------------------------------

def test_unit_scope_includes_its_people(w):
    w.doc("receipt", w.ext, w.coy, [cable(10)])
    w.doc("transfer", w.coy, w.person, [cable(3)], op_date="2026-10-02")
    scope = locations.scope_for_unit(w.conn, w.coy_unit)
    assert set(scope) == {w.coy, w.person}
    total = sum(r["qty_m"] for r in balances.balances(w.conn, scope))
    assert total == 10_000      # видане бійцю лишається в залишках роти
    assert w.qty(w.person, w.cable) == 3_000
    # Склад до роти не входить, а частина з підлеглими — охоплює все.
    assert w.wh not in scope
    assert set(locations.scope_for_unit(w.conn, w.unit, include_subunits=True)) == {w.wh, w.coy, w.person}


def test_location_only_for_accounting_unit(w):
    with pytest.raises(locations.LocationError):
        locations.for_unit(w.conn, w.platoon)
    with pytest.raises(sqlite3.IntegrityError):
        w.conn.execute("INSERT INTO locations(kind, unit_id) VALUES ('unit', ?)", (w.platoon,))


def test_cannot_unmark_accounting_unit_with_movements(w):
    w.doc("receipt", w.ext, w.coy, [cable(1)])
    rec = refs.get(w.conn, REFS["units"], w.coy_unit)
    with pytest.raises(refs.ValidationError) as exc:
        refs.save(w.conn, REFS["units"], w.coy_unit, {**rec, "is_accounting": 0})
    assert "is_accounting" in exc.value.errors


# --- Нумерація ----------------------------------------------------------------

def test_numbering_per_year_and_editing(w):
    a = w.doc("receipt", w.ext, w.wh, [cable(1)], op_date="2026-12-30", post=False)
    b = w.doc("receipt", w.ext, w.wh, [cable(1)], op_date="2026-12-31", post=False)
    c = w.doc("receipt", w.ext, w.wh, [cable(1)], op_date="2027-01-02", post=False)
    assert [documents.get(w.conn, d)["reg_no"] for d in (a, b, c)] == [1, 2, 1]

    with pytest.raises(DocumentError, match="уже зайнятий"):
        documents.update_draft(w.conn, b, reg_no=1)
    documents.update_draft(w.conn, b, reg_no=15, doc_no="15/ТЗО")
    assert documents.next_reg_no(w.conn, 2026) == 16
    import json
    changes = json.loads(w.conn.execute(
        "SELECT details FROM audit_log WHERE action = 'update' ORDER BY id DESC").fetchone()[0])
    assert {c["label"] for c in changes} == {"Реєстраційний номер", "Номер документа"}

    # Анульована чернетка лишає свій номер у реєстрі.
    documents.cancel(w.conn, a)
    assert documents.get(w.conn, a)["status"] == "cancelled"
    with pytest.raises(sqlite3.IntegrityError):
        w.conn.execute("DELETE FROM documents WHERE id = ?", (a,))

    # Перенесення чернетки в інший рік — новий номер того року.
    documents.update_draft(w.conn, c, doc_date="2026-12-31", op_date="2026-12-31")
    moved = documents.get(w.conn, c)
    assert (moved["reg_year"], moved["reg_no"], moved["doc_no"]) == (2026, 16, "16")


def test_audit_records_document_life(w):
    doc = w.doc("receipt", w.ext, w.wh, [cable(1)])
    documents.reverse(w.conn, doc)
    actions = [r[0] for r in w.conn.execute(
        "SELECT action FROM audit_log WHERE entity = 'documents' ORDER BY id")]
    assert actions == ["create", "post", "create", "post", "reverse"]
