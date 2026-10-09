"""Довідники «на льоту»: знайти або створити найменування й поштучну одиницю за тим, що
вписав користувач (у рядку документа чи в рядку файлу Excel). Помилки — ValueError з текстом
для користувача.

© 2026 Yevhenii Hosting by LLC Hosting in Ukraine
"""
from __future__ import annotations

from . import refs, settings


def find_or_create_item(conn, nom_id: int, serial: str | None, inv: str | None) -> int:
    """Поштучна одиниця за інвентарним або заводським номером; немає — створити (із журналом)."""
    if inv:
        row = conn.execute("SELECT id, nomenclature_id FROM items WHERE inventory_no = ?", (inv,)).fetchone()
        if row:
            if row["nomenclature_id"] != nom_id:
                raise ValueError(f"інвентарний № {inv} належить іншому найменуванню")
            return row["id"]
    if serial:
        row = conn.execute("SELECT id FROM items WHERE nomenclature_id = ? AND serial_no = ?",
                           (nom_id, serial)).fetchone()
        if row:
            return row["id"]
    ref = refs.REFS["items"]
    values = {f.name: f.default for f in ref.fields}
    values.update(nomenclature_id=nom_id, serial_no=serial, inventory_no=inv, components=[])
    try:
        return refs.save(conn, ref, None, values)
    except refs.ValidationError as exc:
        raise ValueError("; ".join(exc.errors.values())) from None


def find_nomenclature(conn, name: str) -> int | None:
    """Найменування з довідника за вписаною назвою (або «назва [код]»), без урахування регістру."""
    wanted = " ".join(name.split()).lower()
    for nom_id, label in refs.Labels(conn).map("nomenclature").items():
        if label.lower() == wanted:
            return nom_id
    row = conn.execute("SELECT id FROM nomenclature WHERE ulower(name) = ? ORDER BY is_active DESC, id",
                       (wanted,)).fetchone()
    return row["id"] if row else None


def create_nomenclature(conn, name: str, uom: str, accounting_class: str,
                        serial_tracked: bool, code: str | None = None) -> int:
    """Нове найменування, вписане в рядку документа (із записом у журнал змін)."""
    name = " ".join(name.split())
    ref = refs.REFS["nomenclature"]
    values = {f.name: f.default for f in ref.fields}
    values.update(name=name, uom=uom.strip() or "шт.",
                  accounting_class=accounting_class if accounting_class in dict(refs.ACCOUNTING_CLASSES)
                  else "inventory",
                  serial_tracked=1 if serial_tracked else 0, code=code or values.get("code"),
                  service_id=settings.load(conn)["default_service_id"])
    try:
        return refs.save(conn, ref, None, values)
    except refs.ValidationError as exc:
        raise ValueError(f"нове найменування «{name}»: " + "; ".join(exc.errors.values())) from None
