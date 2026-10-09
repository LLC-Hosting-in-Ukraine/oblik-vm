"""Сторінки документів: журнал, «Що сталося?», чернетка, проведення, сторно."""
from __future__ import annotations

import json
from datetime import date

from io import BytesIO

from flask import Blueprint, abort, flash, redirect, render_template, request, send_file, url_for
from werkzeug.datastructures import MultiDict

from .. import (balances, condition, documents, docx_out, help, importer, locations, orders, pdf_out, refs,
                settings)
from ..catalog import create_nomenclature, find_nomenclature, find_or_create_item
from ..documents import CATEGORY_TITLES, DOC_TYPES, STATUS_TITLES, DocumentError
from ..textutil import format_money, format_qty, parse_money, parse_qty
from . import get_db

bp = Blueprint("documents", __name__)

# Які місця можна обирати «звідки» / «куди» для кожного типу документа.
PLACE_GROUPS = {
    "opening": (None, ("u", "p")),
    "receipt": (("c",), ("u", "p")),
    "transfer": (("u", "p"), ("u", "p")),
    "dispatch": (("u", "p"), ("c",)),
    "writeoff": (("u", "p"), None),
    "order": (("u", "c"), ("u", "c")),   # вантажовідправник / вантажоодержувач
    "condition": (("u",), None),          # місце обліку; списане → «Списано», отримане — туди ж
}
FIXED_PLACE = {"opening": ("from", locations.OPENING), "writeoff": ("to", locations.WRITTEN_OFF),
               "condition": ("to", locations.WRITTEN_OFF)}


def _extra_values(form) -> dict:
    """Реквізити наряду й акта якісного стану так, як їх ввели у формі."""
    result = {k: form.get(f"x_{k}", "") for k in {**documents.ORDER_EXTRA, **documents.CONDITION_EXTRA}}
    for k in documents.CONDITION_PERSONS:
        result[k] = int(form[f"x_{k}"]) if form.get(f"x_{k}", "").isdigit() else None
    result["commission_members"] = [int(v) for v in form.getlist("x_commission_members") if v.isdigit()]
    return result


def _last_commission(conn) -> dict:
    """Комісія з останнього акта якісного стану — щоб не обирати щоразу заново."""
    row = conn.execute("SELECT * FROM documents WHERE doc_type = 'condition' AND status <> 'cancelled' "
                       "ORDER BY id DESC LIMIT 1").fetchone()
    if row is None:
        return {}
    ex = documents.condition_extra(row)
    return {k: ex[k] for k in ("commission_head", "commission_members", "senior_person")}
GROUP_TITLES = {"u": "Місця обліку (склади, підрозділи)", "p": "Особи (видане в користування)",
                "c": "Контрагенти (за межами частини)"}


# --- Місця: код у формі ↔ location_id -----------------------------------------

def place_options(conn, groups: tuple[str, ...] | None,
                  source: bool = False) -> list[tuple[str, list[tuple[str, str]]]]:
    """Варіанти «звідки» / «куди». Особа як «куди» — просто особа (за чиїм обліком рахувати
    видане, визначає «звідки»); як «звідки» — її наявні місця «видано такою-то службою»."""
    if not groups:
        return []
    labels = refs.Labels(conn)
    result = []
    for g in groups:
        if g == "u":
            rows = conn.execute("SELECT id FROM units WHERE is_accounting = 1 AND is_active = 1 "
                                "ORDER BY name COLLATE UK").fetchall()
            opts = [(f"u:{r['id']}", labels.get("units", r["id"])) for r in rows]
        elif g == "p" and source:
            locs = conn.execute("SELECT l.id FROM locations l JOIN persons p ON p.id = l.person_id "
                                "WHERE l.kind = 'person' ORDER BY p.last_name COLLATE UK, "
                                "p.first_name COLLATE UK, l.id").fetchall()
            opts = [(f"pl:{r['id']}", locations.title(conn, r["id"])) for r in locs]
        elif g == "p":
            opts = [(f"p:{pid}", title) for pid, title in refs.options(conn, "persons")]
        else:
            opts = [(f"c:{cid}", title) for cid, title in refs.options(conn, "counterparties")]
        result.append((GROUP_TITLES[g], opts))
    return result


def place_to_location(conn, code: str, source_location_id: int | None = None) -> int:
    """Код з форми → місце. Для особи як «куди» місце обліку, за яким рахується видане,
    береться з «звідки»: видав склад — рахується за складом (п. 11, 16 розд. V)."""
    try:
        kind, raw_id = code.split(":")
        ref_id = int(raw_id)
    except ValueError:
        raise locations.LocationError("Оберіть місце зі списку") from None
    if kind == "u":
        return locations.for_unit(conn, ref_id)
    if kind == "pl":
        loc = conn.execute("SELECT id FROM locations WHERE id = ? AND kind = 'person'", (ref_id,)).fetchone()
        if loc is None:
            raise locations.LocationError("Оберіть особу зі списку")
        return loc["id"]
    if kind == "p":
        owner = locations.owner(conn, source_location_id) if source_location_id else None
        if owner is None:
            owner = locations.default_owner(conn, ref_id)
        if owner is None:
            raise locations.LocationError(
                "не зрозуміло, за яким місцем обліку рахувати майно цієї особи. Видайте його з "
                "місця обліку (склад, служба) або вкажіть підрозділ особи в довіднику «Особи»")
        return locations.for_person(conn, ref_id, owner)
    if kind == "c":
        return locations.for_counterparty(conn, ref_id)
    raise locations.LocationError("Оберіть місце зі списку")


