"""Акт якісного (технічного) стану (дод. 1 до Порядку списання): списати й оприбуткувати в одному
документі — напр., кілька одиниць об'єднують в одну систему."""
from __future__ import annotations

import sqlite3

import pytest

from oblik import balances, db, documents, locations, registers
from tests.test_documents_web import add, doc_id_from, env, open_db  # noqa: F401 — фікстура env


def setup(storage, ids):
    """Склад: три камери (SN-001…003) і 100 м кабелю. Система — нове поштучне найменування."""
    conn = open_db(storage)
    wh = locations.for_unit(conn, ids["wh"])
    cams = [add(conn, "items", nomenclature_id=ids["cam"], serial_no=f"SN-00{i}") for i in (1, 2, 3)]
    rec = documents.create_draft(conn, "receipt", locations.for_counterparty(conn, ids["ext"]), wh,
                                 doc_date="2026-10-01")
    documents.set_lines(conn, rec, [
        {"nomenclature_id": ids["cable"], "category": 1, "price_kop": 1000, "qty_m": 100000},
        *[{"nomenclature_id": ids["cam"], "item_id": c, "category": 1, "price_kop": 446000, "qty_m": 1000}
          for c in cams]])
    documents.post(conn, rec)
    sys_nom = add(conn, "nomenclature", name="Система відеоспостереження", uom="к-т",
                  accounting_class="fixed", serial_tracked=1)
    system = add(conn, "items", nomenclature_id=sys_nom, inventory_no="СВН-1")
    return conn, wh, cams, sys_nom, system


def act(conn, wh, lines, on="2026-10-05"):
    doc = documents.create_draft(conn, "condition", wh, locations.system(conn, locations.WRITTEN_OFF),
                                 doc_date=on, basis="Об'єднання")
    documents.set_lines(conn, doc, lines)
    return doc


def merge_lines(ids, cams, sys_nom, system):
    out = [{"nomenclature_id": ids["cam"], "item_id": c, "category": 1, "price_kop": 446000, "qty_m": 1000,
            "years_norm": "10", "years_fact": "5"} for c in cams]
    out.append({"nomenclature_id": ids["cable"], "category": 1, "price_kop": 1000, "qty_m": 20000})
    # Система — одна одиниця за сумою списаного: 3 × 4 460 + 20 × 10 = 13 580 грн.
    return out + [{"nomenclature_id": sys_nom, "item_id": system, "category": 1, "price_kop": 1358000,
                   "qty_m": 1000, "direction": "in"}]


def test_merge_into_system_and_storno(env):
    storage, _, ids = env
    conn, wh, cams, sys_nom, system = setup(storage, ids)
    doc = act(conn, wh, merge_lines(ids, cams, sys_nom, system))
    documents.post(conn, doc)
    left = {(r["nomenclature_id"], r["item_id"]): r["qty_m"] for r in balances.balances(conn, [wh], "2026-10-31")}
    assert left == {(ids["cable"], None): 80000, (sys_nom, system): 1000}
    assert balances.item_position(conn, cams[0], "2026-10-31") is None
    # Списане — у «Списано», система — з «Оприбутковано за актом».
    obtained = locations.system(conn, locations.OBTAINED)
    moves = conn.execute("SELECT from_location_id, to_location_id FROM movements WHERE document_id = ? "
                         "ORDER BY id", (doc,)).fetchall()
    assert (moves[-1]["from_location_id"], moves[-1]["to_location_id"]) == (obtained, wh)
    assert moves[0]["to_location_id"] == locations.system(conn, locations.WRITTEN_OFF)
    assert documents.lines(conn, doc)[0]["years_fact"] == "5"

    # Дод. 1: камери й кабель вибули, система надійшла; дод. 2 — назва документа.
    s = {r["name"]: r for r in registers.summary(conn, f"u:{ids['wh']}", "2026-10-01", "2026-10-31").rows}
    assert s["Система відеоспостереження"]["in_m"] == 1000 and s["Камера"]["out_m"] == 3000
    j = {r["reg_no"]: r for r in registers.journal(conn, 2026).rows}
    assert j[documents.get(conn, doc)["reg_no"]]["title"] == "Акт якісного (технічного) стану"

    # Сторно повертає камери й кабель і знімає систему.
    documents.reverse(conn, doc, on_date="2026-10-06")
    left = {(r["nomenclature_id"], r["item_id"]): r["qty_m"] for r in balances.balances(conn, [wh], "2026-10-31")}
    assert left == {(ids["cable"], None): 100000, **{(ids["cam"], c): 1000 for c in cams}}
    s = {r["name"]: r for r in registers.summary(conn, f"u:{ids['wh']}", "2026-10-01", "2026-10-31").rows}
    assert "Система відеоспостереження" not in s and s["Камера"]["out_m"] == 0   # обороту немає


