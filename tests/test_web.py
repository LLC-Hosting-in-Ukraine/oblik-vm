"""Перевірка веб-частини кроку 0: головна, відновлення, завершення."""
from __future__ import annotations

import os

import pytest

from oblik.config import Paths
from oblik.storage import Storage
from oblik.web import create_app


@pytest.fixture
def paths(tmp_path):
    return Paths(root=tmp_path / "Флешка", local=tmp_path / "ПК")


def make_client(paths):
    storage = Storage(paths)
    storage.open()
    app = create_app(storage)
    app.config["TESTING"] = True
    return storage, app.test_client()


def test_index_shows_db_status(paths):
    _, client = make_client(paths)
    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Технічна інформація" in html
    assert str(paths.db) in html


def test_recovery_redirect_and_resolve(paths):
    storage, _ = make_client(paths)
    hidden = paths.root.with_name("вийнято")
    os.rename(paths.root, hidden)
    from tests.test_storage import write_setting
    write_setting(storage, "unit", "з ПК")
    os.rename(hidden, paths.root)

    storage2, client = make_client(paths)
    assert client.get("/").headers["Location"].endswith("/recovery")
    resp = client.post("/recovery", data={"choice": "local"})
    assert resp.status_code == 302
    assert storage2.ready
    assert client.get("/").status_code == 200


def test_reference_pages_crud(paths):
    storage, client = make_client(paths)
    assert client.get("/refs").status_code == 200
    for kind in ["units", "persons", "counterparties", "services", "nomenclature", "items"]:
        assert client.get(f"/refs/{kind}").status_code == 200, kind
        assert client.get(f"/refs/{kind}/new").status_code == 200, kind

    resp = client.post("/refs/persons/new", data={
        "last_name": "Мельник", "first_name": "Олена", "rank": "сержант", "roles": ["mvo"]})
    assert resp.status_code == 302
    html = client.get("/refs/persons?q=мельн").get_data(as_text=True)
    assert "Мельник" in html and "МВО" in html

    # Помилка валідації — форма повертається з повідомленням.
    resp = client.post("/refs/persons/new", data={"last_name": ""})
    assert resp.status_code == 200
    assert "Обов&#39;язкове поле" in resp.get_data(as_text=True)

    # Зміни записано на флешку.
    from tests.test_storage import setting_in  # noqa: F401
    import sqlite3
    c = sqlite3.connect(paths.db)
    assert c.execute("SELECT last_name FROM persons").fetchall() == [("Мельник",)]
    c.close()

    assert client.get("/refs/persons/1").status_code == 200
    assert "Створено" in client.get("/audit").get_data(as_text=True)


def test_settings_page(paths):
    _, client = make_client(paths)
    html = client.get("/").get_data(as_text=True)
    assert "З чого почати" in html and '<li class="done">' not in html
    resp = client.post("/settings", data={
        "unit_name": "Військова частина А0000", "unit_code": "А0000", "operator_name": "Тест Т."})
    assert resp.status_code == 302
    # Крок «Реквізити частини» позначено виконаним.
    assert '<li class="done">' in client.get("/").get_data(as_text=True)


def test_footer_has_copyright(paths):
    _, client = make_client(paths)
    assert "Yevhenii Hosting by LLC Hosting in Ukraine" in client.get("/").get_data(as_text=True)


def test_shutdown_calls_stop(paths):
    storage = Storage(paths)
    storage.open()
    app = create_app(storage)
    called = []
    app.config["SHUTDOWN"] = lambda: called.append(True)
    resp = app.test_client().post("/shutdown")
    assert resp.status_code == 200 and called == [True]


def test_second_server_gets_other_port():
    """Дві копії не можуть слухати один порт (на Windows — SO_EXCLUSIVEADDRUSE)."""
    from flask import Flask

    import run
    a = run.start_server(Flask("a"))
    b = run.start_server(Flask("b"))
    try:
        assert a.port != b.port
    finally:
        a.server_close()
        b.server_close()


def test_normative_docs_in_help(paths):
    from oblik import help
    _, client = make_client(paths)
    page = client.get("/help").get_data(as_text=True)
    assert "Нормативні документи" in page and "zakon.rada.gov.ua/laws/show/z1192-17" in page
    for n in help.NORMATIVE:
        resp = client.get(f"/help/normative/{n.file}")
        assert resp.status_code == 200 and len(resp.data) > 10000, n.file
    assert client.get("/help/normative/..%2Fsecret.db").status_code == 404
    assert client.get("/help/normative/other.pdf").status_code == 404


def test_about_has_feedback(paths):
    from oblik.config import PROJECT_URL
    _, client = make_client(paths)
    page = client.get("/about").get_data(as_text=True)
    assert "Відгуки й пропозиції" in page and PROJECT_URL in page and "Не публікуйте реальних даних" in page
