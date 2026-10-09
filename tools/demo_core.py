"""Демонстрація ядра обліку на тимчасовій базі (нічого не зберігає).

Запуск:  .venv\\Scripts\\python tools\\demo_core.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from oblik import balances, db, documents, locations, refs  # noqa: E402
from oblik.textutil import format_money, format_qty  # noqa: E402


def add(conn, ref_kind, **values):
    ref = refs.REFS[ref_kind]
    full = {f.name: f.default for f in ref.fields}
    full.update(values)
    return refs.save(conn, ref, None, full)


def show(conn, title, scope, on_date=None):
    print(f"\n  Залишки: {title}" + (f" на {documents.fmt_date(on_date)}" if on_date else ""))
    rows = balances.balances(conn, scope, on_date)
    if not rows:
        print("    (порожньо)")
    labels = refs.Labels(conn)
    for r in rows:
        name = labels.get("items", r["item_id"]) if r["item_id"] else labels.get("nomenclature", r["nomenclature_id"])
        cat = f", кат. {documents.CATEGORY_TITLES[r['category']]}" if r["category"] else ""
        where = locations.title(conn, r["location_id"])
        print(f"    {where:<22} {name:<38} {format_qty(r['qty_m']):>6} × {format_money(r['price_kop']):>10}{cat}")


def step(text):
    print(f"\n▶ {text}")


def main():
    path = Path(tempfile.mkdtemp()) / "demo.db"
    conn = db.connect(path)
    db.migrate(conn)
    db.init_meta(conn, "demo")

    mvo = add(conn, "persons", last_name="Коваль", first_name="Олена", rank="сержант", roles=["mvo"])
    wh_u = add(conn, "units", name="Склад ТЗО", kind="warehouse", is_accounting=1, mvo_person_id=mvo)
    coy_u = add(conn, "units", name="1 рота", kind="subunit", is_accounting=1, mvo_person_id=mvo)
    soldier = add(conn, "persons", last_name="Бондар", first_name="Іван", rank="солдат", unit_id=coy_u)
    higher = add(conn, "counterparties", name="Вища частина", kind="higher_unit")
    cable = add(conn, "nomenclature", name="Кабель КПП-ВП 4×2×0,51", uom="м")
    cam_n = add(conn, "nomenclature", name="Відеокамера IP", uom="шт.", accounting_class="fixed",
                serial_tracked=1)
    cam = add(conn, "items", nomenclature_id=cam_n, serial_no="SN-001")

    wh, coy = locations.for_unit(conn, wh_u), locations.for_unit(conn, coy_u)
    person = locations.for_person(conn, soldier)
    ext = locations.for_counterparty(conn, higher)

    def doc(doc_type, src, dst, lines, d):
        doc_id = documents.create_draft(conn, doc_type, src, dst, doc_date=d, op_date=d)
        documents.set_lines(conn, doc_id, lines)
        documents.post(conn, doc_id)
        print(f"  проведено: {documents.label(conn, doc_id)}")
        return doc_id

    step("Надходження від вищої частини на склад: 300 м кабелю (по 12,44) і камера SN-001")
    doc("receipt", ext, wh, [
        {"nomenclature_id": cable, "qty_m": 300_000, "price_kop": 1244, "category": 1},
        {"nomenclature_id": cam_n, "item_id": cam, "qty_m": 1000, "price_kop": 446000, "category": 1},
    ], "2026-10-01")
    show(conn, "склад", [wh])

    step("Накладна: склад → 1 рота, 100 м кабелю і камера")
    wrong = doc("transfer", wh, coy, [
        {"nomenclature_id": cable, "qty_m": 100_000, "price_kop": 1244, "category": 1},
        {"nomenclature_id": cam_n, "item_id": cam, "qty_m": 1000, "price_kop": 446000, "category": 1},
    ], "2026-10-02")

    step("Спроба видати зі складу 250 м (там лишилось 200)")
    d = documents.create_draft(conn, "transfer", wh, coy, doc_date="2026-10-03")
    documents.set_lines(conn, d, [{"nomenclature_id": cable, "qty_m": 250_000, "price_kop": 1244, "category": 1}])
    try:
        documents.post(conn, d)
    except documents.DocumentError as exc:
        print("  відмова:", *exc.errors)
    documents.cancel(conn, d)
    print(f"  чернетку анульовано, номер {documents.get(conn, d)['reg_no']} лишається в реєстрі")

    step("Спроба змінити проведену накладну")
    try:
        documents.update_draft(conn, wrong, basis="виправлення")
    except documents.DocumentError as exc:
        print("  відмова:", *exc.errors)

    step("Помилка в накладній — сторнуємо її і оформлюємо правильну (80 м)")
    s = documents.reverse(conn, wrong, on_date="2026-10-03")
    print(f"  проведено: {documents.label(conn, s)}")
    doc("transfer", wh, coy, [
        {"nomenclature_id": cable, "qty_m": 80_000, "price_kop": 1244, "category": 1},
        {"nomenclature_id": cam_n, "item_id": cam, "qty_m": 1000, "price_kop": 446000, "category": 1},
    ], "2026-10-03")

    step("Рота видає камеру в користування солдату Бондарю")
    doc("transfer", coy, person, [
        {"nomenclature_id": cam_n, "item_id": cam, "qty_m": 1000, "price_kop": 446000, "category": 1},
    ], "2026-10-04")

    show(conn, "склад", [wh])
    show(conn, "1 рота разом з особовим складом", locations.scope_for_unit(conn, coy_u))
    show(conn, "солдат Бондар", [person])
    show(conn, "1 рота", [coy], "2026-10-02")

    step("Картка руху камери SN-001")
    for m in balances.item_card(conn, cam):
        mark = " (сторно)" if m["doc_type"] == "storno" else ""
        print(f"    {documents.fmt_date(m['op_date'])}  {locations.title(conn, m['from_location_id']):<16} → "
              f"{locations.title(conn, m['to_location_id']):<20} {documents.DOC_TYPES[m['doc_type']].title} "
              f"№ {m['doc_no']}{mark}")

    step("Журнал змін (останні 5 записів)")
    for r in conn.execute("SELECT ts, action, summary FROM audit_log ORDER BY id DESC LIMIT 5"):
        print(f"    {r['ts']}  {r['action']:<8} {r['summary']}")
    conn.close()


if __name__ == "__main__":
    main()
