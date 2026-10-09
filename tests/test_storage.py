"""Сценарії роботи з флешкою: бекапи, висмикування, аварійне завершення, конфлікт."""
from __future__ import annotations

import os
import sqlite3

import pytest

from oblik import db
from oblik.config import Paths
from oblik.storage import (
    RECOVERY_CONFLICT,
    RECOVERY_LOCAL_NEWER,
    Storage,
    atomic_copy_db,
    make_backup,
    read_meta,
)


@pytest.fixture
def paths(tmp_path):
    # Кирилиця й пробіли в шляху — як у реальному житті.
    return Paths(root=tmp_path / "Флешка D" / "Облік", local=tmp_path / "ПК локально")


def write_setting(storage: Storage, key: str, value: str) -> None:
    """Імітація зміни з веб-інтерфейсу: запис + лічильник змін + синхронізація."""
    conn = db.connect(storage.work_db)
    with conn:
        conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)", (key, value))
        db.bump_change_seq(conn)
    conn.close()
    storage.sync()


def setting_in(path, key):
    conn = sqlite3.connect(path)
    try:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def yank(paths: Paths):
    """Висмикнути флешку: папка з даними зникає."""
    hidden = paths.root.with_name(paths.root.name + "_вийнято")
    os.rename(paths.root, hidden)
    return hidden


def reinsert(paths: Paths, hidden):
    os.rename(hidden, paths.root)


def test_first_start_creates_db_on_flash(paths):
    s = Storage(paths)
    s.open()
    assert paths.db.exists()
    assert s.last_backup is None
    meta = read_meta(paths.db)
    assert meta["change_seq"] == "0"
    assert s.work_db.parent.name == meta["db_uuid"]


def test_change_is_written_to_flash_immediately(paths):
    s = Storage(paths)
    s.open()
    write_setting(s, "unit", "А0000")
    assert setting_in(paths.db, "unit") == "А0000"
    assert not paths.db.with_name(paths.db.name + ".tmp").exists()


def test_backup_on_start_and_rotation(paths):
    Storage(paths).open()
    s = Storage(paths)
    s.open()
    assert s.last_backup is not None and s.last_backup.exists()

    for i in range(40):
        (paths.backups / f"oblik_2000-01-01_00-00-{i:02d}.db").write_bytes(b"x")
    make_backup(paths, keep=30)
    files = sorted(paths.backups.glob("oblik_*.db"))
    assert len(files) == 30
    # Лишились найновіші: найстаріші штучні копії видалено.
    assert not (paths.backups / "oblik_2000-01-01_00-00-00.db").exists()


def test_clean_close_removes_local_copy(paths):
    s = Storage(paths)
    s.open()
    write_setting(s, "unit", "А0000")
    assert s.close()
    assert not s.work_dir.exists()

    s2 = Storage(paths)
    s2.open()
    assert s2.recovery is None
    assert setting_in(s2.work_db, "unit") == "А0000"


def test_yank_keeps_working_and_syncs_after_reinsert(paths):
    s = Storage(paths)
    s.open()
    hidden = yank(paths)

    write_setting(s, "unit", "А1111")
    assert s.unsynced
    assert setting_in(s.work_db, "unit") == "А1111"

    reinsert(paths, hidden)
    s.retry_sync_if_needed()
    assert not s.unsynced
    assert setting_in(paths.db, "unit") == "А1111"


def test_crash_while_flash_out_offers_recovery(paths):
    s = Storage(paths)
    s.open()
    write_setting(s, "unit", "старе")
    hidden = yank(paths)
    write_setting(s, "unit", "нове")  # не потрапило на флешку
    # Аварійне завершення: close() не викликається.
    reinsert(paths, hidden)

    s2 = Storage(paths)
    s2.open()
    assert s2.recovery == RECOVERY_LOCAL_NEWER
    assert not s2.ready

    s2.resolve_recovery(keep_local=True)
    assert s2.ready
    assert setting_in(paths.db, "unit") == "нове"


def test_crash_without_pending_changes_needs_no_recovery(paths):
    s = Storage(paths)
    s.open()
    write_setting(s, "unit", "А2222")  # записано на флешку
    # Аварійне завершення: робоча копія лишилась, але вона не новіша за флешку.
    s2 = Storage(paths)
    s2.open()
    assert s2.recovery is None
    assert setting_in(s2.work_db, "unit") == "А2222"


def test_conflict_keeps_both_versions(paths):
    s = Storage(paths)
    s.open()
    hidden = yank(paths)
    write_setting(s, "unit", "з ПК")
    reinsert(paths, hidden)

    # Тим часом на іншому ПК з цією ж флешкою теж працювали.
    other = Storage(Paths(root=paths.root, local=paths.root.parent / "інший ПК"))
    other.open()
    write_setting(other, "unit", "з іншого ПК")
    other.close()

    s3 = Storage(paths)
    s3.open()
    assert s3.recovery == RECOVERY_CONFLICT

    s3.resolve_recovery(keep_local=False)
    assert setting_in(paths.db, "unit") == "з іншого ПК"
    aside = list(paths.backups.glob("vidkladeno_z_pk_*.db"))
    assert len(aside) == 1 and setting_in(aside[0], "unit") == "з ПК"


def test_atomic_copy_leaves_old_file_if_copy_fails(paths, tmp_path):
    s = Storage(paths)
    s.open()
    write_setting(s, "unit", "ціла")
    broken = tmp_path / "не база.db"
    broken.write_bytes(b"not a sqlite file")
    with pytest.raises(sqlite3.DatabaseError):
        atomic_copy_db(broken, paths.db)
    assert setting_in(paths.db, "unit") == "ціла"


def test_newer_schema_is_refused(paths):
    s = Storage(paths)
    s.open()
    conn = sqlite3.connect(s.work_db)
    conn.execute("PRAGMA user_version = 999")
    conn.commit()
    conn.close()
    s.sync()
    s.close()

    with pytest.raises(db.NewerDatabaseError):
        Storage(paths).open()
