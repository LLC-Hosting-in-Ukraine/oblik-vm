"""Імпорт оборотки з бухгалтерії. Дані — вигадані, форма — як у реальної оборотки."""
from __future__ import annotations

import base64
import re
from decimal import Decimal
from io import BytesIO

import pytest
from openpyxl import Workbook

from oblik import balances, documents, locations, oborotka
from oblik.documents import DocumentError
from tests.test_documents_web import add, env, open_db  # noqa: F401 — фікстура env

HEAD = ["Рахунок", "Контрагент (найменування)", "ПБО", "Найменування номенклатури", "Основна одиниця виміру",
        "Номенклатура", "Ціна", "Кількість", "Сума"]
KOVAL, BONDAR, OTHER = "Коваль Олена Іванівна", "Бондар Іван Петрович", "Шевченко Тарас Григорович"
ROWS = [
    ["1014", KOVAL, "ВЧ А0000", "Камера відеоспостереження купольна", "шт.", "10140000001", 4460, 1, 4460],
    ["1014", KOVAL, "ВЧ А0000", "Відеореєстратор 16-канальний", "од.", "10140000002", 12000.555, 1, 12000.56],
    ["1515", KOVAL, "ВЧ А0000", "Кабель КПП-ВП 2х0,5", "м", "15150000001", 10.5, 250, 2625],
    ["1812", BONDAR, "ВЧ А0000", "Ліхтар налобний", "шт.", "18120000001", 350.333, 4, 1401.33],
    ["1515", OTHER, "ВЧ А0000", "Папір А4", "уп.", "15150000002", 210, 10, 2100],
]


def xlsx(rows=ROWS, head=HEAD) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(head)
    for r in rows:
        ws.append(r)
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_read_and_defaults():
    data = xlsx(ROWS + [["Разом", "", "", "", "", "", "", 265, 22586.89]])
    assert oborotka.is_oborotka(data)
    ob = oborotka.read(data)
    assert ob.errors == [] and len(ob.rows) == 5          # «Разом» пропущено
    assert [(m.name, m.rows) for m in ob.mvos] == [(BONDAR, 1), (KOVAL, 3), (OTHER, 1)]
    acc = {a.code: (a.accounting_class, a.serial, a.all_one) for a in ob.accounts}
    assert acc == {"1014": ("fixed", True, True), "1515": ("inventory", False, False),
                   "1812": ("mshp", False, False)}
    # Наш шаблон і чужі файли — не оборотка.
    assert not oborotka.is_oborotka(xlsx([], head=["Найменування", "Кількість"]))
    assert not oborotka.is_oborotka(b"not excel")
    # Інші назви граф (як у старих оборотках) теж розпізнаються.
    alt = ["Рахунок", "Контрагент (МВО)", "ПБО", "Найменування номенклатури", "Основна одиниця виміру",
           "Номенклатура", "Нова ціна (бухгалтерська оцінка)", "Кількість на кінець", "Залишок на кінець"]
    assert len(oborotka.read(xlsx(head=alt)).rows) == 5


