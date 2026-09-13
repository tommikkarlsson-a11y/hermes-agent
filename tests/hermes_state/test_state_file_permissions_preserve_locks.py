"""Permission tightening must preserve a live SQLite writer's POSIX locks."""

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_state import SessionDB, _secure_state_db_files
from tests.hermes_state._wal_generation_harness import pin_wal


def _check_permission_and_lock_contract(tmp_path, monkeypatch):
    pin_wal(monkeypatch)
    path = tmp_path / "state.db"
    writer = SessionDB(db_path=path)
    try:
        writer.create_session("permission-probe", "cli")
        writer.append_message("permission-probe", role="user", content="before peer")
        sides = [Path(str(path) + suffix) for suffix in ("-wal", "-shm")]
        identities = [(p.stat().st_dev, p.stat().st_ino) for p in sides]
        for p in (path, *sides):
            p.chmod(0o644)
        # Both constructor paths may encounter an inode already held in this process.
        _secure_state_db_files(path, create_main=True)
        _secure_state_db_files(path)
        assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in (path, *sides))

        # A normal second process closes its connection. It must not mistake the
        # first writer for dead and delete the WAL/SHM under that live connection.
        peer = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sqlite3,sys; "
                "c=sqlite3.connect(sys.argv[1]); "
                "assert c.execute('SELECT count(*) FROM messages').fetchone()[0] == 1; c.close()",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert peer.returncode == 0, peer.stderr
        assert all(p.exists() for p in sides), (
            "peer closed a live writer's WAL generation"
        )
        assert [(p.stat().st_dev, p.stat().st_ino) for p in sides] == identities
        writer.append_message(
            "permission-probe", role="assistant", content="after peer"
        )
        assert [m["content"] for m in writer.get_messages("permission-probe")] == [
            "before peer",
            "after peer",
        ]
    finally:
        writer.close()

    # Preserve the privacy boundary as well as the locks: creation is private,
    # existing files are tightened, and no state filename follows a symlink.
    fresh = tmp_path / "fresh.db"
    old_umask = os.umask(0o022)
    try:
        _secure_state_db_files(fresh, create_main=True)
    finally:
        os.umask(old_umask)
    assert stat.S_IMODE(fresh.stat().st_mode) == 0o600
    target = tmp_path / "unrelated"
    target.write_text("synthetic")
    target.chmod(0o644)
    for index, suffix in enumerate(("", "-wal", "-shm")):
        base = tmp_path / (f"symlink-{index}.db")
        Path(str(base) + suffix).symlink_to(target)
        with pytest.raises(OSError):
            _secure_state_db_files(base, create_main=True)
        assert stat.S_IMODE(target.stat().st_mode) == 0o644


@pytest.mark.macos_only
def test_state_permissions_preserve_live_wal_on_macos(tmp_path, monkeypatch):
    _check_permission_and_lock_contract(tmp_path, monkeypatch)


@pytest.mark.linux_only
def test_state_permissions_preserve_live_wal_on_linux(tmp_path, monkeypatch):
    _check_permission_and_lock_contract(tmp_path, monkeypatch)
