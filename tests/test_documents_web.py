"""Документи через веб-інтерфейс: повний цикл."""
from __future__ import annotations

import re

import pytest

from oblik import balances, db, documents, locations, refs
from oblik.config import Paths
from oblik.storage import Storage
from oblik.web import create_app


def add(conn, ref_kind, **values):
    ref = refs.REFS[ref_kind]
    full = {f.name: f.default for f in ref.fields}
    full.update(values)
    return refs.save(conn, ref, None, full)


@pytest.fixture
def env(tmp_path):
    storage = Storage(Paths(root=tmp_path / "Флешка", local=tmp_path / "ПК"))
    storage.open()
    conn = db.connect(storage.work_db)
    ids = {}
    ids["mvo"] = add(conn, "persons", last_name="Коваль", first_name="Олена", roles=["mvo"])
    ids["wh"] = add(conn, "units", name="Склад ТЗО", kind="warehouse", is_accounting=1,
                    mvo_person_id=ids["mvo"])
    ids["soldier"] = add(conn, "persons", last_name="Бондар", first_name="Іван")
    ids["ext"] = add(conn, "counterparties", name="Вища частина", kind="higher_unit")
    ids["cable"] = add(conn, "nomenclature", name="Кабель", uom="м")
    ids["cam"] = add(conn, "nomenclature", name="Камера", uom="шт.", accounting_class="fixed",
                     serial_tracked=1)
    conn.commit()
    conn.close()
    app = create_app(storage)
    app.config["TESTING"] = True
    return storage, app.test_client(), ids


def doc_id_from(resp) -> int:
    return int(re.search(r"/documents/(\d+)$", resp.headers["Location"]).group(1))


def open_db(storage):
    return db.connect(storage.work_db)


def test_pages_open(env):
    _, client, _ = env
    for url in ["/documents", "/documents/new", "/documents/new/receipt", "/documents/new/transfer",
                "/documents/new/opening", "/documents/new/writeoff", "/help", "/about"]:
        assert client.get(url).status_code == 200, url
    html = client.get("/help").get_data(as_text=True)
    assert "Сторно" in html and "п. 7 розд. I" in html
    assert "GNU GENERAL PUBLIC LICENSE" in client.get("/about").get_data(as_text=True)
    # Значок-підказка є в довідниках.
    assert 'class="hint"' in client.get("/refs/units/new").get_data(as_text=True)


