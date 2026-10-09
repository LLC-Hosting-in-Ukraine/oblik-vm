"""Примірники документа: за замовчуванням — сторонам документа, можна додати й обрати, для кого."""
from __future__ import annotations

import json

from oblik import documents
from tests.test_docx import text_of
from tests.test_documents_web import add, doc_id_from, env, open_db  # noqa: F401 — фікстура env
from tests.test_issue_owner import cable_key, receive_cable


def test_default_two_copies_follow_places(env):
    storage, client, ids = env
    conn = open_db(storage)
    conn.execute("INSERT INTO settings(key, value) VALUES ('subunit_name', 'Військова частина А1111')")
    conn.commit()
    conn.close()
    receipt = receive_cable(client, ids, f"u:{ids['wh']}")
    conn = open_db(storage)
    # Ззовні — назва контрагента; наша сторона — наша частина.
    assert documents.copies(conn, documents.get(conn, receipt)) == ["Вища частина", "Військова частина А1111"]
    conn.close()
    # Видача бійцю — обидві сторони свої: місця.
    head = {"from_place": f"u:{ids['wh']}", "to_place": f"p:{ids['soldier']}", "doc_date": "2026-10-05"}
    doc = doc_id_from(client.post("/documents/new/transfer", data=head))
    conn = open_db(storage)
    d = documents.get(conn, doc)
    assert documents.copies(conn, d) == ["Склад ТЗО", "Бондар І."]
    conn.close()
    # Форма показує список і підказки; зберегли без змін — лишається «за замовчуванням».
    page = client.get(f"/documents/{doc}").get_data(as_text=True)
    assert 'name="copy"' in page and 'id="copy-options"' in page and "у справу" in page
    auto = json.dumps(["Склад ТЗО", "Бондар І."])
    base = {**head, "mode": "out", "action": "save", "ln_key": [cable_key(ids)], "ln_qty": ["1"],
            "ln_req": [""], "ln_note": [""], "copies_block": "1", "copies_auto": auto}
    client.post(f"/documents/{doc}", data={**base, "copy": ["Склад ТЗО", "Бондар І."]})
    conn = open_db(storage)
    assert "copies" not in documents.extra(documents.get(conn, doc))
    conn.close()


def test_custom_copies_saved_and_printed(env):
    storage, client, ids = env
    receive_cable(client, ids, f"u:{ids['wh']}", "10")
    head = {"from_place": f"u:{ids['wh']}", "to_place": f"p:{ids['soldier']}", "doc_date": "2026-10-05"}
    doc = doc_id_from(client.post("/documents/new/transfer", data=head))
    auto = json.dumps(["Склад ТЗО", "Бондар І."])
    client.post(f"/documents/{doc}", data={
        **head, "mode": "out", "action": "post", "ln_key": [cable_key(ids)], "ln_qty": ["2"], "ln_req": [""],
        "ln_note": [""], "copies_block": "1", "copies_auto": auto,
        "copy": ["Склад ТЗО", "Бондар І.", "фінансово-економічного органу", ""]})
    conn = open_db(storage)
    assert documents.copies(conn, documents.get(conn, doc)) == ["Склад ТЗО", "Бондар І.",
                                                               "фінансово-економічного органу"]
    conn.close()
    text = text_of(client.get(f"/documents/{doc}/docx/nakladna").data)
    assert "Виконано у 3 примірниках:" in text
    assert "Примірник № 3 – фінансово-економічного органу." in text
    assert client.get(f"/documents/{doc}/pdf/nakladna").status_code == 200


def test_copies_text():
    assert documents.copies_text([]) == ""
    assert documents.copies_text(["у справу"]) == "Виконано у 1 примірнику:\nПримірник № 1 – у справу."
    assert documents.copies_text(["А", "Б"]).splitlines() == [
        "Виконано у 2 примірниках:", "Примірник № 1 – А;", "Примірник № 2 – Б."]
