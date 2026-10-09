"""Захист від повторного запуску: друга копія програми просто відкриває вкладку першої."""
from __future__ import annotations

import json
from pathlib import Path
from typing import IO

LOCK_FILE = "instance.lock"
INFO_FILE = "instance.json"


def acquire(folder: Path) -> IO | None:
    """Захопити блокування. None — програма вже запущена."""
    folder.mkdir(parents=True, exist_ok=True)
    handle = open(folder / LOCK_FILE, "a+")
    handle.seek(0)
    try:
        try:
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except ImportError:  # не Windows
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def write_info(folder: Path, url: str) -> None:
    (folder / INFO_FILE).write_text(json.dumps({"url": url}), encoding="utf-8")


def read_url(folder: Path) -> str | None:
    try:
        return json.loads((folder / INFO_FILE).read_text(encoding="utf-8"))["url"]
    except (OSError, ValueError, KeyError):
        return None
