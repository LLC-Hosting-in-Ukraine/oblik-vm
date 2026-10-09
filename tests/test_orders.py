"""Наряд на видавання (приймання) військового майна (дод. 5): розпорядчий документ без руху."""
from __future__ import annotations

import sqlite3

import pytest

from oblik import balances, documents, docx_out, locations, registers
from oblik.documents import DocumentError
from tests.test_docx import text_of
from tests.test_documents_web import add, doc_id_from, env, open_db  # noqa: F401 — фікстура env
from tests.test_issue_owner import receive_cable


def make_order(client, ids, other):
    head = {"from_place": f"u:{ids['wh']}", "to_place": f"c:{other}", "doc_date": "2026-10-08",
            "op_date": "2026-10-10", "valid_until": "2026-10-31", "basis": "Розпорядження № 15",
            "x_transport_kind": "автомобільний", "x_transport_no": "АА 0000 ВВ",
            "x_dispatch_order": "Транспорт одержувача", "x_guard_unit": "А2222",
            "copies_block": "1", "copies_auto": "[]", "copy": ["Військова частина А2222", "у справу"]}
    doc = doc_id_from(client.post("/documents/new/order", data=head))
    resp = client.post(f"/documents/{doc}", data={
        **head, "mode": "in", "action": "post",
        # Камера обліковується поштучно — у наряді номерів не вказують, кількість будь-яка.
        "ln_nom": [str(ids["cam"]), str(ids["cable"])], "ln_name": ["Камера", "Кабель"],
        "ln_serial": ["", ""], "ln_inv": ["", ""], "ln_cat": ["1", "1"], "ln_price": ["4460", "10"],
        "ln_qty": ["2", "50"], "ln_req": ["", ""], "ln_note": ["", "для А2222"]})
    assert resp.status_code == 302, resp.get_data(as_text=True)
    return doc


def test_order_is_signed_without_movements(env):
    storage, client, ids = env
    conn = open_db(storage)
    other = add(conn, "counterparties", name="Військова частина А2222", kind="military_unit",
                address="м. Н-ськ, вул. Вигадана, 1")
    conn.commit()
    conn.close()
    receive_cable(client, ids, f"u:{ids['wh']}", "10")
    assert "Даємо наряд" in client.get("/documents/new").get_data(as_text=True)
    form = client.get("/documents/new/order").get_data(as_text=True)
    assert "Вантажовідправник" in form and "Номер транспортного документа" in form

    doc = make_order(client, ids, other)
    conn = open_db(storage)
    d = documents.get(conn, doc)
    assert d["status"] == "posted" and d["doc_type"] == "order"
    assert documents.order_extra(d)["transport_no"] == "АА 0000 ВВ"
    assert conn.execute("SELECT COUNT(*) FROM movements WHERE document_id = ?", (doc,)).fetchone()[0] == 0
    # Залишки не змінились: наряд лише розпорядження (кабелю 10 на складі, а в наряді — 50).
    wh = locations.for_unit(conn, ids["wh"])
    assert sum(r["qty_m"] for r in balances.balances(conn, [wh], "2026-10-31")) == 10000
    # Книга реєстрації — з власною назвою.
    j = {r["reg_no"]: r for r in registers.journal(conn, 2026).rows}
    assert j[d["reg_no"]]["title"] == "Наряд на видавання (приймання) військового майна"
    # Сторнувати нічого.
    with pytest.raises(DocumentError, match="не рухав"):
        documents.reverse(conn, doc)
    # Рух за нарядом заборонено і на рівні бази.
    line = conn.execute("SELECT id FROM document_lines WHERE document_id = ?", (doc,)).fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO movements(document_id, line_id, op_date, from_location_id, to_location_id, "
                     "nomenclature_id, price_kop, qty_m) VALUES (?, ?, '2026-10-10', ?, ?, ?, 0, 1000)",
                     (doc, line, wh, locations.for_counterparty(conn, other), ids["cable"]))
    assert [f.code for f in docx_out.available_forms(conn, doc)] == ["naryad"]
    conn.close()

    view = client.get(f"/documents/{doc}").get_data(as_text=True)
    assert f"/documents/{doc}/pdf/naryad" in view
    resp = client.get(f"/documents/{doc}/pdf/naryad")
    assert resp.status_code == 200 and resp.data[:5] == b"%PDF-"
    text = text_of(client.get(f"/documents/{doc}/docx/naryad").data)
    assert "{{" not in text and "{%" not in text
    for expected in ("НАРЯД № 2", "Дійсний до «31» жовтня 2026 року", "Склад ТЗО",
                     "Військова частина А2222, м. Н-ськ, вул. Вигадана, 1", "АА 0000 ВВ", "автомобільний",
                     "Транспорт одержувача", "Камера", "для А2222", "Розпорядження № 15",
                     "Усього найменувань: 2 (два)", "Виконано у 2 примірниках",
                     "Примірник № 1 – Військова частина А2222;", "Примірник № 2 – у справу."):
        assert expected in text, expected


def test_order_validation(env):
    storage, client, ids = env
    conn = open_db(storage)
    other = locations.for_counterparty(conn, ids["ext"])
    wh = locations.for_unit(conn, ids["wh"])
    doc = documents.create_draft(conn, "order", wh, other, doc_date="2026-10-08")
    assert documents.validate(conn, doc) == ["У документі немає жодного рядка"]
    documents.set_lines(conn, doc, [{"nomenclature_id": ids["cable"], "qty_m": 1000, "price_kop": 100}])
    assert "категорію" in documents.validate(conn, doc)[0]
    # Наряд може бути й між двома чужими частинами (ми — орган забезпечення).
    a3 = add(conn, "counterparties", name="Військова частина А3333", kind="military_unit")
    doc2 = documents.create_draft(conn, "order", other, locations.for_counterparty(conn, a3), doc_date="2026-10-08")
    documents.set_lines(conn, doc2, [{"nomenclature_id": ids["cable"], "qty_m": 5000, "price_kop": 100,
                                      "category": 1}])
    assert documents.validate(conn, doc2) == []
    documents.post(conn, doc2)
    conn.close()
