"""Зберігання бази: флешка + робоча копія на локальному диску.

Як це працює:
  * основна база — <флешка>/oblik.db. Вона змінюється тільки атомарно:
    нова версія пишеться у oblik.db.tmp, а потім одним кроком замінює стару.
    Тому навіть якщо флешку висмикнути посеред запису, на ній лишиться
    ціла (стара або нова) база;
  * під час роботи програма працює з робочою копією на ПК:
    %LOCALAPPDATA%/OblikVM/<ідентифікатор бази>/work.db;
  * після кожної зміни робоча копія одразу записується на флешку.
    Якщо флешки немає — програма працює далі, а зміни допишуться,
    щойно флешка повернеться;
  * у state.json записано, яку версію (лічильник змін) востаннє вдалося
    записати на флешку. Якщо програма завершилась аварійно і на ПК лишились
    незбережені зміни — при наступному запуску буде запропоновано їх відновити;
  * при нормальному завершенні робоча копія з ПК видаляється.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import threading
import uuid
from datetime import datetime
from pathlib import Path

from . import db
from .config import BACKUP_KEEP, Paths

log = logging.getLogger(__name__)

WORK_DB = "work.db"
STATE_FILE = "state.json"
BACKUP_PREFIX = "oblik_"
ASIDE_PREFIX = "vidkladeno_"

# Ситуації, коли потрібне рішення користувача перед початком роботи.
RECOVERY_LOCAL_NEWER = "local_newer"  # на ПК є зміни, яких немає на флешці
RECOVERY_CONFLICT = "conflict"        # змінено і на ПК, і на флешці (на іншому ПК)


# --- Копіювання файлів бази -------------------------------------------------

def _ro_uri(path: Path) -> str:
    return path.resolve().as_uri() + "?mode=ro"


def _fsync(path: Path) -> None:
    fd = os.open(path, os.O_RDWR | getattr(os, "O_BINARY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def copy_db(src: Path, dst: Path) -> None:
    """Узгоджена копія бази (через backup API SQLite) з фіксацією на диск."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()
    source = sqlite3.connect(_ro_uri(src), uri=True)
    try:
        target = sqlite3.connect(dst)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()
    _fsync(dst)


def atomic_copy_db(src: Path, dst: Path) -> None:
    """Замінити dst копією src так, щоб dst завжди лишався цілим файлом."""
    tmp = dst.with_name(dst.name + ".tmp")
    copy_db(src, tmp)
    os.replace(tmp, dst)


def read_meta(path: Path) -> dict[str, str]:
    conn = sqlite3.connect(_ro_uri(path), uri=True)
    try:
        return dict(conn.execute("SELECT key, value FROM meta").fetchall())
    finally:
        conn.close()


def _stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d_%H-%M-%S")


def _unique(folder: Path, stem: str) -> Path:
    target = folder / f"{stem}.db"
    n = 1
    while target.exists():
        target = folder / f"{stem}_{n}.db"
        n += 1
    return target


# --- Резервні копії ---------------------------------------------------------

def make_backup(paths: Paths, keep: int = BACKUP_KEEP) -> Path | None:
    """Копія бази з флешки в backups/; зберігаються останні `keep` копій."""
    if not paths.db.exists():
        return None
    paths.backups.mkdir(parents=True, exist_ok=True)
    target = _unique(paths.backups, BACKUP_PREFIX + _stamp())
    copy_db(paths.db, target)
    rotate_backups(paths.backups, keep)
    return target


def rotate_backups(folder: Path, keep: int) -> None:
    # Імена містять дату й час, тому сортування за іменем = за часом.
    # Відкладені копії (vidkladeno_*) не видаляються автоматично.
    files = sorted(folder.glob(BACKUP_PREFIX + "*.db"))
    for old in files[:-keep] if keep > 0 else files:
        old.unlink()


# --- Сеанс роботи з базою ---------------------------------------------------