def test_full_document_cycle(env):
    storage, client, ids = env

    # 1. Надходження: шапка → чернетка.
    resp = client.post("/documents/new/receipt", data={
        "from_place": f"c:{ids['ext']}", "to_place": f"u:{ids['wh']}",
        "doc_date": "2026-10-01", "basis": "Наряд № 47"})
    assert resp.status_code == 302
    receipt = doc_id_from(resp)

    # 2. Рядки: кабель і камера з новим заводським номером.
    resp = client.post(f"/documents/{receipt}", data={
        "mode": "in", "action": "save", "from_place": f"c:{ids['ext']}", "to_place": f"u:{ids['wh']}",
        "doc_date": "2026-10-01", "reg_no": "1", "doc_no": "1", "basis": "Наряд № 47",
        "ln_nom": [str(ids["cable"]), str(ids["cam"]), ""],
        "ln_serial": ["", "SN-777", ""], "ln_inv": ["", "", ""],
        "ln_cat": ["1", "1", ""], "ln_price": ["12,44", "4 460,00", ""],
        "ln_qty": ["300", "", ""], "ln_req": ["", "", ""], "ln_note": ["", "", ""]})
    assert resp.status_code == 302
    conn = open_db(storage)
    lines = documents.lines(conn, receipt)
    assert [(ln["qty_m"], ln["price_kop"]) for ln in lines] == [(300_000, 1244), (1000, 446000)]
    item = conn.execute("SELECT id FROM items WHERE serial_no = 'SN-777'").fetchone()["id"]
    conn.close()

    # 3. Перевірка і проведення.
    html = client.post(f"/documents/{receipt}", data={**_same(storage, receipt), "action": "check"}
                       ).get_data(as_text=True)
    assert "усе гаразд" in html
    resp = client.post(f"/documents/{receipt}", data={**_same(storage, receipt), "action": "post"})
    assert resp.status_code == 302
    html = client.get(f"/documents/{receipt}").get_data(as_text=True)
    assert "Проведено" in html and "Сторнувати" in html

    # 4. Видача солдату з залишку: список залишків містить кабель і камеру.
    resp = client.post("/documents/new/transfer", data={
        "from_place": f"u:{ids['wh']}", "to_place": f"p:{ids['soldier']}", "doc_date": "2026-10-02"})
    transfer = doc_id_from(resp)
    html = client.get(f"/documents/{transfer}").get_data(as_text=True)
    assert "є 300 м" in html and "SN-777" in html
    cable_key = f"{ids['cable']}||1|1244"
    cam_key = f"{ids['cam']}|{item}|1|446000"

    # Забагато — помилка, а введені рядки не губляться.
    form = {"mode": "out", "from_place": f"u:{ids['wh']}", "to_place": f"p:{ids['soldier']}",
            "doc_date": "2026-10-02", "reg_no": "2", "doc_no": "2",
            "ln_key": [cable_key, cam_key], "ln_qty": ["500", ""], "ln_req": ["", ""], "ln_note": ["", ""]}
    html = client.post(f"/documents/{transfer}", data={**form, "action": "post"}).get_data(as_text=True)
    assert "доступно 300, потрібно 500" in html
    assert 'value="500"' in html

    form["ln_qty"] = ["120", ""]
    assert client.post(f"/documents/{transfer}", data={**form, "action": "post"}).status_code == 302
    conn = open_db(storage)
    soldier_loc = locations.for_person(conn, ids["soldier"])
    assert balances.item_position(conn, item)["location_id"] == soldier_loc
    assert sum(r["qty_m"] for r in balances.balances(conn, [soldier_loc], nomenclature_id=ids["cable"])) == 120_000
    conn.close()

    # 5. Сторно видачі.
    resp = client.post(f"/documents/{transfer}/reverse", data={"on_date": "2026-10-03", "basis": "помилка"})
    assert resp.status_code == 302
    storno = doc_id_from(resp)
    assert "Це сторно" in client.get(f"/documents/{storno}").get_data(as_text=True)
    assert "сторновано" in client.get(f"/documents/{transfer}").get_data(as_text=True)
    conn = open_db(storage)
    assert balances.item_position(conn, item)["location_id"] != soldier_loc
    conn.close()

    # 6. Анулювання чернетки.
    resp = client.post("/documents/new/transfer", data={
        "from_place": f"u:{ids['wh']}", "to_place": f"p:{ids['soldier']}", "doc_date": "2026-10-04"})
    draft = doc_id_from(resp)
    client.post(f"/documents/{draft}", data={"action": "cancel"})
    conn = open_db(storage)
    assert documents.get(conn, draft)["status"] == "cancelled"
    conn.close()

    # Журнал документів показує все.
    html = client.get("/documents?year=2026").get_data(as_text=True)
    assert "Анульовано" in html and "Сторно" in html


