"""Виконання наряду: документи за нарядом, графи 8–9, попередження, чернетка за нарядом."""
from __future__ import annotations

import re
from datetime import date

import pytest

from oblik import documents, docx_out, locations, orders
from oblik.documents import DocumentError
from tests.test_docx import text_of
from tests.test_documents_web import add, doc_id_from, env, open_db  # noqa: F401 — фікстура env


def setup(storage, ids):
    """Склад отримав 100 м кабелю і три камери; наряд: видати іншій частині 60 м і 2 камери."""
    conn = open_db(storage)
    other = add(conn, "counterparties", name="Військова частина А2222", kind="military_unit")
    wh = locations.for_unit(conn, ids["wh"])
    ext, dst = locations.for_counterparty(conn, ids["ext"]), locations.for_counterparty(conn, other)
    cams = [add(conn, "items", nomenclature_id=ids["cam"], serial_no=f"SN-00{i}") for i in (1, 2, 3)]
    rec = documents.create_draft(conn, "receipt", ext, wh, doc_date="2026-10-01")
    documents.set_lines(conn, rec, [
        {"nomenclature_id": ids["cable"], "category": 1, "price_kop": 1000, "qty_m": 100000},
        *[{"nomenclature_id": ids["cam"], "item_id": c, "category": 1, "price_kop": 446000, "qty_m": 1000}
          for c in cams]])
    documents.post(conn, rec)
    order = documents.create_draft(conn, "order", wh, dst, doc_date="2026-10-02", valid_until="2026-10-20")
    documents.set_lines(conn, order, [
        {"nomenclature_id": ids["cable"], "category": 1, "price_kop": 1000, "qty_m": 60000},
        {"nomenclature_id": ids["cam"], "category": 1, "price_kop": 446000, "qty_m": 2000}])
    documents.post(conn, order)
    conn.commit()
    return conn, order, wh, dst, cams


def dispatch(conn, order, wh, dst, lines, on="2026-10-05"):
    doc = documents.create_draft(conn, "dispatch", wh, dst, doc_date=on, order_id=order)
    documents.set_lines(conn, doc, lines)
    return doc


def test_execution_counts_posted_not_reversed(env):
    storage, _, ids = env
    conn, order, wh, dst, cams = setup(storage, ids)
    ex = orders.execution(conn, order, today=date(2026, 10, 3))
    assert ex.state == "none" and not ex.expired
    assert [x.remaining_m for x in ex.lines] == [60000, 2000]

    d1 = dispatch(conn, order, wh, dst, [
        {"nomenclature_id": ids["cable"], "category": 1, "price_kop": 1000, "qty_m": 40000},
        {"nomenclature_id": ids["cam"], "item_id": cams[0], "category": 1, "price_kop": 446000, "qty_m": 1000}])
    # Чернетка у виконання не йде.
    assert orders.execution(conn, order).state == "none"
    documents.post(conn, d1)
    ex = orders.execution(conn, order, today=date(2026, 10, 25))
    assert ex.state == "partial" and ex.expired
    assert [x.fact_m for x in ex.lines] == [40000, 1000]
    assert ex.lines[0].docs == [(d1, "Накладна № 3 від 05.10.2026")]

    # Сторно знімає документ з виконання.
    documents.reverse(conn, d1, on_date="2026-10-06")
    assert orders.execution(conn, order).state == "none"

    d2 = dispatch(conn, order, wh, dst, [
        {"nomenclature_id": ids["cable"], "category": 1, "price_kop": 1000, "qty_m": 60000},
        *[{"nomenclature_id": ids["cam"], "item_id": c, "category": 1, "price_kop": 446000, "qty_m": 1000}
          for c in cams[:2]]], on="2026-10-07")
    documents.post(conn, d2)
    ex = orders.execution(conn, order, today=date(2026, 10, 25))
    assert ex.state == "done" and not ex.expired
    # Виконаного наряду немає серед пропонованих для нових документів.
    assert order not in [oid for oid, _ in orders.order_options(conn)]
    assert order in [oid for oid, _ in orders.order_options(conn, current=order)]
    assert orders.print_columns(conn, order) == [("60", "Накладна № 5 від 07.10.2026"),
                                                 ("2", "Накладна № 5 від 07.10.2026")]


def test_warnings_do_not_block_posting(env):
    storage, _, ids = env
    conn, order, wh, dst, cams = setup(storage, ids)
    soap = add(conn, "nomenclature", name="Мило", uom="шт.")
    rec = documents.create_draft(conn, "receipt", locations.for_counterparty(conn, ids["ext"]), wh,
                                 doc_date="2026-10-01")
    documents.set_lines(conn, rec, [{"nomenclature_id": soap, "category": 1, "price_kop": 100, "qty_m": 5000}])
    documents.post(conn, rec)
    doc = dispatch(conn, order, wh, dst, [
        {"nomenclature_id": ids["cable"], "category": 1, "price_kop": 1000, "qty_m": 70000},
        {"nomenclature_id": soap, "category": 1, "price_kop": 100, "qty_m": 1000}], on="2026-10-25")
    w = orders.warnings(conn, doc)
    assert any("лишилося видати 60 м, у документі — 70 м" in x for x in w)
    assert any("«Мило» — немає в наряді" in x for x in w)
    assert any("Строк дії наряду закінчився 20.10.2026" in x for x in w)
    documents.post(conn, doc)   # лише попередження — проводиться
    assert orders.execution(conn, order).lines[0].fact_m == 70000