def location_to_place(conn, location_id: int, side: str = "to") -> str:
    loc = locations.get(conn, location_id)
    person = f"pl:{loc['id']}" if side == "from" else f"p:{loc['person_id']}"
    return {"unit": f"u:{loc['unit_id']}", "person": person,
            "counterparty": f"c:{loc['counterparty_id']}"}.get(loc["kind"], "")


def is_outgoing(conn, doc) -> bool:
    """Майно забирається з внутрішнього місця — рядки обирають з наявних залишків.
    Наряд — розпорядження: рядки вписують вільно (майно ще не рухається)."""
    if doc["doc_type"] in documents.NO_MOVEMENT_TYPES:
        return False
    return locations.kind(conn, doc["from_location_id"]) in locations.INTERNAL_KINDS


# --- Журнал документів --------------------------------------------------------

EXEC_FILTERS = {"open": "Наряди: не виконані", "overdue": "Наряди: строк дії минув"}


@bp.get("/documents")
def index():
    conn = get_db()
    year = request.args.get("year", type=int) or date.today().year
    status = request.args.get("status") or ""
    doc_type = request.args.get("type") or ""
    q = request.args.get("q", "").strip()
    # Виконання нарядів: невиконані / прострочені — за всі роки (наряд міг бути торішнім).
    exec_filter = request.args.get("exec") or ""
    if exec_filter not in EXEC_FILTERS:
        exec_filter = ""
    where, params = (["doc_type = 'order'", "status = 'posted'"], []) if exec_filter else (["reg_year = ?"], [year])
    for word in q.lower().split():
        where.append("(ulower(basis) LIKE ? OR ulower(doc_no) LIKE ? OR ulower(note) LIKE ? "
                     "OR CAST(reg_no AS TEXT) = ?)")
        params += [f"%{word}%"] * 3 + [word]
    if status:
        where.append("status = ?")
        params.append(status)
    if doc_type:
        where.append("doc_type = ?")
        params.append(doc_type)
    rows = conn.execute(
        "SELECT d.*, (SELECT COUNT(*) FROM document_lines l WHERE l.document_id = d.id) AS n_lines, "
        "(SELECT COALESCE(SUM((l.qty_m * l.price_kop + 500) / 1000), 0) FROM document_lines l "
        " WHERE l.document_id = d.id) AS total_kop "
        f"FROM documents d WHERE {' AND '.join(where)} ORDER BY reg_no DESC", params).fetchall()
    years = [r[0] for r in conn.execute(
        "SELECT DISTINCT reg_year FROM documents ORDER BY reg_year DESC")] or [year]
    if year not in years:
        years.insert(0, year)
    today = date.today()
    states = {r["id"]: orders.execution(conn, r["id"], today=today)
              for r in rows if r["doc_type"] == "order" and r["status"] == "posted"}
    if exec_filter == "open":
        rows = [r for r in rows if states[r["id"]].state != "done"]
    elif exec_filter == "overdue":
        rows = [r for r in rows if states[r["id"]].expired]
    table = [{
        **dict(r),
        "execution": states.get(r["id"]),
        "type_title": DOC_TYPES[r["doc_type"]].title,
        "status_title": STATUS_TITLES[r["status"]],
        "from_title": locations.title(conn, r["from_location_id"]),
        "to_title": locations.title(conn, r["to_location_id"]),
        "date": documents.fmt_date(r["doc_date"]),
        "total": format_money(r["total_kop"]),
    } for r in rows]
    return render_template("documents_list.html", table=table, year=year, years=years,
                           status=status, doc_type=doc_type, statuses=STATUS_TITLES, q=q,
                           exec_filter=exec_filter, exec_filters=EXEC_FILTERS,
                           types={k: t.title for k, t in DOC_TYPES.items()})


# --- Новий документ -----------------------------------------------------------

@bp.get("/documents/new")
def new():
    has_opening = get_db().execute("SELECT 1 FROM documents WHERE doc_type = 'opening' "
                                   "AND status = 'posted' LIMIT 1").fetchone() is not None
    return render_template("documents_new.html", situations=help.SITUATIONS, has_opening=has_opening)


@bp.get("/documents/import/template.xlsx")
def import_template():
    return send_file(BytesIO(importer.template_xlsx()), as_attachment=True,
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                     download_name="Шаблон початкових залишків.xlsx")


@bp.route("/documents/import", methods=["GET", "POST"])
def import_opening():
    """Початкові залишки з Excel (шаблон програми) → чернетка «Введення початкових залишків»."""
    conn = get_db()
    form = request.form
    errors: list[str] = []
    if request.method == "POST":
        upload = request.files.get("file")
        data = upload.read() if upload else b""
        try:
            to_loc = place_to_location(conn, form.get("to_place", ""))
        except locations.LocationError as exc:
            errors.append(f"Куди: {exc}")
        if not data:
            errors.append("Оберіть заповнений файл Excel")
        if not errors:
            try:
                doc_id, res = importer.create_opening(conn, data, to_loc,
                                                      form.get("op_date") or date.today().isoformat())
            except DocumentError as exc:
                errors = exc.errors
            else:
                flash(f"Створено чернетку з файлу: рядків — {len(res.lines)}"
                      + (f", нових найменувань — {len(res.new_names)}" if res.new_names else "")
                      + ". Перевірте її і натисніть «Провести».", "ok")
                return redirect(url_for("documents.edit", doc_id=doc_id))
    values = {"to_place": form.get("to_place", ""), "op_date": form.get("op_date") or date.today().isoformat()}
    return render_template("documents_import.html", errors=errors, values=values,
                           place_options=place_options(conn, ("u", "p")))