def test_condition_act_validation(env):
    storage, _, ids = env
    conn, wh, cams, sys_nom, system = setup(storage, ids)
    # Лише «оприбуткувати» — не акт якісного стану.
    doc = act(conn, wh, merge_lines(ids, cams, sys_nom, system)[-1:])
    assert any("немає майна, що списується" in e for e in documents.validate(conn, doc))
    # Списати більше, ніж є.
    doc = act(conn, wh, [{"nomenclature_id": ids["cable"], "category": 1, "price_kop": 1000, "qty_m": 150000}])
    assert any("доступно 100, потрібно 150" in e for e in documents.validate(conn, doc))
    # Рядок «оприбуткувати» в інших документах заборонений.
    wo = documents.create_draft(conn, "writeoff", wh, locations.system(conn, locations.WRITTEN_OFF),
                                doc_date="2026-10-05")
    documents.set_lines(conn, wo, [{"nomenclature_id": ids["cable"], "category": 1, "price_kop": 1000,
                                    "qty_m": 1000, "direction": "in"}])
    assert any("лише в акті якісного" in e for e in documents.validate(conn, wo))
    # Оприбуткувати одиницю, яка вже на обліку, не можна.
    doc = act(conn, wh, [
        {"nomenclature_id": ids["cable"], "category": 1, "price_kop": 1000, "qty_m": 1000},
        {"nomenclature_id": ids["cam"], "item_id": cams[0], "category": 1, "price_kop": 0, "qty_m": 1000,
         "direction": "in"}])
    assert any("уже на обліку" in e for e in documents.validate(conn, doc))


def test_migration_007_keeps_documents_and_triggers(tmp_path):
    conn = db.connect(tmp_path / "old.db")
    for num, f in db.migrations():
        if num <= 6:
            conn.executescript(f"BEGIN;\n{f.read_text(encoding='utf-8')}\nPRAGMA user_version = {num};\nCOMMIT;")
    conn.execute("INSERT INTO units(id, name, kind, is_accounting) VALUES (1, 'Склад', 'warehouse', 1)")
    conn.execute("INSERT INTO locations(id, kind, unit_id) VALUES (10, 'unit', 1)")
    conn.execute("INSERT INTO counterparties(id, name, kind) VALUES (1, 'А2222', 'military_unit')")
    conn.execute("INSERT INTO locations(id, kind, counterparty_id) VALUES (11, 'counterparty', 1)")
    conn.execute("INSERT INTO documents(id, doc_type, status, reg_year, reg_no, doc_no, doc_date, op_date, "
                 "from_location_id, to_location_id, created_at, posted_at) VALUES "
                 "(1, 'order', 'posted', 2026, 1, '1', '2026-10-01', '2026-10-01', 10, 11, 'x', 'x')")
    conn.execute("INSERT INTO documents(id, doc_type, status, reg_year, reg_no, doc_no, doc_date, op_date, "
                 "from_location_id, to_location_id, order_id, created_at) VALUES "
                 "(2, 'dispatch', 'draft', 2026, 2, '2', '2026-10-02', '2026-10-02', 10, 11, 1, 'x')")
    conn.commit()
    assert db.migrate(conn) == [7]
    assert conn.execute("SELECT order_id FROM documents WHERE id = 2").fetchone()[0] == 1
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    with pytest.raises(sqlite3.IntegrityError, match="Проведений документ"):
        conn.execute("UPDATE documents SET doc_no = '5' WHERE id = 1")
    with pytest.raises(sqlite3.IntegrityError, match="не видаляються"):
        conn.execute("DELETE FROM documents WHERE id = 1")


