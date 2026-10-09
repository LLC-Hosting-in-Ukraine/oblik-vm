"""Сторінки звітів: залишки на дату (з Excel), картка руху одиниці."""
from __future__ import annotations

from datetime import date
from io import BytesIO

from flask import Blueprint, abort, render_template, request, send_file

from .. import documents, refs, registers, reports
from ..reports import ReportError
from ..textutil import format_money, format_qty
from . import get_db

bp = Blueprint("reports", __name__)

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@bp.get("/reports")
def index():
    return render_template("reports_index.html")


def _stock_args() -> tuple[str, str, bool]:
    on_date = request.args.get("on_date") or date.today().isoformat()
    try:
        date.fromisoformat(on_date)
    except ValueError:
        on_date = date.today().isoformat()
    return request.args.get("scope", "all"), on_date, request.args.get("sub") == "1"


@bp.get("/reports/stock")
def stock():
    conn = get_db()
    scope, on_date, sub = _stock_args()
    report, error = None, None
    try:
        report = reports.stock(conn, scope, on_date, sub)
    except ReportError as exc:
        error = str(exc)
    rows = []
    if report:
        rows = [{**r, "price": format_money(r["price_kop"]), "qty": format_qty(r["qty_m"]),
                 "sum": format_money(r["value_kop"])} for r in report.rows]
    return render_template("reports_stock.html", report=report, rows=rows, error=error,
                           scope=scope, on_date=on_date, sub=sub,
                           choices=reports.scope_choices(conn),
                           total=format_money(report.total_kop) if report else "",
                           fmt_date=documents.fmt_date)


@bp.get("/reports/stock.xlsx")
def stock_xlsx():
    conn = get_db()
    scope, on_date, sub = _stock_args()
    try:
        report = reports.stock(conn, scope, on_date, sub)
    except ReportError:
        abort(400)
    data = reports.stock_xlsx(conn, report)
    return send_file(BytesIO(data), mimetype=XLSX_MIME, as_attachment=True,
                     download_name=f"Залишки на {documents.fmt_date(on_date)}.xlsx")


@bp.get("/reports/items")
def items():
    q = request.args.get("q", "").strip()
    return render_template("reports_items.html", q=q, table=reports.find_items(get_db(), q))


@bp.get("/reports/items/<int:item_id>")
def item_card(item_id):
    try:
        card = reports.item_card(get_db(), item_id)
    except ReportError:
        abort(404)
    return render_template("reports_item_card.html", card=card)


# --- Дод. 1. Узагальнююча відомість ------------------------------------------------

def _iso(value: str | None, default: str) -> str:
    try:
        return date.fromisoformat(value).isoformat() if value else default
    except ValueError:
        return default


def _person_arg(name: str, default: int | None) -> int | None:
    raw = request.args.get(name)
    if raw is None:
        return default
    return int(raw) if raw.isdigit() else None


def _summary_args(conn):
    d_from, d_to = registers.default_period()
    scope = request.args.get("scope", "all")
    date_from = _iso(request.args.get("from"), d_from)
    date_to = _iso(request.args.get("to"), d_to)
    sub = request.args.get("sub") == "1"
    report = registers.summary(conn, scope, date_from, date_to, sub)
    form = {
        "scope": scope, "from": date_from, "to": date_to, "sub": sub,
        "number": request.args.get("number", "").strip(),
        "book": request.args.get("book", "").strip() or report.default_book,
        "book_details": request.args.get("book_details", "").strip(),
        "compiled": _iso(request.args.get("compiled"), date.today().isoformat()),
        "resp": _person_arg("resp", report.default_responsible_id),
        "keeper": _person_arg("keeper", report.default_keeper_id),
    }
    return report, form


@bp.get("/reports/summary")
def summary():
    conn = get_db()
    report, form, error = None, None, None
    try:
        report, form = _summary_args(conn)
    except ReportError as exc:
        error = str(exc)
        d_from, d_to = registers.default_period()
        form = {"scope": request.args.get("scope", "all"), "from": d_from, "to": d_to, "sub": False,
                "number": "", "book": "", "book_details": "", "compiled": date.today().isoformat(),
                "resp": None, "keeper": None}
    rows = [{**r, "in": format_qty(r["in_m"]), "out": format_qty(r["out_m"]), "end": format_qty(r["end_m"])}
            for r in (report.rows if report else [])]
    xlsx_args = {k: ("1" if v is True else v) for k, v in form.items() if v not in (None, False, "")}
    return render_template("reports_summary.html", report=report, rows=rows, form=form, error=error,
                           choices=reports.scope_choices(conn), persons=refs.options(conn, "persons"),
                           xlsx_args=xlsx_args, fmt_date=documents.fmt_date)


@bp.get("/reports/summary.xlsx")
def summary_xlsx():
    conn = get_db()
    try:
        report, form = _summary_args(conn)
    except ReportError:
        abort(400)
    data = registers.summary_xlsx(conn, report, form["number"], form["book"], form["book_details"],
                                  form["compiled"], form["resp"], form["keeper"])
    name = (f"Узагальнююча відомість {documents.fmt_date(form['from'])}-"
            f"{documents.fmt_date(form['to'])}.xlsx")
    return send_file(BytesIO(data), mimetype=XLSX_MIME, as_attachment=True, download_name=name)


# --- Дод. 2. Книга реєстрації та руху облікових документів ---------------------------

def _journal_year(conn) -> tuple[int, list[int]]:
    year = request.args.get("year", type=int) or date.today().year
    years = [r[0] for r in conn.execute("SELECT DISTINCT reg_year FROM documents ORDER BY reg_year DESC")]
    if year not in years:
        years.insert(0, year)
    return year, years


@bp.get("/reports/journal")
def journal():
    conn = get_db()
    year, years = _journal_year(conn)
    return render_template("reports_journal.html", j=registers.journal(conn, year), year=year, years=years)


@bp.get("/reports/journal.xlsx")
def journal_xlsx():
    conn = get_db()
    year, _ = _journal_year(conn)
    data = registers.journal_xlsx(conn, registers.journal(conn, year))
    return send_file(BytesIO(data), mimetype=XLSX_MIME, as_attachment=True,
                     download_name=f"Книга реєстрації документів {year}.xlsx")