@bp.route("/documents/new/<doc_type>", methods=["GET", "POST"])
def create(doc_type):
    if doc_type not in PLACE_GROUPS:
        abort(404)
    conn = get_db()
    form = request.form if request.method == "POST" else {}
    errors: list[str] = []
    if request.method == "POST":
        try:
            doc_id = _save_header(conn, None, doc_type, form)
        except (DocumentError, locations.LocationError) as exc:
            errors = getattr(exc, "errors", None) or [str(exc)]
        else:
            return redirect(url_for("documents.edit", doc_id=doc_id))
    defaults = settings.load(conn)
    values = {
        "doc_date": form.get("doc_date") or date.today().isoformat(),
        "op_date": form.get("op_date") or "",
        "from_place": form.get("from_place", ""), "to_place": form.get("to_place", ""),
        "basis": form.get("basis", ""), "valid_until": form.get("valid_until", ""),
        "service_id": int(form["service_id"]) if form.get("service_id") else defaults["default_service_id"],
        "recipient_person_id": int(form["recipient_person_id"]) if form.get("recipient_person_id") else None,
        "note": form.get("note", ""),
        "order_id": int(form["order_id"]) if form.get("order_id", "").isdigit() else None,
        "extra": _extra_values(form) if form else {**_extra_values(MultiDict()), **_last_commission(conn)},
        "copies": [c for c in form.getlist("copy") if c.strip()] if form else [],
        "copies_auto": [], "copy_options": copy_options(conn),
    }
    return render_template("documents_header.html", doc=None, doc_type=doc_type,
                           situation=help.SITUATION_BY_CODE[doc_type], values=values,
                           errors=errors, **_header_choices(conn, doc_type, values))


def _header_choices(conn, doc_type: str, values: dict) -> dict:
    src_groups, dst_groups = PLACE_GROUPS[doc_type]
    fixed = FIXED_PLACE.get(doc_type)
    fixed_title = locations.title(conn, locations.system(conn, fixed[1])) if fixed else None
    return {
        "from_options": place_options(conn, src_groups, source=True),
        "to_options": place_options(conn, dst_groups),
        "fixed": fixed[0] if fixed else None,
        "fixed_title": fixed_title,
        "service_options": refs.options(conn, "services", current=values.get("service_id")),
        "printed": doc_type in PRINTED_TYPES,
        "person_options": refs.options(conn, "persons", current=values.get("recipient_person_id")),
        "order_options": (orders.order_options(conn, values.get("order_id"))
                          if doc_type in documents.LINKABLE_TYPES else None),
    }


PRINTED_TYPES = set(docx_out.DOC_FORMS)   # документи з паперовими формами — у них є примірники


def _form_extra(conn, doc_id: int | None, doc_type: str, form) -> dict | None:
    """Додаткові реквізити з форми: наряду (транспорт, варта…) і список примірників.
    Список, який не змінювали (збігається з показаним за замовчуванням), не зберігаємо —
    тоді він сам підлаштовується під «звідки / куди»."""
    data = documents.extra(documents.get(conn, doc_id)) if doc_id else {}
    if doc_type == "order":
        data.update({k: form.get(f"x_{k}", "").strip() for k in documents.ORDER_EXTRA})
    if doc_type == "condition":
        data.update({k: form.get(f"x_{k}", "").strip() for k in documents.CONDITION_EXTRA})
        ex = _extra_values(form)
        for k in [*documents.CONDITION_PERSONS, "commission_members"]:
            data[k] = ex[k]
    if form.get("copies_block"):
        submitted = [c.strip() for c in form.getlist("copy") if c.strip()]
        try:
            shown_default = json.loads(form.get("copies_auto") or "[]")
        except ValueError:
            shown_default = []
        if submitted and submitted != shown_default:
            data["copies"] = submitted
        else:
            data.pop("copies", None)
    return data or None


def copy_options(conn, doc=None) -> list[str]:
    """Підказки «для кого примірник»."""
    s = settings.load(conn)
    opts = []
    if doc is not None:
        opts += documents.default_copies(conn, doc)
        opts += [locations.title(conn, doc["from_location_id"], with_owner=False),
                 locations.title(conn, doc["to_location_id"], with_owner=False)]
    opts += [s.get("subunit_name"), s.get("unit_name"), "фінансово-економічного органу", "у справу"]
    opts += [title for _, title in refs.options(conn, "counterparties")]
    result = []
    for o in opts:
        if o and o not in result:
            result.append(o)
    return result


