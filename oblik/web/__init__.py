"""Веб-інтерфейс (Flask) на 127.0.0.1."""
from __future__ import annotations

import os
import sqlite3

from flask import Flask, g, redirect, request, url_for

from .. import __copyright__, __version__, db, help, onboarding
from ..config import APP_NAME, bundle_dir
from ..storage import Storage

# Сторінки, доступні до вирішення питання з відновленням бази.
_ALLOWED_DURING_RECOVERY = {"main.recovery", "main.shutdown", "static"}
# Сторінки, доступні до прийняття правил використання.
_ALLOWED_BEFORE_TERMS = _ALLOWED_DURING_RECOVERY | {"main.terms"}


def create_app(storage: Storage) -> Flask:
    base = bundle_dir() / "oblik"
    app = Flask(
        __name__,
        template_folder=str(base / "templates"),
        static_folder=str(base / "static"),
    )
    app.secret_key = os.urandom(24)  # лише для повідомлень flash у межах сеансу
    app.config["STORAGE"] = storage
    app.config["SHUTDOWN"] = None  # run.py підставляє функцію зупинки сервера
    # Доступно й в імпортованих макросах (значок «?» з поясненням терміна).
    app.jinja_env.globals["term"] = help.term

    from .main import bp as main_bp
    from .documents import bp as documents_bp
    from .refs import bp as refs_bp
    from .reports import bp as reports_bp
    app.register_blueprint(main_bp)
    app.register_blueprint(refs_bp)
    app.register_blueprint(documents_bp)
    app.register_blueprint(reports_bp)

    @app.before_request
    def _require_recovery_decision():
        if storage.recovery and request.endpoint not in _ALLOWED_DURING_RECOVERY:
            return redirect(url_for("main.recovery"))
        # У тестах правила не питаємо, якщо тест не попросив цього явно.
        if app.config.get("TESTING") and not app.config.get("REQUIRE_TERMS"):
            return None
        if request.endpoint not in _ALLOWED_BEFORE_TERMS and not onboarding.terms_accepted(get_db()):
            return redirect(url_for("main.terms", next=request.full_path))
        return None

    @app.teardown_request
    def _finish_db(exc):
        conn: sqlite3.Connection | None = g.pop("db", None)
        if conn is None:
            return
        changed = False
        try:
            if exc is None:
                conn.commit()
                if conn.total_changes > 0:
                    with conn:
                        db.bump_change_seq(conn)
                    changed = True
            else:
                conn.rollback()
        finally:
            conn.close()
        if changed:
            storage.sync()

    @app.context_processor
    def _common():
        endpoint = request.endpoint or ""
        if endpoint.startswith("refs.") and request.view_args and "kind" in request.view_args:
            hint_key = f"refs.{request.view_args['kind']}"
        else:
            hint_key = endpoint
        return {"app_name": APP_NAME, "app_version": __version__, "app_copyright": __copyright__,
                "storage": storage, "page_hint": help.PAGE_HINTS.get(hint_key)}

    return app


def get_db() -> sqlite3.Connection:
    """З'єднання з робочою копією бази на час запиту.

    Після запиту зміни фіксуються, лічильник змін збільшується, і база
    одразу записується на флешку.
    """
    if "db" not in g:
        from flask import current_app
        storage: Storage = current_app.config["STORAGE"]
        g.db = db.connect(storage.work_db)
    return g.db
