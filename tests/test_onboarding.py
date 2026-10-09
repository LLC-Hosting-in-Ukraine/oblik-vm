"""Перший запуск: правила використання і кроки «з чого почати»."""
from __future__ import annotations

from oblik import db, onboarding
from oblik.config import Paths
from oblik.storage import Storage
from oblik.web import create_app
from tests import test_posting
from tests.test_posting import World


def make(tmp_path, require_terms=True):
    storage = Storage(Paths(root=tmp_path / "Флешка", local=tmp_path / "ПК"))
    storage.open()
    app = create_app(storage)
    app.config.update(TESTING=True, REQUIRE_TERMS=require_terms)
    return storage, app.test_client()


def test_terms_asked_until_accepted(tmp_path):
    storage, client = make(tmp_path)
    resp = client.get("/documents")
    assert resp.status_code == 302 and "/terms" in resp.headers["Location"]
    html = client.get("/terms").get_data(as_text=True)
    assert "не проходила жодних експертиз" in html and "127.0.0.1" in html
    assert "Для службового користування" in html
    # Без позначки — не приймається.
    resp = client.post("/terms", data={"next": "/documents"})
    assert "Поставте позначку" in resp.get_data(as_text=True)
    assert client.get("/documents").status_code == 302
    # З позначкою — повертає туди, куди йшли, і більше не питає.
    resp = client.post("/terms", data={"agree": "1", "next": "/documents?year=2026"})
    assert resp.headers["Location"].endswith("/documents?year=2026")
    assert client.get("/documents").status_code == 200
    # Чужу адресу як «next» не приймаємо.
    conn = db.connect(storage.work_db)
    assert onboarding.terms_accepted(conn)["version"] == onboarding.TERMS_VERSION
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'accept'").fetchone()[0] == 1
    conn.close()
    # Сторінку правил видно й після прийняття (посилання внизу кожної сторінки).
    html = client.get("/").get_data(as_text=True)
    assert "Правила використання" in html and "нікуди не передаються" in html
    assert "Правила прийнято" in client.get("/terms").get_data(as_text=True)


def test_terms_next_only_local(tmp_path):
    _, client = make(tmp_path)
    resp = client.post("/terms", data={"agree": "1", "next": "//evil.example/"})
    assert resp.headers["Location"].endswith("/start")


def test_static_and_shutdown_before_terms(tmp_path):
    _, client = make(tmp_path)
    assert client.get("/static/style.css").status_code == 200
    assert client.post("/shutdown").status_code == 200


def test_steps_follow_database(tmp_path):
    storage, client = make(tmp_path, require_terms=False)
    conn = db.connect(storage.work_db)
    assert not any(s.done for s in onboarding.steps(conn))
    world = World(conn)
    test_posting.W = world
    done = {s.code for s in onboarding.steps(conn) if s.done}
    assert done == {"persons", "units", "nomenclature"}
    assert not onboarding.is_started(conn)
    world.doc("opening", world.opening, world.wh, [test_posting.cable(10)])
    conn.commit()
    assert "opening" in {s.code for s in onboarding.steps(conn) if s.done}
    conn.close()
    html = client.get("/start").get_data(as_text=True)
    assert "Початкові залишки" in html and "частина в частині" in html
    assert "З чого почати" in client.get("/").get_data(as_text=True)
    assert "Початкових залишків ще немає" not in client.get("/documents/new").get_data(as_text=True)