def _save_header(conn, doc_id: int | None, doc_type: str, form) -> int:
    fixed = FIXED_PLACE.get(doc_type)
    errors = []
    from_loc = to_loc = None
    try:
        from_loc = (locations.system(conn, fixed[1]) if fixed and fixed[0] == "from"
                    else place_to_location(conn, form.get("from_place", "")))
    except locations.LocationError as exc:
        errors.append(f"Звідки: {exc}")
    try:
        to_loc = (locations.system(conn, fixed[1]) if fixed and fixed[0] == "to"
                  else place_to_location(conn, form.get("to_place", ""), from_loc))
    except locations.LocationError as exc:
        errors.append(f"Куди: {exc}")
    if from_loc is not None and from_loc == to_loc:
        errors.append("«Звідки» і «Куди» не можуть збігатися")
    if errors:
        raise DocumentError(errors)

    header = {
        "basis": form.get("basis", "").strip() or None,
        "valid_until": form.get("valid_until") or None,
        "extra_json": _form_extra(conn, doc_id, doc_type, form),
        "service_id": int(form["service_id"]) if form.get("service_id") else None,
        "recipient_person_id": int(form["recipient_person_id"]) if form.get("recipient_person_id") else None,
        "note": form.get("note", "").strip() or None,
    }
    if doc_type in documents.LINKABLE_TYPES:
        header["order_id"] = int(form["order_id"]) if form.get("order_id", "").isdigit() else None
        if header["order_id"] and not header["basis"]:
            order = documents.get(conn, header["order_id"])
            header["basis"] = f"Наряд № {order['doc_no']} від {documents.fmt_date(order['doc_date'])}"
    doc_date = form.get("doc_date") or date.today().isoformat()
    op_date = form.get("op_date") or doc_date
    if doc_id is None:
        return documents.create_draft(conn, doc_type, from_loc, to_loc, doc_date=doc_date,
                                      op_date=op_date, **header)
    extra = {"doc_date": doc_date, "op_date": op_date,
             "from_location_id": from_loc, "to_location_id": to_loc}
    if form.get("doc_no", "").strip():
        extra["doc_no"] = form["doc_no"].strip()
    if form.get("reg_no", "").strip():
        if not form["reg_no"].strip().isdigit():
            raise DocumentError("Реєстраційний номер має бути цілим числом")
        extra["reg_no"] = int(form["reg_no"])
    documents.update_draft(conn, doc_id, **header, **extra)
    return doc_id


# --- Рядки --------------------------------------------------------------------

class Prefixed:
    """Друга таблиця рядків (акт якісного стану, «Оприбуткувати»): поля in_* читаються як ln_*."""

    def __init__(self, form, prefix: str):
        self.form, self.prefix = form, prefix

    def getlist(self, name: str) -> list[str]:
        return self.form.getlist(self.prefix + name[3:] if name.startswith("ln_") else name)


def _stock_key(nom_id, item_id, category, price_kop) -> str:
    return f"{nom_id}|{item_id or ''}|{category or ''}|{price_kop}"


def _parse_key(text: str) -> tuple[int, int | None, int | None, int]:
    nom, item, cat, price = text.split("|")
    return int(nom), (int(item) if item else None), (int(cat) if cat else None), int(price)


def stock_options(conn, doc, current_lines) -> list[tuple[str, str]]:
    """Наявні залишки місця «звідки» на дату операції (+ ключі вже вибраних рядків)."""
    labels = refs.Labels(conn)
    noms = {r["id"]: r for r in conn.execute("SELECT id, uom FROM nomenclature")}
    rows = balances.balances(conn, [doc["from_location_id"]], doc["op_date"])
    seen, result = set(), []

    def label(nom_id, item_id, cat, price, qty):
        name = labels.get("items", item_id) if item_id else labels.get("nomenclature", nom_id)
        cat_s = f", кат. {CATEGORY_TITLES[cat]}" if cat else ""
        have = f" — є {format_qty(qty)} {noms[nom_id]['uom']}" if qty is not None else " — немає в наявності"
        return f"{name}{cat_s}, {format_money(price)} грн{have}"

    for r in sorted(rows, key=lambda r: labels.get("nomenclature", r["nomenclature_id"]).lower()):
        if r["qty_m"] <= 0:
            continue
        key = _stock_key(r["nomenclature_id"], r["item_id"], r["category"], r["price_kop"])
        seen.add(key)
        result.append((key, label(r["nomenclature_id"], r["item_id"], r["category"],
                                  r["price_kop"], r["qty_m"])))
    for ln in current_lines:
        key = _stock_key(ln["nomenclature_id"], ln["item_id"], ln["category"], ln["price_kop"])
        if key not in seen:
            seen.add(key)
            result.append((key, label(ln["nomenclature_id"], ln["item_id"], ln["category"],
                                      ln["price_kop"], None)))
    return result


