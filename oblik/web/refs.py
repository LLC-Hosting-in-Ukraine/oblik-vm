"""Сторінки довідників, налаштувань і журналу змін."""
from __future__ import annotations

import json

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from .. import audit, refs, settings
from . import get_db

bp = Blueprint("refs", __name__)

# Порядок довідників у меню.
MENU = ["units", "persons", "counterparties", "services", "nomenclature", "items"]


def _ref(kind: str) -> refs.Ref:
    ref = refs.REFS.get(kind)
    if ref is None:
        abort(404)
    return ref


def _fk_options(conn, ref: refs.Ref, record: dict, own_id: int | None) -> dict:
    result = {}
    for f in ref.fields:
        if f.kind == "fk":
            exclude = own_id if f.fk == ref.kind else None
            result[f.name] = refs.options(conn, f.fk, current=record.get(f.name), exclude=exclude)
    return result


@bp.get("/refs")
def index():
    conn = get_db()
    items = [(refs.REFS[k], refs.count(conn, refs.REFS[k])) for k in MENU]
    return render_template("refs_index.html", items=items)


@bp.get("/refs/<kind>")
def list_view(kind):
    ref = _ref(kind)
    conn = get_db()
    q = request.args.get("q", "")
    show_inactive = bool(request.args.get("inactive"))
    if kind == "units" and not q.strip():
        return render_template("refs_units_tree.html", ref=ref, nodes=units_tree(conn, show_inactive),
                               show_inactive=show_inactive, q=q)
    rows = refs.list_rows(conn, ref, q, show_inactive)
    labels = refs.Labels(conn)
    table = [
        {"id": r["id"], "active": r["is_active"],
         "cells": [refs.display(f, r.get(f.name), labels) for f in ref.list_fields]}
        for r in rows
    ]
    return render_template("refs_list.html", ref=ref, table=table, q=q,
                           show_inactive=show_inactive)


def units_tree(conn, show_inactive: bool = False) -> list[dict]:
    """Структура частини деревом: вузли по порядку з глибиною (для відступів)."""
    labels = refs.Labels(conn)
    kinds = dict(refs.UNIT_KINDS)
    rows = conn.execute("SELECT * FROM units " + ("" if show_inactive else "WHERE is_active = 1 ")
                        + "ORDER BY name COLLATE UK").fetchall()
    ids = {r["id"] for r in rows}
    children: dict[int | None, list] = {}
    for r in rows:
        parent = r["parent_id"] if r["parent_id"] in ids else None
        children.setdefault(parent, []).append(r)
    persons = dict(conn.execute("SELECT unit_id, COUNT(*) FROM persons WHERE is_active = 1 "
                                "GROUP BY unit_id").fetchall())
    result: list[dict] = []
    seen: set[int] = set()

    def walk(parent, depth, prefix_last):
        kids = children.get(parent, [])
        for i, r in enumerate(kids):
            if r["id"] in seen:   # захист від циклу в даних
                continue
            seen.add(r["id"])
            last = i == len(kids) - 1
            result.append({
                "id": r["id"], "name": r["name"], "short_name": r["short_name"],
                "kind": kinds.get(r["kind"], r["kind"]), "depth": depth,
                "accounting": r["is_accounting"], "active": r["is_active"],
                "mvo": labels.get("persons", r["mvo_person_id"]) if r["is_accounting"] else "",
                "persons": persons.get(r["id"], 0),
                "guides": prefix_last + [last],
            })
            walk(r["id"], depth + 1, prefix_last + [last])

    walk(None, 0, [])
    return result


@bp.route("/refs/<kind>/new", methods=["GET", "POST"])
def create(kind):
    return _edit(_ref(kind), None)


@bp.route("/refs/<kind>/<int:id_>", methods=["GET", "POST"])
def edit(kind, id_):
    return _edit(_ref(kind), id_)


