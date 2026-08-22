import json

import pytest

from hermes_state import SessionDB
from hermes_state_worker_visibility import migrate_worker_session_visibility


@pytest.fixture
def db(tmp_path):
    database = SessionDB(tmp_path / "state.db")
    try:
        yield database
    finally:
        database.close()


def _seed(db, sid, *, source="cli", hidden=0, cwd=None, model_config=None, parent=None):
    db.create_session(
        sid,
        source=source,
        cwd=cwd,
        model_config=model_config,
        parent_session_id=parent,
        hidden=hidden,
    )
    # Historical fixtures predate born-hidden creation, so force their prior state.
    db._conn.execute("UPDATE sessions SET hidden = ? WHERE id = ?", (hidden, sid))
    db._conn.commit()


def test_migration_dry_run_apply_rerun_and_rollback(db, tmp_path):
    workspaces = tmp_path / "kanban" / "workspaces"
    _seed(db, "user", source="desktop")
    _seed(db, "bot", source="desktop")
    db.set_session_title("bot", "Bot Chat")
    _seed(db, "subagent", source="subagent")
    _seed(db, "kanban", source="kanban")
    _seed(db, "tool", source="tool")
    _seed(db, "cron", source="cron")
    _seed(
        db,
        "legacy-delegate",
        source="cli",
        model_config={"_delegate_from": "deleted-parent"},
    )
    _seed(db, "legacy-kanban", source="cli", cwd=str(workspaces / "t_deadbeef"))

    dry_run = migrate_worker_session_visibility(
        db, workspaces_roots=[workspaces], dry_run=True
    )
    assert dry_run["changed_ids"] == []
    assert set(dry_run["candidate_ids"]) == {
        "subagent",
        "kanban",
        "tool",
        "cron",
        "legacy-delegate",
        "legacy-kanban",
    }
    assert db.get_session("subagent")["hidden"] == 0

    applied = migrate_worker_session_visibility(
        db, workspaces_roots=[workspaces], dry_run=False
    )
    assert set(applied["changed_ids"]) == set(dry_run["candidate_ids"])
    assert all(db.get_session(sid)["hidden"] == 1 for sid in applied["changed_ids"])
    assert db.get_session("user")["hidden"] == 0
    assert db.get_session("bot")["hidden"] == 0

    rerun = migrate_worker_session_visibility(
        db, workspaces_roots=[workspaces], dry_run=False
    )
    assert rerun["changed_ids"] == []

    rolled_back = migrate_worker_session_visibility(
        db, rollback_receipt=applied["receipt"]
    )
    assert set(rolled_back["changed_ids"]) == set(applied["changed_ids"])
    assert all(db.get_session(sid)["hidden"] == 0 for sid in applied["changed_ids"])
    assert db.get_session("user")["hidden"] == 0
    assert db.get_session("bot")["hidden"] == 0


def test_migration_reports_ambiguous_legacy_orphan_without_mutating_it(db):
    # Simulate a pre-FK historical row whose parent was removed by an older
    # build. Current SessionDB correctly refuses to create this shape.
    db._conn.execute("PRAGMA foreign_keys = OFF")
    _seed(db, "orphan", source="cli", parent="missing-parent")
    db._conn.execute("PRAGMA foreign_keys = ON")

    report = migrate_worker_session_visibility(db, dry_run=False)

    assert report["legacy_orphan_ids"] == ["orphan"]
    assert report["changed_ids"] == []
    assert db.get_session("orphan")["hidden"] == 0


def test_rollback_rejects_receipt_for_another_database(db, tmp_path):
    receipt = {
        "schema": "worker-session-visibility/v1",
        "db_path": str(tmp_path / "other.db"),
        "changes": [{"id": "worker", "hidden": 0}],
    }

    with pytest.raises(ValueError, match="different database"):
        migrate_worker_session_visibility(db, rollback_receipt=receipt)


def test_receipt_is_json_serializable(db):
    _seed(db, "worker", source="cron")

    report = migrate_worker_session_visibility(db, dry_run=False)

    assert json.loads(json.dumps(report["receipt"])) == report["receipt"]