def parse_lines(conn, form, outgoing: bool, order: bool = False) -> tuple[list[dict], list[str]]:
    """Рядки з форми → рядки документа. Порожні рядки пропускаються.
    order — наряд: без поштучних одиниць (номери — в акті за нарядом), кількість довільна."""
    get = lambda name: form.getlist(name)  # noqa: E731
    qtys, reqs, notes = get("ln_qty"), get("ln_req"), get("ln_note")
    result, errors = [], []
    count = len(get("ln_key") if outgoing else get("ln_nom"))
    for i in range(count):
        n = i + 1
        pick = lambda arr: (arr[i].strip() if i < len(arr) else "")  # noqa: E731
        qty_raw, req_raw, note = pick(qtys), pick(reqs), pick(get("ln_note"))
        try:
            if outgoing:
                key = pick(get("ln_key"))
                if not key and not qty_raw:
                    continue
                if not key:
                    raise ValueError("оберіть майно з залишку")
                nom_id, item_id, cat, price = _parse_key(key)
                qty = 1000 if item_id else parse_qty(qty_raw)
            else:
                nom_raw, name = pick(get("ln_nom")), pick(get("ln_name"))
                serial, inv = pick(get("ln_serial")) or None, pick(get("ln_inv")) or None
                if not nom_raw and not name and not qty_raw and not serial and not inv:
                    continue
                if nom_raw.isdigit():
                    nom_id = int(nom_raw)
                elif name:
                    nom_id = find_nomenclature(conn, name)
                else:
                    raise ValueError("впишіть або оберіть найменування")
                nom = None
                if nom_id is not None:
                    nom = conn.execute("SELECT * FROM nomenclature WHERE id = ?", (nom_id,)).fetchone()
                    if nom is None:
                        raise ValueError("найменування не знайдено")
                serial_tracked = nom["serial_tracked"] if nom else pick(get("ln_new_serial")) == "1"
                new_serial = serial_tracked
                if order:
                    serial_tracked = False
                title = nom["name"] if nom else " ".join(name.split())
                cat_raw = pick(get("ln_cat"))
                cat = int(cat_raw) if cat_raw else None
                price = parse_money(pick(get("ln_price"))) or 0
                qty = parse_qty(qty_raw)
                if req_raw:
                    parse_qty(req_raw)
                if serial_tracked:
                    if not serial and not inv:
                        raise ValueError(f"«{title}» обліковується поштучно — "
                                         "вкажіть заводський або інвентарний номер")
                    if qty_raw and qty != 1000:
                        raise ValueError("для поштучного майна — один рядок на одну одиницю (кількість 1)")
                    qty = 1000
                elif not qty:
                    raise ValueError("вкажіть кількість більше нуля")
                # Рядок перевірено — лише тепер додаємо нове в довідники.
                if nom_id is None:
                    nom_id = create_nomenclature(conn, name, pick(get("ln_new_uom")),
                                                 pick(get("ln_new_class")), new_serial)
                item_id = find_or_create_item(conn, nom_id, serial, inv) if serial_tracked else None
            if not qty:
                raise ValueError("вкажіть кількість більше нуля")
            result.append({"nomenclature_id": nom_id, "item_id": item_id, "category": cat,
                           "price_kop": price, "qty_m": qty,
                           "qty_requested_m": parse_qty(req_raw) if req_raw else None,
                           "note": note or None,
                           "years_norm": pick(get("ln_ynorm")) or None,
                           "years_fact": pick(get("ln_yfact")) or None})
        except ValueError as exc:
            errors.append(f"Рядок {n}: {exc}")
    return result, errors


def _line_rows(conn, doc_lines) -> list[dict]:
    labels = refs.Labels(conn)
    rows = []
    for ln in doc_lines:
        nom = conn.execute("SELECT * FROM nomenclature WHERE id = ?", (ln["nomenclature_id"],)).fetchone()
        item = (conn.execute("SELECT * FROM items WHERE id = ?", (ln["item_id"],)).fetchone()
                if ln["item_id"] else None)
        rows.append({
            **dict(ln),
            "key": _stock_key(ln["nomenclature_id"], ln["item_id"], ln["category"], ln["price_kop"]),
            "name": labels.get("nomenclature", ln["nomenclature_id"]),
            "item_label": labels.get("items", ln["item_id"]) if ln["item_id"] else "",
            "serial_no": item["serial_no"] if item else "",
            "inventory_no": item["inventory_no"] if item else "",
            "code": nom["code"] or "",
            "uom": nom["uom"],
            "serial_tracked": nom["serial_tracked"],
            "qty": format_qty(ln["qty_m"]),
            "req": format_qty(ln["qty_requested_m"]) if ln["qty_requested_m"] is not None else "",
            "price": format_money(ln["price_kop"]).replace(" ", " "),
            "sum": format_money(balances.value_kop(ln["qty_m"], ln["price_kop"])),
            "cat": CATEGORY_TITLES.get(ln["category"], ""),
        })
    return rows


# --- Перегляд і редагування ---------------------------------------------------

