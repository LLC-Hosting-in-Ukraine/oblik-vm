"""Шляхи та параметри запуску.

Усі дані користувача лежать поруч із програмою (на флешці):
    oblik.db         — база
    backups/         — автоматичні резервні копії
    templates_docx/  — шаблони документів Word
    output/          — згенеровані документи
    logs/            — журнал роботи програми
Робоча копія бази під час сеансу — у локальній папці користувача (див. storage.py).
"""
from __future__ import annotations

import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

APP_NAME = "Облік ВМ"
APP_DIR_NAME = "OblikVM"
DB_FILE = "oblik.db"
BACKUP_KEEP = 30
PREFERRED_PORT = 8740
SYNC_RETRY_SECONDS = 5


def is_frozen() -> bool:
    """Чи запущено зібраний .exe (PyInstaller)."""
    return getattr(sys, "frozen", False)


def bundle_dir() -> Path:
    """Ресурси, вшиті в програму (HTML, стилі, шаблони за замовчуванням)."""
    if is_frozen():
        return Path(sys._MEIPASS)  # noqa: SLF001 — так PyInstaller передає шлях
    return Path(__file__).resolve().parent.parent


def default_root() -> Path:
    """Папка з даними: поруч із .exe, а під час розробки — dev_data/."""
    env = os.environ.get("OBLIK_ROOT")
    if env:
        return Path(env).resolve()
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return bundle_dir() / "dev_data"


def default_local() -> Path:
    """Локальна папка на диску ПК для робочої копії бази."""
    env = os.environ.get("OBLIK_LOCAL")
    if env:
        return Path(env).resolve()
    if os.environ.get("LOCALAPPDATA"):  # Windows
        return Path(os.environ["LOCALAPPDATA"]) / APP_DIR_NAME
    if sys.platform == "darwin":  # macOS (для розробки)
        return Path.home() / "Library" / "Application Support" / APP_DIR_NAME
    xdg = os.environ.get("XDG_DATA_HOME")  # Linux
    if xdg:
        return Path(xdg) / APP_DIR_NAME
    try:
        return Path.home() / ".local" / "share" / APP_DIR_NAME
    except RuntimeError:  # домашню папку не визначено
        return Path(tempfile.gettempdir()) / APP_DIR_NAME


@dataclass(frozen=True)
class Paths:
    root: Path   # флешка (папка програми)
    local: Path  # локальна папка на ПК

    @classmethod
    def default(cls) -> "Paths":
        return cls(root=default_root(), local=default_local())

    @property
    def db(self) -> Path:
        return self.root / DB_FILE

    @property
    def backups(self) -> Path:
        return self.root / "backups"

    @property
    def templates_docx(self) -> Path:
        return self.root / "templates_docx"

    @property
    def output(self) -> Path:
        return self.root / "output"

    @property
    def logs(self) -> Path:
        return self.root / "logs"