def test_create_drafts_for_chosen_mvo(env):
    storage, _, ids = env
    conn = open_db(storage)
    wh = locations.for_unit(conn, ids["wh"])
    ob = oborotka.read(xlsx())
    classes = {a.code: (a.accounting_class, a.serial) for a in ob.accounts}
    res = oborotka.create_drafts(conn, xlsx(), oborotka.Plan({KOVAL: wh, BONDAR: wh}, classes, "2026-06-11"))
    assert len(res.doc_ids) == 1 and res.lines == 4 and res.new_names == 4
    # 12000,555 → 12000,56 збігається із сумою; 4 × 350,333 = 1401,33, а 4 × 350,33 = 1401,32.
    assert res.rounding_rows == 1 and res.rounding == Decimal("0.01")
    doc = documents.get(conn, res.doc_ids[0])
    assert (doc["doc_type"], doc["status"], doc["to_location_id"], doc["op_date"]) == \
        ("opening", "draft", wh, "2026-06-11")
    documents.post(conn, doc["id"])
    names = {r["id"]: r for r in conn.execute("SELECT * FROM nomenclature")}
    left = sorted((names[r["nomenclature_id"]]["name"], r["category"], r["qty_m"], r["price_kop"])
                  for r in balances.balances(conn, [wh], "2026-06-30"))
    assert left == [("Відеореєстратор 16-канальний", 1, 1000, 1200056), ("Кабель КПП-ВП 2х0,5", 1, 250000, 1050),
                    ("Камера відеоспостереження купольна", 1, 1000, 446000), ("Ліхтар налобний", 1, 4000, 35033)]
    cam = conn.execute("SELECT * FROM nomenclature WHERE name = 'Камера відеоспостереження купольна'").fetchone()
    assert cam["serial_tracked"] and cam["accounting_class"] == "fixed" and cam["account"] == "1014"
    assert conn.execute("SELECT inventory_no FROM items WHERE nomenclature_id = ?", (cam["id"],)).fetchone()[0] \
        == "10140000001"
    cable = conn.execute("SELECT * FROM nomenclature WHERE name = 'Кабель КПП-ВП 2х0,5'").fetchone()
    assert cable["code"] == "15150000001" and not cable["serial_tracked"] and cable["accounting_class"] == "inventory"
    # Чужого МВО (Папір А4) немає.
    assert conn.execute("SELECT COUNT(*) FROM nomenclature WHERE name = 'Папір А4'").fetchone()[0] == 0


def test_errors_change_nothing(env):
    storage, _, ids = env
    conn = open_db(storage)
    wh = locations.for_unit(conn, ids["wh"])
    noms = conn.execute("SELECT COUNT(*) FROM nomenclature").fetchone()[0]
    bad = ROWS[:3] + [["1515", KOVAL, "", "Кабель", "кг", "1", 1, 1, 1],        # у довіднику Кабель — «м»
                      ["1515", KOVAL, "", "Мило", "шт.", "2", "", 1, 1],          # без ціни
                      ["1515", KOVAL, "", "Сіль", "кг", "3", 1, -2, -2]]          # від'ємна кількість
    ob = oborotka.read(xlsx(bad))
    classes = {a.code: (a.accounting_class, a.serial) for a in ob.accounts}
    with pytest.raises(DocumentError) as exc:
        oborotka.create_drafts(conn, xlsx(bad), oborotka.Plan({KOVAL: wh}, classes, "2026-06-11"))
    errors = exc.value.errors
    assert any(e.startswith("Рядок 5:") and "«м»" in e for e in errors)
    assert any(e.startswith("Рядок 6:") and "ціна" in e for e in errors)
    assert any(e.startswith("Рядок 7:") and "кількість" in e for e in errors)
    assert conn.execute("SELECT COUNT(*) FROM nomenclature").fetchone()[0] == noms
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
    with pytest.raises(DocumentError, match="хоча б одну МВО"):
        oborotka.create_drafts(conn, xlsx(), oborotka.Plan({}, classes, "2026-06-11"))


def test_web_oborotka_two_steps(env):
    storage, client, ids = env
    data = xlsx()
    resp = client.post("/documents/import", data={"op_date": "2026-06-11", "to_place": f"u:{ids['wh']}",
                                                  "file": (BytesIO(data), "obor.xlsx")},
                       content_type="multipart/form-data")
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200 and "Оборотка з бухгалтерії: що імпортувати" in html
    assert KOVAL in html and BONDAR in html and "Рахунки → клас майна" in html
    b64 = re.search(r'name="file_b64" value="([^"]+)"', html).group(1)
    assert base64.b64decode(b64) == data
    # МВО обрано, місце — ні: помилка, вибір не губиться.
    ob = oborotka.read(xlsx())
    kov = str([m.name for m in ob.mvos].index(KOVAL))
    resp = client.post("/documents/import/oborotka", data={"file_b64": b64, "op_date": "2026-06-11",
                                                           "mvo": [kov], f"place_{kov}": ""})
    html = resp.get_data(as_text=True)
    assert "Оберіть, куди внести" in html and f'value="{kov}" id="mvo-{kov}"\n                   checked' in html
    resp = client.post("/documents/import/oborotka", data={
        "file_b64": b64, "op_date": "2026-06-11", "mvo": [kov], f"place_{kov}": f"u:{ids['wh']}",
        "cls_1014": "fixed", "serial_1014": "1", "cls_1515": "inventory", "cls_1812": "mshp"})
    assert resp.status_code == 302
    edit = client.get(resp.headers["Location"]).get_data(as_text=True)
    # Розбіжність округлення — лише в рядку іншої МВО, тож попередження немає.
    assert "Створено чернеток: 1, рядків — 3" in edit and "округлено до копійок" not in edit