@bp.route("/documents/<int:doc_id>", methods=["GET", "POST"])
def edit(doc_id):
    conn = get_db()
    try:
        doc = documents.get(conn, doc_id)
    except DocumentError:
        abort(404)
    if doc["status"] != "draft":
        return _view(conn, doc)

    errors: list[str] = []
    check: list[str] | None = None
    if request.method == "POST":
        action = request.form.get("action", "save")
        if action == "cancel":
            documents.cancel(conn, doc_id)
            flash("Чернетку анульовано. Номер лишається в журналі з позначкою «анульовано».", "ok")
            return redirect(url_for("documents.index"))
        try:
            _save_header(conn, doc_id, doc["doc_type"], request.form)
        except (DocumentError, locations.LocationError) as exc:
            errors += getattr(exc, "errors", None) or [str(exc)]
        doc = documents.get(conn, doc_id)
        new_lines, line_errors = parse_lines(conn, request.form,
                                             request.form.get("mode") == "out",
                                             order=doc["doc_type"] == "order")
        if doc["doc_type"] == "condition":   # друга таблиця — «Оприбуткувати»
            in_lines, in_errors = parse_lines(conn, Prefixed(request.form, "in_"), outgoing=False)
            new_lines += [{**ln, "direction": "in"} for ln in in_lines]
            line_errors += [f"Оприбуткувати, р{e[1:]}" for e in in_errors]
        errors += line_errors
        if not line_errors:
            documents.set_lines(conn, doc_id, new_lines)
        if not errors:
            if action == "post":
                try:
                    documents.post(conn, doc_id)
                except DocumentError as exc:
                    errors = exc.errors
                else:
                    flash(f"Проведено: {documents.label(conn, doc_id)}", "ok")
                    filled = condition.fill_after_post(conn, doc_id) if doc["doc_type"] == "condition" else None
                    if filled:
                        flash(f"Склад «{refs.Labels(conn).get('items', filled[0])}» заповнено зі списаного "
                              f"({filled[1]} складових) — перевірте в картці одиниці.", "ok")
                    return redirect(url_for("documents.edit", doc_id=doc_id))
            elif action == "check":
                check = documents.validate(conn, doc_id)
            else:
                flash("Чернетку збережено.", "ok")
                return redirect(url_for("documents.edit", doc_id=doc_id))
        doc = documents.get(conn, doc_id)

    doc_lines = documents.lines(conn, doc_id)
    outgoing = is_outgoing(conn, doc)
    values = {**dict(doc),
              "from_place": location_to_place(conn, doc["from_location_id"], "from"),
              "to_place": location_to_place(conn, doc["to_location_id"]),
              "extra": {**documents.order_extra(doc), **documents.condition_extra(doc)},
              "copies": documents.copies(conn, doc),
              "copies_auto": documents.default_copies(conn, doc),
              "copy_options": copy_options(conn, doc)}
    if request.method == "POST" and errors:
        values.update({k: request.form.get(k, "") for k in
                       ("from_place", "to_place", "basis", "doc_no", "reg_no")})
        values["extra"] = _extra_values(request.form)
        order_raw = request.form.get("order_id", "")
        values["order_id"] = int(order_raw) if order_raw.isdigit() else None
        values["copies"] = [c for c in request.form.getlist("copy") if c.strip()]
    rows = _line_rows(conn, [ln for ln in doc_lines if not ln["direction"]])
    in_rows = _line_rows(conn, [ln for ln in doc_lines if ln["direction"]])
    if request.method == "POST" and errors:
        rows = _raw_rows(request.form, request.form.get("mode") == "out")
        in_rows = _raw_rows(Prefixed(request.form, "in_"), False)
    return render_template(
        "documents_edit.html", doc=doc, values=values, errors=errors, check=check,
        situation=help.SITUATION_BY_CODE[doc["doc_type"]], doc_type=doc["doc_type"],
        type_title=DOC_TYPES[doc["doc_type"]].title, outgoing=outgoing, rows=rows, in_rows=in_rows,
        stock=stock_options(conn, doc, doc_lines) if outgoing else [],
        nomenclature=_nomenclature_json(conn),
        uoms=refs.UOMS, classes=refs.ACCOUNTING_CLASSES,
        categories=CATEGORY_TITLES,
        totals=_totals(doc_lines),
        totals_out=_totals([ln for ln in doc_lines if not ln["direction"]]),
        totals_in=_totals([ln for ln in doc_lines if ln["direction"]]),
        mvo_place=_mvo_person_place(conn, doc), word_forms=word_forms(conn, doc["id"]),
        order_link=_order_link(conn, doc), order_warnings=orders.warnings(conn, doc["id"]),
        from_title=locations.title(conn, doc["from_location_id"]),
        to_title=locations.title(conn, doc["to_location_id"]),
        fmt_date=documents.fmt_date,
        **_header_choices(conn, doc["doc_type"], values),
    )


def _nomenclature_json(conn) -> list[dict]:
    """Найменування для підказок у рядках: підпис, од. виміру, поштучний облік."""
    labels = refs.Labels(conn)
    return [{"id": r["id"], "label": labels.get("nomenclature", r["id"]), "uom": r["uom"],
             "serial": bool(r["serial_tracked"])}
            for r in conn.execute("SELECT id, uom, serial_tracked FROM nomenclature "
                                  "WHERE is_active = 1 ORDER BY name COLLATE UK")]


def _mvo_person_place(conn, doc) -> str | None:
    """Початкові залишки внесено на особу, яка є МВО місця обліку: майно під звітом МВО
    має рахуватися на місці обліку, а не як видане їй в особисте користування."""
    if doc["doc_type"] != "opening":
        return None
    loc = locations.get(conn, doc["to_location_id"])
    if loc["kind"] != "person":
        return None
    row = conn.execute("SELECT id FROM units WHERE is_accounting = 1 AND mvo_person_id = ? "
                       "ORDER BY name COLLATE UK LIMIT 1", (loc["person_id"],)).fetchone()
    return refs.Labels(conn).get("units", row["id"]) if row else None


def _raw_rows(form, outgoing: bool) -> list[dict]:
    """Рядки так, як їх ввів користувач (щоб не губити введене при помилці)."""
    names = ["ln_key"] if outgoing else ["ln_nom", "ln_name", "ln_new_uom", "ln_new_class",
                                         "ln_new_serial", "ln_serial", "ln_inv", "ln_price", "ln_cat"]
    lists = {n: form.getlist(n) for n in names + ["ln_qty", "ln_req", "ln_note", "ln_ynorm", "ln_yfact"]}
    count = max((len(v) for v in lists.values()), default=0)
    rows = []
    for i in range(count):
        v = {n: (lst[i] if i < len(lst) else "") for n, lst in lists.items()}
        if not any(x.strip() for x in v.values()):
            continue
        rows.append({
            "key": v.get("ln_key", ""),
            "nomenclature_id": int(v["ln_nom"]) if v.get("ln_nom", "").isdigit() else None,
            "name": v.get("ln_name", ""), "new_uom": v.get("ln_new_uom", ""),
            "new_class": v.get("ln_new_class", ""), "new_serial": v.get("ln_new_serial", ""),
            "serial_no": v.get("ln_serial", ""), "inventory_no": v.get("ln_inv", ""),
            "price": v.get("ln_price", ""),
            "category": int(v["ln_cat"]) if v.get("ln_cat", "").isdigit() else None,
            "qty": v["ln_qty"], "req": v["ln_req"], "note": v["ln_note"], "sum": "",
            "years_norm": v["ln_ynorm"], "years_fact": v["ln_yfact"],
        })
    return rows


def _totals(doc_lines) -> dict:
    total = sum(balances.value_kop(ln["qty_m"], ln["price_kop"]) for ln in doc_lines)
    return {"count": len(doc_lines), "sum": format_money(total)}


