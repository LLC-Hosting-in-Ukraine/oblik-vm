"""Точка входу: бекап → робоча копія бази → сервер на 127.0.0.1 → браузер.

© 2026 Yevhenii Hosting by LLC Hosting in Ukraine
"""
from __future__ import annotations

import logging
import os
import socket
import sys
import threading
import webbrowser
from logging.handlers import RotatingFileHandler

from werkzeug.serving import make_server

from oblik import __copyright__, __version__, instance
from oblik.config import APP_NAME, PREFERRED_PORT, SYNC_RETRY_SECONDS, Paths
from oblik.storage import Storage
from oblik.web import create_app

log = logging.getLogger("oblik")


def setup_logging(paths: Paths) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    try:
        paths.logs.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(
            paths.logs / "oblik.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"))
    except OSError:
        pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )
    logging.getLogger("werkzeug").setLevel(logging.WARNING)


def bind_socket(port: int) -> socket.socket:
    """Порт тільки для нас. На Windows без SO_EXCLUSIVEADDRUSE інша програма (або друга
    копія) може «сісти» на той самий порт, і запити підуть не туди."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):  # Windows
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        sock.bind(("127.0.0.1", port))
        sock.listen(64)
    except OSError:
        sock.close()
        raise
    return sock


def start_server(app):
    try:
        sock = bind_socket(PREFERRED_PORT)
    except OSError:  # порт зайнятий — беремо будь-який вільний
        sock = bind_socket(0)
    server = make_server("127.0.0.1", sock.getsockname()[1], app, threaded=False, fd=sock.fileno())
    sock.close()  # сервер працює з власною копією дескриптора
    return server


def main() -> int:
    paths = Paths.default()
    setup_logging(paths)

    lock = instance.acquire(paths.local)
    if lock is None:
        url = instance.read_url(paths.local)
        print(f"{APP_NAME} вже запущено. Відкриваю вкладку: {url}")
        if url and not os.environ.get("OBLIK_NO_BROWSER"):
            webbrowser.open(url)
        return 0

    storage = Storage(paths)
    try:
        storage.open()
    except Exception:
        log.exception("Не вдалося відкрити базу")
        input("Помилка відкриття бази (див. вище). Натисніть Enter, щоб закрити...")
        return 1

    app = create_app(storage)
    server = start_server(app)
    url = f"http://127.0.0.1:{server.port}/"
    instance.write_info(paths.local, url)

    stop_event = threading.Event()

    def request_stop():
        stop_event.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    app.config["SHUTDOWN"] = request_stop

    def sync_retry_loop():
        while not stop_event.wait(SYNC_RETRY_SECONDS):
            storage.retry_sync_if_needed()

    threading.Thread(target=sync_retry_loop, daemon=True).start()

    print("=" * 60)
    print(f"  {APP_NAME} {__version__}")
    print(f"  {__copyright__}")
    print("-" * 60)
    print(f"  Програма працює: {url}")
    print(f"  База: {paths.db}")
    print("  Не закривайте це вікно під час роботи.")
    print("  Для виходу натисніть «Завершити роботу» в браузері.")
    print("=" * 60)
    if not os.environ.get("OBLIK_NO_BROWSER"):
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        storage.close()
        lock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
