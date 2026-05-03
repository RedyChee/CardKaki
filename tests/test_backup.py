import sqlite3

import pytest

from cardkaki.backup import run_backup, snapshot


def _make_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT);
        INSERT INTO users (id, name) VALUES (1, 'alice'), (2, 'bob');
        """
    )
    conn.commit()
    return conn


def test_snapshot_preserves_rows(tmp_path):
    src_path = tmp_path / "users.sqlite"
    out_dir = tmp_path / "backups"
    conn = _make_db(src_path)
    conn.close()

    snap = snapshot(src_path, out_dir)
    assert snap.exists()

    s = sqlite3.connect(snap)
    rows = s.execute("SELECT id, name FROM users ORDER BY id").fetchall()
    s.close()
    assert rows == [(1, "alice"), (2, "bob")]


def test_snapshot_works_with_open_source_connection(tmp_path):
    src_path = tmp_path / "users.sqlite"
    out_dir = tmp_path / "backups"
    conn = _make_db(src_path)  # leave connection open

    snap = snapshot(src_path, out_dir)
    assert snap.exists()

    s = sqlite3.connect(snap)
    n = s.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    s.close()
    conn.close()
    assert n == 2


def test_run_backup_no_r2_keeps_local(tmp_path, monkeypatch):
    # Strip any inherited R2 env vars
    for var in ("R2_ACCOUNT_ID", "R2_BUCKET", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        monkeypatch.delenv(var, raising=False)

    src_path = tmp_path / "users.sqlite"
    out_dir = tmp_path / "backups"
    conn = _make_db(src_path)
    conn.close()

    result = run_backup(src_path, out_dir)
    assert result["uploaded"] is False
    snaps = list(out_dir.glob("users-*.sqlite"))
    assert len(snaps) == 1, "local snapshot should remain when upload skipped"


def test_snapshot_creates_out_dir(tmp_path):
    src_path = tmp_path / "users.sqlite"
    out_dir = tmp_path / "deeply" / "nested" / "backups"
    conn = _make_db(src_path)
    conn.close()
    snap = snapshot(src_path, out_dir)
    assert snap.exists()
    assert snap.parent == out_dir