def test_price_date_tail_not_a_duplicate(env):
    """«(1450,00 - 30.07.2026)» у назві з бухгалтерії — не частина назви; виправлене вручну
    найменування впізнається і за назвою без хвоста, і за кодом."""
    assert oborotka.clean_name("Кабель ATIS (бухта 100м) (1450,00 - 30.07.2026)") == "Кабель ATIS (бухта 100м)"
    assert oborotka.clean_name("Конектор RJ45 (1,00-21.12.2023}") == "Конектор RJ45"
    assert oborotka.clean_name("Гільза (пакунок - 100 шт.)") == "Гільза (пакунок - 100 шт.)"

    storage, _, ids = env
    conn = open_db(storage)
    wh = locations.for_unit(conn, ids["wh"])
    manual = add(conn, "nomenclature", name="Кабель ATIS (бухта 100м)", uom="бух.",
                 accounting_class="inventory", code="15150000016179")
    rows = [["1515", KOVAL, "ВЧ А0000", "Кабель ATIS (бухта 100м) (1450,00 - 30.07.2026)", "бух.",
             "15150000016179", 1450, 20, 29000],
            ["1515", KOVAL, "ВЧ А0000", "Кабель ATIS старе ім'я", "бух.", "15150000016179", 1450, 5, 7250]]
    data = xlsx(rows)
    res = oborotka.create_drafts(conn, data, oborotka.Plan({KOVAL: wh}, {"1515": ("inventory", False)}, "2026-09-13"))
    assert res.new_names == 0
    assert {ln["nomenclature_id"] for ln in documents.lines(conn, res.doc_ids[0])} == {manual}
    assert conn.execute("SELECT COUNT(*) FROM nomenclature WHERE code = '15150000016179'").fetchone()[0] == 1


def test_default_category_per_account(env):
    storage, _, ids = env
    conn = open_db(storage)
    wh = locations.for_unit(conn, ids["wh"])
    ob = oborotka.read(xlsx())
    classes = {a.code: (a.accounting_class, a.serial) for a in ob.accounts}
    res = oborotka.create_drafts(conn, xlsx(), oborotka.Plan({KOVAL: wh}, classes, "2026-09-13", {"1014": 2}))
    cats = {}
    for ln in documents.lines(conn, res.doc_ids[0]):
        name = conn.execute("SELECT name FROM nomenclature WHERE id = ?", (ln["nomenclature_id"],)).fetchone()[0]
        cats[name] = ln["category"]
    assert cats == {"Камера відеоспостереження купольна": 2, "Відеореєстратор 16-канальний": 2,
                    "Кабель КПП-ВП 2х0,5": 1}


def test_mvo_guessed_to_own_place(env):
    storage, client, ids = env
    conn = open_db(storage)
    # Коваль Олена (МВО складу з фікстури) — відома; Бондар Іван — ні, поки не стане МВО свого місця.
    assert client.post("/documents/import", data={"to_place": f"u:{ids['wh']}", "op_date": "2026-09-13",
                                                  "file": (BytesIO(xlsx()), "o.xlsx")},
                       content_type="multipart/form-data").status_code == 200
    from oblik.web.documents import mvo_place_guess
    assert mvo_place_guess(conn, KOVAL) == f"u:{ids['wh']}"
    assert mvo_place_guess(conn, BONDAR) == ""
    shop = add(conn, "units", name="Склад 2", kind="warehouse", is_accounting=1, mvo_person_id=ids["soldier"])
    assert mvo_place_guess(conn, BONDAR) == f"u:{shop}"