def _same(storage, doc_id) -> dict:
    """Форма, що повторює поточний стан чернетки (шапка + рядки)."""
    conn = open_db(storage)
    doc = documents.get(conn, doc_id)
    from oblik.web.documents import location_to_place
    lines = documents.lines(conn, doc_id)
    items = {ln["item_id"]: conn.execute("SELECT * FROM items WHERE id = ?", (ln["item_id"],)).fetchone()
             for ln in lines if ln["item_id"]}
    data = {"mode": "in", "from_place": location_to_place(conn, doc["from_location_id"], "from"),
            "to_place": location_to_place(conn, doc["to_location_id"]),
            "doc_date": doc["doc_date"], "reg_no": str(doc["reg_no"]), "doc_no": doc["doc_no"],
            "basis": doc["basis"] or "",
            "ln_nom": [str(ln["nomenclature_id"]) for ln in lines],
            "ln_serial": [items[ln["item_id"]]["serial_no"] if ln["item_id"] else "" for ln in lines],
            "ln_inv": ["" for _ in lines],
            "ln_cat": [str(ln["category"] or "") for ln in lines],
            "ln_price": [str(ln["price_kop"] / 100).replace(".", ",") for ln in lines],
            "ln_qty": [str(ln["qty_m"] // 1000) for ln in lines],
            "ln_req": ["" for _ in lines], "ln_note": ["" for _ in lines]}
    conn.close()
    return data


def test_header_errors_shown(env):
    _, client, ids = env
    resp = client.post("/documents/new/receipt", data={"from_place": "", "to_place": f"u:{ids['wh']}"})
    assert resp.status_code == 200
    assert "Звідки" in resp.get_data(as_text=True)


def test_new_nomenclature_typed_in_line(env):
    storage, client, ids = env
    resp = client.post("/documents/new/receipt", data={
        "from_place": f"c:{ids['ext']}", "to_place": f"u:{ids['wh']}", "doc_date": "2026-10-01"})
    doc = doc_id_from(resp)
    base = {"mode": "in", "action": "save", "from_place": f"c:{ids['ext']}",
            "to_place": f"u:{ids['wh']}", "doc_date": "2026-10-01"}
    # Нове кількісне, нове поштучне і наявне (вписане назвою, у іншому регістрі).
    resp = client.post(f"/documents/{doc}", data={**base,
        "ln_nom": ["", "", ""], "ln_name": ["Акумулятор  18650", "Рація Р-1", "кабель"],
        "ln_new_uom": ["шт.", "к-т", "шт."], "ln_new_class": ["inventory", "fixed", "inventory"],
        "ln_new_serial": ["0", "1", "0"],
        "ln_serial": ["", "SN-001", ""], "ln_inv": ["", "", ""], "ln_cat": ["1", "1", "1"],
        "ln_price": ["150", "20 000", "10"], "ln_qty": ["4", "", "5"],
        "ln_req": ["", "", ""], "ln_note": ["", "", ""]})
    assert resp.status_code == 302
    conn = open_db(storage)
    noms = {r["name"]: r for r in conn.execute("SELECT * FROM nomenclature")}
    assert set(noms) == {"Кабель", "Камера", "Акумулятор 18650", "Рація Р-1"}
    assert noms["Рація Р-1"]["serial_tracked"] == 1 and noms["Рація Р-1"]["uom"] == "к-т"
    assert noms["Рація Р-1"]["accounting_class"] == "fixed"
    lines = documents.lines(conn, doc)
    assert [ln["nomenclature_id"] for ln in lines] == [
        noms["Акумулятор 18650"]["id"], noms["Рація Р-1"]["id"], ids["cable"]]
    assert lines[1]["item_id"] is not None
    # У журналі змін видно, що найменування додано.
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE entity = 'nomenclature' "
                        "AND action = 'create'").fetchone()[0] == 4
    conn.close()
    # Повторне введення тієї ж назви не дублює довідник.
    client.post(f"/documents/{doc}", data={**base,
        "ln_nom": [""], "ln_name": ["акумулятор 18650"], "ln_new_uom": ["шт."],
        "ln_new_class": ["inventory"], "ln_new_serial": ["0"], "ln_serial": [""], "ln_inv": [""],
        "ln_cat": ["1"], "ln_price": ["150"], "ln_qty": ["1"], "ln_req": [""], "ln_note": [""]})
    conn = open_db(storage)
    assert conn.execute("SELECT COUNT(*) FROM nomenclature").fetchone()[0] == 4
    conn.close()
    # Сторінка показує підказки зі списку і панель нового найменування.
    html = client.get(f"/documents/{doc}").get_data(as_text=True)
    assert 'id="nom-list"' in html and "Акумулятор 18650" in html


def test_new_nomenclature_not_created_when_line_has_error(env):
    storage, client, ids = env
    resp = client.post("/documents/new/receipt", data={
        "from_place": f"c:{ids['ext']}", "to_place": f"u:{ids['wh']}", "doc_date": "2026-10-01"})
    doc = doc_id_from(resp)
    resp = client.post(f"/documents/{doc}", data={
        "mode": "in", "action": "save", "from_place": f"c:{ids['ext']}",
        "to_place": f"u:{ids['wh']}", "doc_date": "2026-10-01",
        "ln_nom": ["", ""], "ln_name": ["Ліхтар", "Рація"], "ln_new_uom": ["шт.", "шт."],
        "ln_new_class": ["inventory", "fixed"], "ln_new_serial": ["0", "1"],
        "ln_serial": ["", ""], "ln_inv": ["", ""], "ln_cat": ["1", "1"], "ln_price": ["10", "10"],
        "ln_qty": ["абв", ""], "ln_req": ["", ""], "ln_note": ["", ""]})
    html = resp.get_data(as_text=True)
    assert "Рядок 1" in html and "Рядок 2" in html and "поштучно" in html
    assert 'value="Ліхтар"' in html   # введене не губиться
    conn = open_db(storage)
    assert conn.execute("SELECT COUNT(*) FROM nomenclature").fetchone()[0] == 2
    conn.close()


def test_opening_to_mvo_person_warns(env):
    storage, client, ids = env
    resp = client.post("/documents/new/opening", data={"to_place": f"p:{ids['mvo']}", "doc_date": "2026-10-01"})
    doc = doc_id_from(resp)
    html = client.get(f"/documents/{doc}").get_data(as_text=True)
    assert "Перевірте «Куди»" in html and "Склад ТЗО" in html
    # На звичайну особу (не МВО) — без попередження; на місце обліку — теж.
    resp = client.post("/documents/new/opening", data={"to_place": f"p:{ids['soldier']}", "doc_date": "2026-10-01"})
    assert "Перевірте «Куди»" not in client.get(f"/documents/{doc_id_from(resp)}").get_data(as_text=True)
    resp = client.post("/documents/new/opening", data={"to_place": f"u:{ids['wh']}", "doc_date": "2026-10-01"})
    assert "Перевірте «Куди»" not in client.get(f"/documents/{doc_id_from(resp)}").get_data(as_text=True)