def test_web_condition_act(env):
    storage, client, ids = env
    conn, wh, cams, sys_nom, system = setup(storage, ids)
    conn.commit()
    conn.close()
    assert "єднали майно в систему" in client.get("/documents/new").get_data(as_text=True)
    head = {"from_place": f"u:{ids['wh']}", "doc_date": "2026-10-05", "basis": "Об'єднання",
            "x_group_name": "технічних засобів охорони", "x_commission_head": str(ids["mvo"]),
            "x_commission_members": [str(ids["soldier"]), ""], "x_senior_person": str(ids["mvo"]),
            "x_commission_conclusion": "Потребують об'єднання в єдину систему.",
            "x_senior_conclusion": "Доцільне об'єднання."}
    resp = client.post("/documents/new/condition", data=head)
    doc = doc_id_from(resp)
    form = client.get(f"/documents/{doc}").get_data(as_text=True)
    assert "Оприбуткувати" in form and "Експлуатується, років" in form and "Вимагається" not in form
    keys = [f"{ids['cam']}|{c}|1|446000" for c in cams] + [f"{ids['cable']}||1|1000"]
    lines = {"mode": "out", "ln_key": keys, "ln_qty": ["", "", "", "20"], "ln_note": [""] * 4,
             "ln_ynorm": ["10"] * 4, "ln_yfact": ["5", "5", "5", ""],
             "in_nom": [""], "in_name": ["Система охорони периметру"], "in_new_uom": ["к-т"],
             "in_new_class": ["fixed"], "in_new_serial": ["1"], "in_serial": [""], "in_inv": [""],
             "in_cat": ["1"], "in_price": ["13580"], "in_qty": ["1"], "in_note": [""]}
    # Поштучна система без номера — помилка з позначкою таблиці, введене не губиться.
    resp = client.post(f"/documents/{doc}", data={**head, **lines, "action": "post"})
    html = resp.get_data(as_text=True)
    assert "Оприбуткувати, рядок 1" in html and "Система охорони периметру" in html
    lines["in_inv"] = ["СОП-1"]
    resp = client.post(f"/documents/{doc}", data={**head, **lines, "action": "post"})
    assert resp.status_code == 302, resp.get_data(as_text=True)

    conn = open_db(storage)
    d = documents.get(conn, doc)
    assert d["status"] == "posted"
    ex = documents.condition_extra(d)
    assert ex["commission_members"] == [ids["soldier"]] and ex["group_name"] == "технічних засобів охорони"
    left = {conn.execute("SELECT name FROM nomenclature WHERE id = ?", (r["nomenclature_id"],)).fetchone()[0]:
            r["qty_m"] for r in balances.balances(conn, [wh], "2026-10-31")}
    assert left == {"Кабель": 80000, "Система охорони периметру": 1000}
    assert [ln["years_fact"] for ln in documents.lines(conn, doc)][:3] == ["5", "5", "5"]
    conn.close()
    view = client.get(resp.headers["Location"]).get_data(as_text=True)
    assert "оприбуткувати" in view and "списати" in view
    # Одна система, склад був порожній — заповнено зі списаного одразу після проведення.
    assert "заповнено зі списаного (4 складових)" in view and "Замінити склад списаним" in view
    assert "Списано: 4" in view and "оприбутковано: 1" in view
    # Наступний акт — з тією самою комісією.
    new = client.get("/documents/new/condition").get_data(as_text=True)
    assert f'<option value="{ids["soldier"]}" selected>' in new


