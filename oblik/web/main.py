"""Головна сторінка, відновлення бази, завершення роботи."""
from __future__ import annotations

from flask import (Blueprint, abort, current_app, flash, redirect, render_template, request,
                   send_from_directory, url_for)

from .. import dashboard, help, onboarding, settings
from ..config import bundle_dir
from ..storage import RECOVERY_CONFLICT, Storage
from ..textutil import uk_sort_key
from . import get_db

bp = Blueprint("main", __name__)


def _storage() -> Storage:
    return current_app.config["STORAGE"]


@bp.get("/")
def index():
    conn = get_db()
    meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
    steps = onboarding.steps(conn)
    return render_template("index.html", meta=meta, paths=_storage().paths,
                           settings_complete=settings.is_complete(conn), steps=steps,
                           started=onboarding.is_started(conn), m=dashboard.metrics(conn),
                           reminders=dashboard.reminders(conn, _storage()),
                           settings_values=settings.load(conn))


@bp.route("/terms", methods=["GET", "POST"])
def terms():
    conn = get_db()
    target = request.values.get("next") or ""
    if not target.startswith("/") or target.startswith("//") or target.startswith("/terms"):
        target = ""
    error = None
    if request.method == "POST":
        if request.form.get("agree") == "1":
            onboarding.accept_terms(conn)
            return redirect(target or url_for("main.start"))
        error = "Поставте позначку, що ви ознайомилися з правилами і погоджуєтеся з ними."
    return render_template("terms.html", accepted=onboarding.terms_accepted(conn), error=error,
                           next=target, paths=_storage().paths, work_db=_storage().work_db)


@bp.get("/start")
def start():
    conn = get_db()
    steps = onboarding.steps(conn)
    return render_template("start.html", steps={s.code: s for s in steps}, step_list=steps,
                           started=onboarding.is_started(conn))


@bp.route("/recovery", methods=["GET", "POST"])
def recovery():
    storage = _storage()
    if storage.recovery is None:
        return redirect(url_for("main.index"))
    if request.method == "POST":
        keep_local = request.form.get("choice") == "local"
        try:
            storage.resolve_recovery(keep_local)
        except OSError as exc:
            flash(f"Не вдалося: {exc}. Перевірте, що флешку вставлено, і спробуйте ще раз.",
                  "error")
            return redirect(url_for("main.recovery"))
        flash("Зміни з цього ПК відновлено й записано на флешку." if keep_local
              else "Продовжуємо з версією з флешки. Копію з ПК відкладено в backups/.",
              "ok")
        return redirect(url_for("main.index"))
    return render_template("recovery.html", conflict=storage.recovery == RECOVERY_CONFLICT)


@bp.get("/help")
def help_view():
    terms = sorted(help.GLOSSARY.values(), key=lambda t: uk_sort_key(t.title))
    return render_template("help.html", terms=terms, situations=help.SITUATIONS, normative=help.NORMATIVE)


@bp.get("/help/normative/<name>")
def normative(name):
    """Нормативний документ, вшитий у програму (працює без інтернету)."""
    if name not in help.NORMATIVE_FILES:
        abort(404)
    return send_from_directory(bundle_dir() / "docs" / "normative", name)


@bp.get("/about")
def about():
    license_path = bundle_dir() / "LICENSE"
    license_text = license_path.read_text(encoding="utf-8") if license_path.exists() else ""
    conn = get_db()
    schema = conn.execute("PRAGMA user_version").fetchone()[0]
    return render_template("about.html", license_text=license_text, schema=schema,
                           paths=_storage().paths)


@bp.post("/shutdown")
def shutdown():
    stop = current_app.config.get("SHUTDOWN")
    if stop:
        stop()
    return render_template("shutdown.html")