class Storage:
    def __init__(self, paths: Paths):
        self.paths = paths
        self.work_dir: Path | None = None
        self.recovery: str | None = None
        self.unsynced = False
        self.sync_error: str | None = None
        self.last_sync: datetime | None = None
        self.last_backup: Path | None = None
        self._lock = threading.Lock()

    @property
    def work_db(self) -> Path:
        assert self.work_dir is not None, "Storage.open() ще не викликано"
        return self.work_dir / WORK_DB

    @property
    def ready(self) -> bool:
        """Чи можна працювати з базою (немає невирішеного відновлення)."""
        return self.work_dir is not None and self.recovery is None

    # --- Відкриття ---

    def open(self) -> None:
        p = self.paths
        p.root.mkdir(parents=True, exist_ok=True)
        if not p.db.exists():
            self._create_new()
            return

        self.last_backup = make_backup(p)
        flash_meta = read_meta(p.db)
        self.work_dir = p.local / flash_meta["db_uuid"]

        state = self._read_state()
        if state is not None and self.work_db.exists():
            work_seq = int(read_meta(self.work_db)["change_seq"])
            synced = state["synced_seq"]
            if work_seq > synced:
                flash_seq = int(flash_meta["change_seq"])
                self.recovery = (
                    RECOVERY_LOCAL_NEWER if flash_seq == synced else RECOVERY_CONFLICT
                )
                log.warning(
                    "Знайдено незбережені зміни на ПК: робоча копія %s, на флешці %s, "
                    "востаннє записано %s → %s",
                    work_seq, flash_seq, synced, self.recovery,
                )
                return
        self._load_from_flash()

    def _create_new(self) -> None:
        uid = uuid.uuid4().hex
        self.work_dir = self.paths.local / uid
        self.work_dir.mkdir(parents=True, exist_ok=True)
        if self.work_db.exists():
            self.work_db.unlink()
        conn = db.connect(self.work_db)
        try:
            db.migrate(conn)
            db.init_meta(conn, uid)
        finally:
            conn.close()
        self._write_state(-1)
        if not self.sync():
            raise OSError(f"Не вдалося створити базу {self.paths.db}: {self.sync_error}")
        log.info("Створено нову базу %s", self.paths.db)

    def _load_from_flash(self) -> None:
        copy_db(self.paths.db, self.work_db)
        self._write_state(int(read_meta(self.work_db)["change_seq"]))
        self._migrate_work()

    def _migrate_work(self) -> None:
        conn = db.connect(self.work_db)
        try:
            applied = db.migrate(conn)
            if applied:
                with conn:
                    db.bump_change_seq(conn)
        finally:
            conn.close()
        if applied:
            log.info("Застосовано міграції схеми: %s", applied)
            self.sync()

    # --- Відновлення після аварійного завершення ---

    def resolve_recovery(self, keep_local: bool) -> None:
        """Рішення користувача: взяти зміни з ПК (True) чи версію з флешки (False).

        Версія, яку відкидаємо, не видаляється, а відкладається в backups/vidkladeno_*.
        """
        if self.recovery is None:
            return
        if keep_local:
            if self.recovery == RECOVERY_CONFLICT:
                self._set_aside(self.paths.db, "z_fleshky")
            self.recovery = None
            self._migrate_work()
            if not self.sync():
                raise OSError(f"Не вдалося записати базу на флешку: {self.sync_error}")
        else:
            self._set_aside(self.work_db, "z_pk")
            self.recovery = None
            self._load_from_flash()

    def _set_aside(self, src: Path, label: str) -> Path:
        self.paths.backups.mkdir(parents=True, exist_ok=True)
        target = _unique(self.paths.backups, f"{ASIDE_PREFIX}{label}_{_stamp()}")
        copy_db(src, target)
        log.warning("Версію бази відкладено: %s", target)
        return target

    # --- Синхронізація ---

    def sync(self) -> bool:
        """Записати робочу копію на флешку. False — флешка недоступна."""
        with self._lock:
            try:
                if not self.paths.root.is_dir():
                    raise FileNotFoundError(f"Папку {self.paths.root} не знайдено")
                seq = int(read_meta(self.work_db)["change_seq"])
                atomic_copy_db(self.work_db, self.paths.db)
                self._write_state(seq)
            except (OSError, sqlite3.Error) as exc:
                if not self.unsynced:
                    log.warning("Не вдалося записати базу на флешку: %s", exc)
                self.unsynced = True
                self.sync_error = str(exc)
                return False
            if self.unsynced:
                log.info("Базу знову записано на флешку")
            self.unsynced = False
            self.sync_error = None
            self.last_sync = datetime.now()
            return True

    def retry_sync_if_needed(self) -> None:
        if self.ready and self.unsynced:
            self.sync()

    # --- Завершення ---

    def close(self) -> bool:
        """Записати базу на флешку і прибрати робочу копію з ПК.

        Якщо флешки немає — робоча копія лишається, щоб нічого не втратити.
        """
        if not self.ready:
            return False
        if not self.sync():
            log.warning("Флешка недоступна — робочу копію залишено на ПК: %s", self.work_dir)
            return False
        shutil.rmtree(self.work_dir, ignore_errors=True)
        log.info("Сеанс завершено, робочу копію з ПК видалено")
        return True

    # --- state.json ---

    def _state_path(self) -> Path:
        assert self.work_dir is not None
        return self.work_dir / STATE_FILE

    def _read_state(self) -> dict | None:
        try:
            return json.loads(self._state_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _write_state(self, synced_seq: int) -> None:
        path = self._state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(STATE_FILE + ".tmp")
        tmp.write_text(
            json.dumps({"synced_seq": synced_seq, "root": str(self.paths.root)}),
            encoding="utf-8",
        )
        os.replace(tmp, path)