def _view(conn, doc):
    doc_lines = documents.lines(conn, doc["id"])
    related = {}
    if doc["reversal_of_id"]:
        related["reversal_of"] = (doc["reversal_of_id"], documents.label(conn, doc["reversal_of_id"]))
    if doc["reversed_by_id"]:
        related["reversed_by"] = (doc["reversed_by_id"], documents.label(conn, doc["reversed_by_id"]))
    labels = refs.Labels(conn)
    return render_template(
        "documents_view.html", nav=_neighbours(conn, doc), word_forms=word_forms(conn, doc["id"]),
        from_link=place_link(conn, doc["from_location_id"]),
        to_link=place_link(conn, doc["to_location_id"]), doc=doc, label=documents.label(conn, doc["id"]),
        type_title=DOC_TYPES[doc["doc_type"]].title, status_title=STATUS_TITLES[doc["status"]],
        from_title=locations.title(conn, doc["from_location_id"]),
        to_title=locations.title(conn, doc["to_location_id"]),
        service=labels.get("services", doc["service_id"]),
        recipient=labels.get("persons", doc["recipient_person_id"]),
        rows=_line_rows(conn, doc_lines), totals=_totals(doc_lines), related=related,
        totals_out=_totals([ln for ln in doc_lines if not ln["direction"]]),
        totals_in=_totals([ln for ln in doc_lines if ln["direction"]]),
        fmt_date=documents.fmt_date, today=date.today().isoformat(),
        history=_doc_history(conn, doc["id"]),
        mvo_place=(_mvo_person_place(conn, doc)
                   if doc["status"] == "posted" and not doc["reversed_by_id"] else None),
        order_link=_order_link(conn, doc), **_order_execution(conn, doc),
        obtained=([{"id": r["id"], "label": labels.get("items", r["id"]), "n": r["n_components"]}
                   for r in condition.obtained_items(conn, doc["id"])]
                  if doc["doc_type"] == "condition" and doc["status"] == "posted" else []),
        page_hint=help.PAGE_HINTS["documents.view_order" if doc["doc_type"] == "order" else "documents.view"],
    )


def _order_link(conn, doc) -> tuple[int, str] | None:
    """Наряд, за яким оформлено документ."""
    return (doc["order_id"], documents.label(conn, doc["order_id"])) if doc["order_id"] else None


def _order_execution(conn, doc) -> dict:
    """Для підписаного наряду: виконання по рядках, документи за ним, чи можна оформити тут."""
    if doc["doc_type"] != "order" or doc["status"] != "posted":
        return {}
    ex = orders.execution(conn, doc["id"])
    labels = refs.Labels(conn)
    noms = {r["id"]: r["uom"] for r in conn.execute("SELECT id, uom FROM nomenclature")}
    rows = [{"n": x.line["line_no"], "name": labels.get("nomenclature", x.line["nomenclature_id"]),
             "uom": noms[x.line["nomenclature_id"]], "cat": CATEGORY_TITLES.get(x.line["category"], ""),
             "ordered": format_qty(x.ordered_m), "fact": format_qty(x.fact_m) if x.fact_m else "",
             "remaining": format_qty(x.remaining_m) if x.remaining_m else "", "docs": x.docs,
             "over": x.fact_m > x.ordered_m}
            for x in ex.lines]
    docs = [{"id": d["id"], "ref": orders.paper_ref(d), "status": d["status"],
             "status_title": STATUS_TITLES[d["status"]], "reversed": bool(d["reversed_by_id"])}
            for d in orders.linked_docs(conn, doc["id"])]
    marks = [{**dict(m), "qty": format_qty(m["qty_m"]), "date": documents.fmt_date(m["mark_date"])}
             for m in orders.marks(conn, doc["id"])]
    return {"execution": ex, "exec_rows": rows, "exec_docs": docs, "marks": marks,
            "mark_lines": [(x.line["id"], f"{r['n']}. {r['name']}"
                            + (f" — лишилося {r['remaining']} {r['uom']}" if r["remaining"] else " — видано повністю"))
                           for x, r in zip(ex.lines, rows)],
            # За замовчуванням — перший рядок, за яким ще є що видати.
            "mark_default": next((x.line["id"] for x in ex.lines if x.remaining_m), None),
            "exec_type": orders.doc_type_for(conn, doc)}


@bp.post("/documents/<int:doc_id>/marks")
def add_mark(doc_id):
    """Ручна відмітка про виконання наряду (видано поза програмою)."""
    conn = get_db()
    f = request.form
    try:
        qty = parse_qty(f.get("qty", "")) if f.get("qty", "").strip() else 0
        orders.add_mark(conn, doc_id, int(f["line_id"]) if f.get("line_id", "").isdigit() else 0,
                        qty, f.get("doc_ref", ""), f.get("mark_date") or None, f.get("note"))
    except ValueError as exc:
        flash(f"Відмітку не збережено: {exc}", "error")
    except DocumentError as exc:
        for e in exc.errors:
            flash(f"Відмітку не збережено: {e}", "error")
    else:
        flash("Відмітку про виконання збережено.", "ok")
    return redirect(url_for("documents.edit", doc_id=doc_id, _anchor="execution"))


@bp.post("/documents/marks/<int:mark_id>/delete")
def delete_mark(mark_id):
    conn = get_db()
    try:
        order_id = orders.delete_mark(conn, mark_id)
    except DocumentError:
        abort(404)
    flash("Відмітку видалено (запис про це лишився в історії наряду).", "ok")
    return redirect(url_for("documents.edit", doc_id=order_id, _anchor="execution"))


