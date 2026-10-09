"""Видане в користування рахується за тим місцем обліку, яке його видало
(п. 11, 16 розд. V Інструкції: з обліку підрозділу не знімається)."""
from __future__ import annotations

import sqlite3

from oblik import balances, db, documents, locations, reports
from tests.test_documents_web import add, doc_id_from, env, open_db  # noqa: F401 — фікстура env


def post_doc(client, doc_type, from_place, to_place, lines, outgoing, op_date="2026-10-02"):
    head = {"from_place": from_place, "to_place": to_place, "doc_date": op_date}
    doc = doc_id_from(client.post(f"/documents/new/{doc_type}", data=head))
    data = {**head, "mode": "out" if outgoing else "in", "action": "post"}
    data.update(lines)
    resp = client.post(f"/documents/{doc}", data=data)
    assert resp.status_code == 302, resp.get_data(as_text=True)
    return doc


def receive_cable(client, ids, unit_place, qty="10"):
    return post_doc(client, "receipt", f"c:{ids['ext']}", unit_place, {
        "ln_nom": [str(ids["cable"])], "ln_name": ["Кабель"], "ln_serial": [""], "ln_inv": [""],
        "ln_cat": ["1"], "ln_price": ["10"], "ln_qty": [qty], "ln_req": [""], "ln_note": [""]},
        outgoing=False, op_date="2026-10-01")


def cable_key(ids):
    return f"{ids['cable']}||1|1000"


def test_issue_counts_for_issuing_unit_even_if_person_elsewhere(env):
    storage, client, ids = env
    conn = open_db(storage)
    # Друга служба; боєць числиться в ній, але видає йому склад.
    ids["comms"] = add(conn, "units", name="Служба зв'язку", kind="service", is_accounting=1,
                       mvo_person_id=ids["mvo"])
    conn.execute("UPDATE persons SET unit_id = ? WHERE id = ?", (ids["comms"], ids["soldier"]))
    conn.commit()
    conn.close()
    receive_cable(client, ids, f"u:{ids['wh']}")
    receive_cable(client, ids, f"u:{ids['comms']}")
    issue = {"ln_key": [cable_key(ids)], "ln_qty": ["3"], "ln_req": [""], "ln_note": [""]}
    post_doc(client, "transfer", f"u:{ids['wh']}", f"p:{ids['soldier']}", issue, outgoing=True)
    post_doc(client, "transfer", f"u:{ids['comms']}", f"p:{ids['soldier']}",
             {**issue, "ln_qty": ["2"]}, outgoing=True)

    conn = open_db(storage)
    person_locs = locations.person_locations(conn, ids["soldier"])
    assert len(person_locs) == 2
    owners = {locations.owner(conn, loc): loc for loc in person_locs}
    assert set(owners) == {ids["wh"], ids["comms"]}
    # Склад: 10 − 3 на складі + 3 у бійця = 10; зв'язок так само зі своїми 2.
    wh = reports.stock(conn, f"u:{ids['wh']}", "2026-10-31")
    assert sorted((r["place"], r["qty_m"]) for r in wh.rows) == [
        ("Бондар І. (видано: Склад ТЗО)", 3000), ("Склад ТЗО", 7000)]
    comms = reports.stock(conn, f"u:{ids['comms']}", "2026-10-31")
    assert sorted((r["place"], r["qty_m"]) for r in comms.rows) == [
        ("Бондар І. (видано: Служба зв'язку)", 2000), ("Служба зв'язку", 8000)]
    # Особа: обидва місця.
    assert sum(r["qty_m"] for r in reports.stock(conn, f"p:{ids['soldier']}", "2026-10-31").rows) == 5000
    conn.close()

    # Повернення: «звідки» — саме видане складом, і повертається на склад.
    html = client.get("/documents/new/transfer").get_data(as_text=True)
    assert f'value="pl:{owners[ids["wh"]]}"' in html and "видано: Склад ТЗО" in html
    post_doc(client, "transfer", f"pl:{owners[ids['wh']]}", f"u:{ids['wh']}",
             {**issue, "ln_qty": ["3"]}, outgoing=True, op_date="2026-10-03")
    conn = open_db(storage)
    assert balances.balances(conn, [owners[ids["wh"]]], "2026-10-31") == []
    assert balances.balances(conn, [owners[ids["comms"]]], "2026-10-31")[0]["qty_m"] == 2000
    conn.close()