def test_link_must_be_signed_order(env):
    storage, _, ids = env
    conn, order, wh, dst, _ = setup(storage, ids)
    draft_order = documents.create_draft(conn, "order", wh, dst, doc_date="2026-10-02")
    doc = dispatch(conn, draft_order, wh, dst, [
        {"nomenclature_id": ids["cable"], "category": 1, "price_kop": 1000, "qty_m": 1000}])
    with pytest.raises(DocumentError, match="ще не підписано"):
        documents.post(conn, doc)
    # Посилання на наряд — у журналі змін як реквізит.
    documents.update_draft(conn, doc, order_id=order)
    row = conn.execute("SELECT details FROM audit_log WHERE entity = 'documents' AND entity_id = ? AND action = 'update'",
                       (doc,)).fetchone()
    assert "За нарядом" in row[0] and "Наряд на видавання (приймання) №" in row[0]


def test_create_draft_from_order(env):
    storage, _, ids = env
    conn, order, wh, dst, cams = setup(storage, ids)
    doc = orders.create_draft(conn, order, on_date="2026-10-05")
    d = documents.get(conn, doc)
    assert (d["doc_type"], d["from_location_id"], d["to_location_id"], d["order_id"]) == \
        ("dispatch", wh, dst, order)
    assert d["basis"] == "Наряд № 2 від 02.10.2026"
    got = [(ln["nomenclature_id"], ln["item_id"], ln["qty_m"]) for ln in documents.lines(conn, doc)]
    # Кабель — з залишку; камери — підібрано дві одиниці.
    assert got == [(ids["cable"], None, 60000), (ids["cam"], cams[0], 1000), (ids["cam"], cams[1], 1000)]
    assert orders.warnings(conn, doc) == []
    documents.post(conn, doc)
    assert orders.execution(conn, order).state == "done"
    with pytest.raises(DocumentError, match="уже виконано"):
        orders.create_draft(conn, order)


def test_order_between_other_units_is_not_formed_here(env):
    storage, _, ids = env
    conn, *_ = setup(storage, ids)
    a = locations.for_counterparty(conn, ids["ext"])
    b = locations.for_counterparty(conn, add(conn, "counterparties", name="А3333", kind="military_unit"))
    order = documents.create_draft(conn, "order", a, b, doc_date="2026-10-02")
    documents.set_lines(conn, order, [{"nomenclature_id": ids["cable"], "category": 1, "price_kop": 0, "qty_m": 1000}])
    documents.post(conn, order)
    with pytest.raises(DocumentError, match="інші частини між собою"):
        orders.create_draft(conn, order)


def test_web_execute_and_print(env):
    storage, client, ids = env
    conn, order, *_ = setup(storage, ids)
    conn.close()
    view = client.get(f"/documents/{order}").get_data(as_text=True)
    assert "Виконання наряду" in view and "Не виконано" in view
    assert f"/documents/{order}/execute" in view

    resp = client.post(f"/documents/{order}/execute")
    doc = doc_id_from(resp)
    edit = client.get(f"/documents/{doc}").get_data(as_text=True)
    assert "За нарядом" in edit and f'value="{order}" selected' in edit
    resp = client.post("/documents/new/dispatch", data={
        "from_place": f"u:{ids['wh']}", "to_place": "", "doc_date": "2026-10-05", "order_id": str(order)})
    # «Куди» порожнє — помилка, але вибраний наряд не губиться.
    assert resp.status_code == 200 and f'value="{order}" selected' in resp.get_data(as_text=True)

    conn = open_db(storage)
    documents.post(conn, doc)
    conn.commit()
    conn.close()
    ref = f"Накладна № 3 від {date.today():%d.%m.%Y}"   # чернетка за нарядом — сьогоднішньою датою
    view = client.get(f"/documents/{order}").get_data(as_text=True)
    assert "Виконано" in view and ref in view
    assert "/execute" not in view and "Сторнувати" not in view
    assert "Оформлено за нарядом" in client.get(f"/documents/{doc}").get_data(as_text=True)
    text = text_of(client.get(f"/documents/{order}/docx/naryad").data)
    assert ref in text
    conn = open_db(storage)
    ctx = docx_out.context(conn, order)
    assert [ln["fact"] for ln in ctx["lines"]] == ["60", "2"]


# --- Ручні відмітки, прострочені наряди --------------------------------------------