def _edit(ref: refs.Ref, id_: int | None):
    conn = get_db()
    errors: dict[str, str] = {}
    if request.method == "POST":
        values, errors = refs.parse_form(conn, ref, request.form)
        if not errors:
            try:
                new_id = refs.save(conn, ref, id_, values)
            except refs.ValidationError as exc:
                errors = exc.errors
            else:
                flash(f"Збережено: {refs.Labels(conn).get(ref.kind, new_id)}", "ok")
                if request.form.get("then") == "new":
                    return redirect(url_for("refs.create", kind=ref.kind))
                return redirect(url_for("refs.list_view", kind=ref.kind))
        record = values
    elif id_ is None:
        record = {f.name: f.default for f in ref.fields}
        for f in ref.fields:   # підказане з адреси, наприклад ?parent_id=3
            if f.kind == "fk" and request.args.get(f.name, "").isdigit():
                record[f.name] = int(request.args[f.name])
    else:
        record = refs.get(conn, ref, id_)
        if record is None:
            abort(404)

    history = []
    if id_ is not None:
        history = _history(conn, ref.kind, id_)
    return render_template(
        "refs_form.html", ref=ref, id_=id_, record=record, errors=errors,
        options=_fk_options(conn, ref, record, id_), form_value=refs.form_value,
        format_qty=refs.format_qty, history=history,
        is_active=(refs.get(conn, ref, id_) or {}).get("is_active", 1) if id_ else 1,
    )


@bp.post("/refs/<kind>/<int:id_>/delete")
def delete(kind, id_):
    ref = _ref(kind)
    conn = get_db()
    try:
        refs.delete(conn, ref, id_)
    except refs.InUseError as exc:
        flash(str(exc), "error")
        return redirect(url_for("refs.edit", kind=kind, id_=id_))
    flash("Запис видалено.", "ok")
    return redirect(url_for("refs.list_view", kind=kind))


@bp.post("/refs/<kind>/<int:id_>/active")
def set_active(kind, id_):
    ref = _ref(kind)
    active = request.form.get("active") == "1"
    refs.set_active(get_db(), ref, id_, active)
    flash("Запис знову активний." if active else
          "Запис неактивний: він не пропонується у списках вибору, історію збережено.", "ok")
    return redirect(url_for("refs.edit", kind=kind, id_=id_))


# --- Налаштування -------------------------------------------------------------

@bp.route("/settings", methods=["GET", "POST"])
def settings_view():
    conn = get_db()
    errors: dict[str, str] = {}
    if request.method == "POST":
        values, errors = settings.parse_form(conn, request.form)
        if not errors:
            try:
                settings.save(conn, values)
            except refs.ValidationError as exc:
                errors = exc.errors
            else:
                flash("Налаштування збережено.", "ok")
                return redirect(url_for("refs.settings_view"))
        record = values
    else:
        record = settings.load(conn)
    options = {f.name: refs.options(conn, f.fk, current=record.get(f.name))
               for f in settings.FIELDS if f.kind == "fk"}
    return render_template("settings.html", fields=settings.FIELDS, record=record,
                           errors=errors, options=options, form_value=refs.form_value)


# --- Журнал змін --------------------------------------------------------------

ENTITY_TITLES = {k: r.title for k, r in refs.REFS.items()} | {
    "settings": "Налаштування", "documents": "Документи"}


def _history(conn, entity: str | None = None, entity_id: int | None = None,
             limit: int = 200) -> list[dict]:
    where, params = [], []
    if entity:
        where.append("entity = ?")
        params.append(entity)
    if entity_id is not None:
        where.append("entity_id = ?")
        params.append(entity_id)
    sql = "SELECT * FROM audit_log"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" ORDER BY id DESC LIMIT {int(limit)}"
    result = []
    for r in conn.execute(sql, params):
        row = dict(r)
        row["action_title"] = audit.ACTIONS.get(row["action"], row["action"])
        row["entity_title"] = ENTITY_TITLES.get(row["entity"], row["entity"])
        row["changes"] = json.loads(row["details"]) if row["details"] else []
        result.append(row)
    return result


@bp.get("/audit")
def audit_view():
    entity = request.args.get("entity") or None
    rows = _history(get_db(), entity, limit=500)
    return render_template("audit.html", rows=rows, entity=entity, entities=ENTITY_TITLES)
