"""Головна: показники і нагадування."""
from __future__ import annotations

from datetime import date

from oblik import dashboard, db, documents
from tests import test_posting
from tests.test_posting import World


def make(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.migrate(conn)
    db.init_meta(conn, "test")
    world = World(conn)
    test_posting.W = world
    return conn, world


def cam(w, item, price=446000):
    return {"nomenclature_id": w.camera, "item_id": item, "qty_m": 1000, "price_kop": price, "category": 1}


def test_metrics(tmp_path):
    conn, w = make(tmp_path)
    w.doc("receipt", w.ext, w.wh, [test_posting.cable(100, price=1000), cam(w, w.cam1), cam(w, w.cam2)],
          op_date="2026-10-01")
    w.doc("transfer", w.wh, w.person, [cam(w, w.cam1)], op_date="2026-10-03")
    w.doc("transfer", w.wh, w.coy, [test_posting.cable(10, price=1000)], op_date="2026-10-04", post=False)
    m = dashboard.metrics(conn, date(2026, 10, 8))
    assert m["total_kop"] == 100 * 1000 + 2 * 446000
    assert m["names"] == 2 and m["serial_units"] == 2
    assert m["issued"] == "4 460,00" and m["issued_persons"] == 1  # нерозривний пробіл
    assert m["posted"] == 2 and m["drafts"] == 1 and m["month_docs"] == 2
    # Видане бійцю (за замовчуванням — рота) іде в смугу роти.
    units = {b["title"]: b for b in m["by_unit"]}
    assert set(units) == {"Склад ТЗО", "1 рота"} and units["Склад ТЗО"]["width"] == 100
    classes = {b["title"] for b in m["by_class"]}
    assert classes == {"Основні засоби", "Запаси"}
    # До надходження — порожньо, без ділення на нуль.
    empty = dashboard.metrics(conn, date(2026, 9, 1))
    assert empty["total_kop"] == 0 and empty["by_class"] == [] and empty["issued_share"] == 0


def test_reminders(tmp_path):
    conn, w = make(tmp_path)
    levels = {r.text.split(":")[0]: r.level for r in dashboard.reminders(conn, None, date(2026, 10, 10))}
    assert levels["Не заповнено налаштування"] == "critical"
    texts = " ".join(r.text for r in dashboard.reminders(conn, None, date(2026, 10, 10)))
    assert "Облік ще не розпочато" in texts and "звання чи посаду" in texts
    # Стара чернетка.
    draft = w.doc("receipt", w.ext, w.wh, [test_posting.cable(1)], post=False)
    conn.execute("UPDATE documents SET created_at = '2026-10-01T10:00:00' WHERE id = ?", (draft,))
    texts = " ".join(r.text for r in dashboard.reminders(conn, None, date(2026, 10, 10)))
    assert "Чернеток, яким більше 3 днів: 1" in texts
    documents.cancel(conn, draft)
    # Кінець місяця — підсумки (лише коли облік розпочато).
    for key, value in (("unit_name", "ВЧ А0000"), ("unit_code", "А0000"), ("operator_name", "Тест Т.")):
        conn.execute("INSERT INTO settings(key, value) VALUES (?, ?)", (key, value))
    w.doc("opening", w.opening, w.wh, [test_posting.cable(5)], op_date="2026-10-01")
    rs = dashboard.reminders(conn, None, date(2026, 11, 3))
    summary = [r for r in rs if r.link == "reports.summary"]
    assert summary and summary[0].link_args == {"from": "2026-10-01", "to": "2026-10-31"}
    assert not [r for r in dashboard.reminders(conn, None, date(2026, 10, 15)) if r.link == "reports.summary"]
    assert [r.level for r in rs] == sorted([r.level for r in rs], key=["critical", "warning", "info"].index)


def test_home_page_renders_numbers(tmp_path):
    from oblik.config import Paths
    from oblik.storage import Storage
    from oblik.web import create_app
    storage = Storage(Paths(root=tmp_path / "Флешка", local=tmp_path / "ПК"))
    storage.open()
    conn = db.connect(storage.work_db)
    w = World(conn)
    test_posting.W = w
    w.doc("receipt", w.ext, w.wh, [cam(w, w.cam1)], op_date="2026-10-01")
    conn.commit()
    conn.close()
    app = create_app(storage)
    app.config["TESTING"] = True
    html = app.test_client().get("/").get_data(as_text=True)
    assert "1 од. з номерами" in html and "built-in" not in html
    assert "Майна на обліку" in html and "Технічна інформація" in html