def test_manual_marks(env):
    storage, _, ids = env
    conn, order, wh, dst, _ = setup(storage, ids)
    cable_line, cam_line = [ln["id"] for ln in documents.lines(conn, order)]
    m = orders.add_mark(conn, order, cable_line, 60000, "  Акт   № 12 від 05.10.2026 ", "2026-10-06")
    orders.add_mark(conn, order, cam_line, 1000, "Акт № 12 від 05.10.2026", "2026-10-06")
    ex = orders.execution(conn, order)
    assert ex.state == "partial"
    assert ex.lines[0].docs == [(None, "Акт № 12 від 05.10.2026")]
    assert orders.print_columns(conn, order)[1] == ("1", "Акт № 12 від 05.10.2026")
    # Відмітку не правлять — лише видаляють (із записом у журналі).
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE order_marks SET qty_m = 1 WHERE id = ?", (m,))
    assert orders.delete_mark(conn, m) == order
    assert orders.execution(conn, order).lines[0].fact_m == 0
    actions = [r[0] for r in conn.execute("SELECT action FROM audit_log WHERE entity = 'documents' "
                                          "AND entity_id = ? ORDER BY id", (order,))]
    assert actions[-3:] == ["mark", "mark", "unmark"]

    with pytest.raises(DocumentError) as exc:
        orders.add_mark(conn, order, cable_line, 0, " ")
    assert len(exc.value.errors) == 2
    # До чернетки наряду й до чужого рядка — ні в коді, ні в базі.
    draft = documents.create_draft(conn, "order", wh, dst, doc_date="2026-10-02")
    documents.set_lines(conn, draft, [{"nomenclature_id": ids["cable"], "category": 1, "price_kop": 0, "qty_m": 1000}])
    draft_line = documents.lines(conn, draft)[0]["id"]
    with pytest.raises(DocumentError, match="підписаному"):
        orders.add_mark(conn, draft, draft_line, 1000, "Акт № 1 від 01.10.2026")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO order_marks(order_id, line_id, qty_m, doc_ref, mark_date, created_at) "
                     "VALUES (?, ?, 1000, 'x', '2026-10-01', '2026-10-01')", (order, draft_line))


def test_overdue_orders_and_reminders(env):
    from oblik import dashboard
    storage, client, ids = env
    conn, order, *_ = setup(storage, ids)   # дійсний до 20.10.2026, не виконаний
    assert orders.overdue(conn, date(2026, 10, 20)) == []
    assert [r["id"] for r in orders.overdue(conn, date(2026, 10, 18), soon_days=3)] == [order]
    assert orders.overdue(conn, date(2026, 10, 16), soon_days=3) == []
    assert [r["id"] for r in orders.overdue(conn, date(2026, 10, 21))] == [order]
    texts = [r.text for r in dashboard.reminders(conn, today=date(2026, 10, 21))]
    assert any("строк дії яких минув" in t for t in texts)
    texts = [r.text for r in dashboard.reminders(conn, today=date(2026, 10, 18))]
    assert any("спливає" in t and "до 20.10.2026" in t for t in texts)
    for ln in documents.lines(conn, order):
        orders.add_mark(conn, order, ln["id"], ln["qty_m"], "Акт № 7 від 10.10.2026")
    assert orders.overdue(conn, date(2026, 10, 21)) == []
    conn.commit()
    conn.close()


def test_web_marks_and_list_filter(env):
    storage, client, ids = env
    conn, order, *_ = setup(storage, ids)
    line = documents.lines(conn, order)[0]["id"]
    conn.close()
    view = client.get(f"/documents/{order}").get_data(as_text=True)
    assert "Відмітити виконання вручну" in view and f"/documents/{order}/marks" in view
    resp = client.post(f"/documents/{order}/marks", data={"line_id": str(line), "qty": "abc", "doc_ref": "Акт"})
    assert "Відмітку не збережено" in client.get(resp.headers["Location"]).get_data(as_text=True)
    resp = client.post(f"/documents/{order}/marks", data={
        "line_id": str(line), "qty": "60", "doc_ref": "Акт № 12 від 05.10.2026", "mark_date": "2026-10-06"})
    view = client.get(resp.headers["Location"]).get_data(as_text=True)
    assert "Відмітку про виконання збережено" in view and "06.10.2026: рядок 1 — видано 60" in view
    # Рядок 1 видано повністю — у формі за замовчуванням наступний.
    line2 = re.findall(r'<option value="(\d+)" selected>', view)
    assert line2 and int(line2[0]) != line
    assert "Виконано частково" in client.get("/documents?year=2026").get_data(as_text=True)
    lst = client.get("/documents?exec=open").get_data(as_text=True)
    assert "Наряди: не виконані (за всі роки)" in lst and f"/documents/{order}" in lst
    conn = open_db(storage)
    mark = orders.marks(conn, order)[0]["id"]
    conn.close()
    resp = client.post(f"/documents/marks/{mark}/delete")
    assert "Відмітку видалено" in client.get(resp.headers["Location"]).get_data(as_text=True)


def test_migration_006_on_existing_base(tmp_path):
    from oblik import db
    conn = db.connect(tmp_path / "old.db")
    for num, f in db.migrations():
        if num <= 5:
            conn.executescript(f"BEGIN;\n{f.read_text(encoding='utf-8')}\nPRAGMA user_version = {num};\nCOMMIT;")
    assert db.migrate(conn)[0] == 6   # далі — новіші міграції
    assert conn.execute("SELECT COUNT(*) FROM order_marks").fetchone()[0] == 0
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