def test_person_to_person_keeps_issuer(env):
    storage, client, ids = env
    conn = open_db(storage)
    other = add(conn, "persons", last_name="Шевчук", first_name="Петро")
    conn.commit()
    conn.close()
    receive_cable(client, ids, f"u:{ids['wh']}")
    issue = {"ln_key": [cable_key(ids)], "ln_qty": ["4"], "ln_req": [""], "ln_note": [""]}
    post_doc(client, "transfer", f"u:{ids['wh']}", f"p:{ids['soldier']}", issue, outgoing=True)
    conn = open_db(storage)
    src = locations.person_locations(conn, ids["soldier"])[0]
    conn.close()
    post_doc(client, "transfer", f"pl:{src}", f"p:{other}", issue, outgoing=True, op_date="2026-10-03")
    conn = open_db(storage)
    dst = locations.person_locations(conn, other)[0]
    assert locations.owner(conn, dst) == ids["wh"]
    assert sum(r["qty_m"] for r in reports.stock(conn, f"u:{ids['wh']}", "2026-10-31").rows) == 10000
    conn.close()


def test_single_accounting_unit_is_default_for_person_outside_tree(env):
    """«Частина в частині»: боєць числиться в А1111 (не місце обліку), служба ТЗО — поруч."""
    storage, client, ids = env
    conn = open_db(storage)
    a0 = add(conn, "units", name="Військова частина А0000", kind="military_unit")
    a1 = add(conn, "units", name="Військова частина А1111", kind="military_unit", parent_id=a0)
    conn.execute("UPDATE units SET parent_id = ? WHERE id = ?", (a1, ids["wh"]))
    conn.execute("UPDATE persons SET unit_id = ? WHERE id = ?", (a1, ids["soldier"]))
    conn.commit()
    assert locations.default_owner(conn, ids["soldier"]) == ids["wh"]
    conn.close()
    # Початкові залишки прямо на бійця — рахуються за єдиним місцем обліку.
    post_doc(client, "opening", "", f"p:{ids['soldier']}", {
        "ln_nom": [str(ids["cable"])], "ln_name": ["Кабель"], "ln_serial": [""], "ln_inv": [""],
        "ln_cat": ["1"], "ln_price": ["10"], "ln_qty": ["2"], "ln_req": [""], "ln_note": [""]},
        outgoing=False)
    conn = open_db(storage)
    wh = reports.stock(conn, f"u:{ids['wh']}", "2026-10-31")
    assert [(r["place"], r["qty_m"]) for r in wh.rows] == [("Бондар І.", 2000)]
    conn.close()


def test_migration_004_sets_owner_and_keeps_triggers(tmp_path):
    path = tmp_path / "old.db"
    conn = db.connect(path)
    for num, f in db.migrations():
        if num <= 3:
            conn.executescript(f"BEGIN;\n{f.read_text(encoding='utf-8')}\nPRAGMA user_version = {num};\nCOMMIT;")
    conn.execute("INSERT INTO units(id, name, kind, is_accounting) VALUES (1, 'Рота', 'subunit', 1)")
    conn.execute("INSERT INTO units(id, name, kind, parent_id) VALUES (2, 'Взвод', 'subunit', 1)")
    conn.execute("INSERT INTO persons(id, last_name, first_name, unit_id) VALUES (1, 'Коваль', 'Олена', 2)")
    conn.execute("INSERT INTO locations(kind, unit_id) VALUES ('unit', 1)")
    conn.execute("INSERT INTO locations(kind, person_id) VALUES ('person', 1)")
    conn.commit()
    assert db.migrate(conn)[0] == 4   # далі — новіші міграції
    row = conn.execute("SELECT unit_id FROM locations WHERE kind = 'person'").fetchone()
    assert row["unit_id"] == 1
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    # Тригери на місцях діють і після перебудови таблиці.
    conn.execute("INSERT INTO units(id, name, kind) VALUES (3, 'Не облік', 'subunit')")
    for sql in ("INSERT INTO locations(kind, unit_id) VALUES ('unit', 3)",
                "INSERT INTO locations(kind, person_id, unit_id) VALUES ('person', 1, 3)",
                "INSERT INTO locations(kind, unit_id) VALUES ('unit', 1)"):
        try:
            conn.execute(sql)
        except sqlite3.IntegrityError:
            continue
        raise AssertionError(f"мало бути заборонено: {sql}")
    conn.close()


def test_storno_of_issue(env):
    storage, client, ids = env
    receive_cable(client, ids, f"u:{ids['wh']}")
    issue = {"ln_key": [cable_key(ids)], "ln_qty": ["4"], "ln_req": [""], "ln_note": [""]}
    doc = post_doc(client, "transfer", f"u:{ids['wh']}", f"p:{ids['soldier']}", issue, outgoing=True)
    conn = open_db(storage)
    documents.reverse(conn, doc, on_date="2026-10-05")
    conn.commit()
    wh = reports.stock(conn, f"u:{ids['wh']}", "2026-10-31")
    assert [(r["place"], r["qty_m"]) for r in wh.rows] == [("Склад ТЗО", 10000)]
    conn.close()
