"""Дерево структури, звіт по підрозділу з кількома МВО, навігація між документами."""
from __future__ import annotations

from oblik import reports
from tests.test_documents_web import add, doc_id_from, env, open_db  # noqa: F401 — фікстура env
from tests.test_issue_owner import receive_cable


def test_units_tree(env):
    storage, client, ids = env
    conn = open_db(storage)
    a0 = add(conn, "units", name="Військова частина А0000", kind="military_unit")
    conn.execute("UPDATE units SET parent_id = ? WHERE id = ?", (a0, ids["wh"]))
    conn.commit()
    conn.close()
    html = client.get("/refs/units").get_data(as_text=True)
    assert 'class="tree"' in html
    assert html.index("Військова частина А0000") < html.index("Склад ТЗО")
    assert "МВО: Коваль О." in html and "+ підлеглий" in html
    # «+ підлеглий» підставляє батька.
    form = client.get(f"/refs/units/new?parent_id={a0}").get_data(as_text=True)
    assert f'<option value="{a0}" selected' in form
    # Пошук — звичайним списком.
    assert 'class="tree"' not in client.get("/refs/units?q=склад").get_data(as_text=True)


def test_department_with_two_mvo(env):
    """Відділення ТЗО (не місце обліку) з двома місцями обліку — по одному на МВО."""
    storage, client, ids = env
    conn = open_db(storage)
    dept = add(conn, "units", name="Відділення ТЗО", kind="service")
    conn.execute("UPDATE units SET parent_id = ? WHERE id = ?", (dept, ids["wh"]))
    mvo2 = add(conn, "persons", last_name="Мельник", first_name="Андрій", roles=["mvo"])
    second = add(conn, "units", name="ТЗО — МВО Мельник А.", kind="service", parent_id=dept,
                 is_accounting=1, mvo_person_id=mvo2)
    conn.commit()
    conn.close()
    receive_cable(client, ids, f"u:{ids['wh']}", "10")
    receive_cable(client, ids, f"u:{second}", "5")
    conn = open_db(storage)
    choices = dict(sum((opts for _, opts in reports.scope_choices(conn)), []))
    assert choices[f"t:{dept}"] == "Відділення ТЗО — разом 2 місць обліку"
    total = reports.stock(conn, f"t:{dept}", "2026-10-31")
    assert sum(r["qty_m"] for r in total.rows) == 15000 and total.many_places
    assert sum(r["qty_m"] for r in reports.stock(conn, f"m:{mvo2}", "2026-10-31").rows) == 5000
    conn.close()
    html = client.get(f"/reports/stock?scope=t:{dept}&on_date=2026-10-31").get_data(as_text=True)
    assert "усі місця обліку в ньому" in html and "ТЗО — МВО Мельник А." in html


def test_document_navigation_and_search(env):
    storage, client, ids = env
    first = receive_cable(client, ids, f"u:{ids['wh']}")
    second = receive_cable(client, ids, f"u:{ids['wh']}")
    html = client.get(f"/documents/{first}").get_data(as_text=True)
    assert f"/documents/{second}" in html and "№ 2 →" in html
    assert "/reports/stock?scope=u:" in html                        # «Куди» — на залишки
    assert f"/refs/counterparties/{ids['ext']}" in html             # «Звідки» — контрагент
    assert f"/refs/nomenclature/{ids['cable']}" in html             # рядок — номенклатура
    html = client.get(f"/documents/{second}").get_data(as_text=True)
    assert "← № 1" in html and "№ 3 →" not in html
    # Пошук за підставою.
    draft = doc_id_from(client.post("/documents/new/receipt", data={
        "from_place": f"c:{ids['ext']}", "to_place": f"u:{ids['wh']}", "doc_date": "2026-10-01",
        "basis": "Наряд № 47 від 01.10.2026"}))
    found = client.get("/documents?year=2026&q=наряд 47").get_data(as_text=True)
    assert f"/documents/{draft}'" in found and f"/documents/{first}'" not in found


def test_draft_page_explains_buttons(env):
    _, client, ids = env
    doc = doc_id_from(client.post("/documents/new/receipt", data={
        "from_place": f"c:{ids['ext']}", "to_place": f"u:{ids['wh']}", "doc_date": "2026-10-01"}))
    html = client.get(f"/documents/{doc}").get_data(as_text=True)
    assert "Чи можна провести — нічого не змінює" in html and "підстава не вказана" in html
    assert "Рядки з майном — додайте нижче" in html