def test_condition_act_print(env):
    from oblik.textutil import format_money
    from tests.test_docx import text_of
    storage, client, ids = env
    conn, wh, cams, sys_nom, system = setup(storage, ids)
    doc = act(conn, wh, merge_lines(ids, cams, sys_nom, system))
    documents.update_draft(conn, doc, extra_json={
        "group_name": "технічних засобів охорони", "commission_head": ids["mvo"],
        "commission_members": [ids["soldier"]], "commission_conclusion": "Потребують об'єднання.",
        "senior_conclusion": "Доцільне об'єднання."})
    documents.post(conn, doc)
    conn.commit()
    conn.close()
    assert f"/documents/{doc}/pdf/act_condition" in client.get(f"/documents/{doc}").get_data(as_text=True)
    resp = client.get(f"/documents/{doc}/pdf/act_condition")
    assert resp.status_code == 200 and resp.data[:5] == b"%PDF-"
    text = text_of(client.get(f"/documents/{doc}/docx/act_condition").data)
    assert "{{" not in text and "{%" not in text
    for expected in ("Додаток 1 до Порядку списання", "Акт якісного (технічного) стану № 2",
                     "технічних засобів охорони", "Списати", "Оприбуткувати", "Камера, зав. № SN-001",
                     "Система відеоспостереження, інв. № СВН-1", format_money(1358000), "Потребують об'єднання.",
                     "Доцільне об'єднання.", "Голова комісії", "Олена КОВАЛЬ", "Іван БОНДАР",
                     "на відповідальне зберігання прийняв"):
        assert expected in text, expected
    conn = open_db(storage)
    from oblik import docx_out
    c = docx_out.context(conn, doc)["cond"]
    assert len(c["rows"]) == 4 and c["rows"][0]["in_qty"] == "1" and c["rows"][1]["in_name"] == ""
    assert (c["out_qty"], c["out_sum"], c["in_qty"], c["in_sum"]) == ("23", format_money(1358000), "1",
                                                                      format_money(1358000))
    assert c["rows"][0]["ynorm"] == "10"


def test_fill_components_from_act(env):
    from oblik import condition, refs
    storage, _, ids = env
    conn, wh, cams, sys_nom, system = setup(storage, ids)
    second = add(conn, "items", nomenclature_id=sys_nom, inventory_no="СВН-2")
    lines = merge_lines(ids, cams, sys_nom, system)
    two = act(conn, wh, lines + [{**lines[-1], "item_id": second, "price_kop": 0}])
    documents.post(conn, two)
    # Дві системи — автоматично не заповнюємо (невідомо, що куди).
    assert condition.fill_after_post(conn, two) is None
    assert condition.fill_components(conn, two, system) == 4
    comps = refs.get(conn, refs.REFS["items"], system)["components"]
    assert [(c["name"], c["qty_m"], c["serial_no"], c["note"]) for c in comps] == [
        ("Камера", 1000, "SN-001", "кат. I"), ("Камера", 1000, "SN-002", "кат. I"),
        ("Камера", 1000, "SN-003", "кат. I"), ("Кабель", 20000, None, "кат. I")]
    row = conn.execute("SELECT details FROM audit_log WHERE entity = 'items' AND entity_id = ? "
                       "AND action = 'update'", (system,)).fetchone()
    assert row is not None and "Кабель — 20 м" in row[0]
    # Чужа одиниця й непроведений акт — відмова.
    from oblik.documents import DocumentError
    with pytest.raises(DocumentError, match="не оприбуткована"):
        condition.fill_components(conn, two, cams[0])
    draft = act(conn, wh, lines)
    with pytest.raises(DocumentError, match="проведеного"):
        condition.fill_components(conn, draft, system)