@bp.post("/documents/<int:doc_id>/components/<int:item_id>")
def fill_components(doc_id, item_id):
    """Акт якісного стану: склад оприбуткованої системи — зі списаного."""
    conn = get_db()
    try:
        n = condition.fill_components(conn, doc_id, item_id)
    except DocumentError as exc:
        for e in exc.errors:
            flash(e, "error")
    else:
        flash(f"Склад «{refs.Labels(conn).get('items', item_id)}» заповнено зі списаного ({n} складових).", "ok")
    return redirect(url_for("documents.edit", doc_id=doc_id))


@bp.post("/documents/<int:doc_id>/execute")
def execute(doc_id):
    """Оформити накладну / акт за нарядом: чернетка з тими самими сторонами й рядками."""
    conn = get_db()
    try:
        new_id = orders.create_draft(conn, doc_id)
    except DocumentError as exc:
        for e in exc.errors:
            flash(e, "error")
        return redirect(url_for("documents.edit", doc_id=doc_id))
    flash("Створено чернетку за нарядом: рядки — те, що ще лишилося видати. Перевірте кількість "
          "і поштучні одиниці, потім проведіть.", "ok")
    return redirect(url_for("documents.edit", doc_id=new_id))


def _neighbours(conn, doc) -> dict:
    """Попередній і наступний документ за реєстраційним номером (у межах року)."""
    def one(sql):
        r = conn.execute(sql, (doc["reg_year"], doc["reg_no"])).fetchone()
        return (r["id"], r["reg_no"]) if r else None
    return {"prev": one("SELECT id, reg_no FROM documents WHERE reg_year = ? AND reg_no < ? "
                        "ORDER BY reg_no DESC LIMIT 1"),
            "next": one("SELECT id, reg_no FROM documents WHERE reg_year = ? AND reg_no > ? "
                        "ORDER BY reg_no LIMIT 1")}


def place_link(conn, location_id: int) -> str | None:
    """Куди перейти з місця: залишки місця обліку чи особи, картка контрагента."""
    loc = locations.get(conn, location_id)
    if loc["kind"] == "unit":
        return url_for("reports.stock", scope=f"u:{loc['unit_id']}")
    if loc["kind"] == "person":
        return url_for("reports.stock", scope=f"p:{loc['person_id']}")
    if loc["kind"] == "counterparty":
        return url_for("refs.edit", kind="counterparties", id_=loc["counterparty_id"])
    return None


def _doc_history(conn, doc_id):
    from .refs import _history
    return _history(conn, "documents", doc_id)


@bp.post("/documents/<int:doc_id>/reverse")
def reverse(doc_id):
    conn = get_db()
    try:
        storno_id = documents.reverse(conn, doc_id, on_date=request.form.get("on_date") or None,
                                      basis=request.form.get("basis", "").strip() or None)
    except DocumentError as exc:
        for e in exc.errors:
            flash(e, "error")
        return redirect(url_for("documents.edit", doc_id=doc_id))
    flash("Документ сторновано. Тепер, за потреби, оформіть правильний документ.", "ok")
    return redirect(url_for("documents.edit", doc_id=storno_id))


# --- Документи Word ------------------------------------------------------------------

def _paths():
    from flask import current_app
    return current_app.config["STORAGE"].paths


def word_forms(conn, doc_id: int) -> list:
    return docx_out.available_forms(conn, doc_id)


@bp.get("/documents/<int:doc_id>/docx/<form_code>")
def docx(doc_id, form_code):
    from io import BytesIO
    from flask import send_file
    conn = get_db()
    try:
        data, name, _saved = docx_out.render(conn, _paths(), doc_id, form_code)
    except DocumentError:
        abort(404)
    except docx_out.DocxError as exc:
        flash(str(exc), "error")
        return redirect(url_for("documents.edit", doc_id=doc_id))
    return send_file(BytesIO(data), as_attachment=True, download_name=name,
                     mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document")


@bp.get("/documents/<int:doc_id>/pdf/<form_code>")
def pdf(doc_id, form_code):
    """PDF відкривається просто в браузері — звідти його друкують."""
    from io import BytesIO
    from flask import send_file
    conn = get_db()
    try:
        data, name = pdf_out.render(conn, _paths(), doc_id, form_code)
    except DocumentError:
        abort(404)
    except (docx_out.DocxError, pdf_out.PdfError) as exc:
        flash(str(exc), "error")
        return redirect(url_for("documents.edit", doc_id=doc_id))
    return send_file(BytesIO(data), as_attachment=False, download_name=name, mimetype="application/pdf")


@bp.route("/templates-docx", methods=["GET", "POST"])
def templates_docx():
    paths = _paths()
    if request.method == "POST":
        form = docx_out.FORMS.get(request.form.get("form", ""))
        if form is None:
            abort(404)
        try:
            backup = docx_out.reset_template(paths, form)
        except OSError as exc:
            flash(f"Не вдалося: {exc}", "error")
        else:
            flash(f"Повернуто стандартний шаблон «{form.title}»."
                  + (f" Ваш змінений збережено як {backup.name}." if backup else ""), "ok")
        return redirect(url_for("documents.templates_docx"))
    forms = []
    for f in docx_out.FORMS.values():
        user = paths.templates_docx / f.file
        status = docx_out.template_status(paths, f)
        forms.append({"form": f, "path": user, "exists": user.exists(), "status": status})
    return render_template("templates_docx.html", forms=forms, fields=docx_out.FIELDS,
                           folder=paths.templates_docx, output=paths.output)
