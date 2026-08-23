from __future__ import annotations

import json
from pathlib import Path

import pytest

from gateway.session_context import clear_session_vars, set_session_vars
from hermes_cli import kanban_db as kb


@pytest.fixture
def owner_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "researcher")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    tokens = set_session_vars(
        source="tui",
        session_key="bot-chat-key",
        session_id="session-123",
        profile="researcher",
    )
    try:
        yield home
    finally:
        clear_session_vars(tokens)


def test_owner_surface_derives_authority_and_refuses_escape(owner_home):
    from tools import kanban_tools as kt
    from toolsets import resolve_toolset

    assert set(resolve_toolset("kanban-owner")) == {
        "kanban_create_self_owned",
        "kanban_show_self_owned",
        "kanban_comment_self_owned",
        "kanban_block_self_owned",
    }
    schema = kt.KANBAN_CREATE_SELF_OWNED_SCHEMA["parameters"]
    assert set(schema["properties"]) == {
        "title",
        "body",
        "acceptance",
        "mission_key",
        "max_runtime_seconds",
        "goal_max_turns",
        "max_retries",
    }

    escaped = json.loads(
        kt._handle_create_self_owned(
            {
                "title": "escape",
                "acceptance": "must be refused",
                "mission_key": "escape-1",
                "assignee": "ops",
            },
            session_id="session-123",
        )
    )
    assert "unsupported authority field" in escaped["error"]

    created = json.loads(
        kt._handle_create_self_owned(
            {
                "title": "bounded mission",
                "body": "Do the exact slice.",
                "acceptance": "Targeted tests pass.",
                "mission_key": "mission-1",
                "max_runtime_seconds": 600,
                "goal_max_turns": 4,
                "max_retries": 1,
            },
            session_id="session-123",
        )
    )
    assert created["ok"] is True

    with kb.connect_closing() as conn:
        owned = kb.get_task(conn, created["task_id"])
        foreign = kb.create_task(
            conn,
            title="foreign",
            assignee="researcher",
            created_by="researcher",
            session_id="other-session",
        )
    assert owned is not None
    assert owned.assignee == "researcher"
    assert owned.created_by == "researcher"
    assert owned.session_id == "session-123"

    shown = json.loads(
        kt._handle_show_self_owned(
            {"task_id": created["task_id"]}, session_id="session-123"
        )
    )
    assert shown["task"]["id"] == created["task_id"]

    refused_show = json.loads(
        kt._handle_show_self_owned(
            {"task_id": foreign}, session_id="session-123"
        )
    )
    refused_comment = json.loads(
        kt._handle_comment_self_owned(
            {"task_id": foreign, "body": "adopt it"},
            session_id="session-123",
        )
    )
    assert "not owned by this exact creator session" in refused_show["error"]
    assert "not owned by this exact creator session" in refused_comment["error"]


def test_owner_typed_block_refuses_healthy_worker_and_stops_route(
    owner_home, monkeypatch
):
    from tools import kanban_tools as kt

    created = json.loads(
        kt._handle_create_self_owned(
            {
                "title": "guarded block",
                "acceptance": "Only block after the worker is not healthy.",
                "mission_key": "block-guard-1",
                "max_runtime_seconds": 600,
                "goal_max_turns": 4,
                "max_retries": 1,
            },
            session_id="session-123",
        )
    )
    task_id = created["task_id"]
    now = int(kb.time.time())
    with kb.connect_closing() as conn:
        with kb.write_txn(conn):
            run = conn.execute(
                "INSERT INTO task_runs (task_id, profile, status, claim_lock, "
                "claim_expires, worker_pid, max_runtime_seconds, "
                "last_heartbeat_at, started_at) VALUES "
                "(?, 'researcher', 'running', 'host:5151', ?, 5151, 600, ?, ?)",
                (task_id, now + 120, now, now - 10),
            )
            conn.execute(
                "UPDATE tasks SET status = 'running', claim_lock = 'host:5151', "
                "claim_expires = ?, worker_pid = 5151, last_heartbeat_at = ?, "
                "started_at = ?, current_run_id = ? WHERE id = ?",
                (now + 120, now, now - 10, run.lastrowid, task_id),
            )
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: pid == 5151)

    refused = json.loads(
        kt._handle_block_self_owned(
            {
                "task_id": task_id,
                "reason": "Do not race the worker",
                "kind": "needs_input",
            },
            session_id="session-123",
        )
    )
    assert "same-profile worker is healthy" in refused["error"]

    with kb.connect_closing() as conn:
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_runs SET status = 'crashed', ended_at = ? "
                "WHERE task_id = ?",
                (now, task_id),
            )
            conn.execute(
                "UPDATE tasks SET status = 'ready', claim_lock = NULL, "
                "claim_expires = NULL, worker_pid = NULL, current_run_id = NULL "
                "WHERE id = ?",
                (task_id,),
            )

    blocked = json.loads(
        kt._handle_block_self_owned(
            {
                "task_id": task_id,
                "reason": "Need explicit owner input",
                "kind": "needs_input",
            },
            session_id="session-123",
        )
    )
    assert blocked["ok"] is True
    with kb.connect_closing() as conn:
        task = kb.get_task(conn, task_id)
        route = kb.list_notify_subs(conn, task_id)[0]
        assert task is not None and task.status == "blocked"
        assert task.block_kind == "needs_input"
        assert route["supervision_deadline_at"] is None


def test_owner_typed_block_rolls_back_when_terminal_receipt_fails(
    owner_home, monkeypatch
):
    from tools import kanban_tools as kt

    created = json.loads(
        kt._handle_create_self_owned(
            {
                "title": "atomic self block",
                "acceptance": "A failed terminal receipt leaves the task runnable.",
                "mission_key": "atomic-block-rollback",
                "max_runtime_seconds": 600,
                "goal_max_turns": 4,
                "max_retries": 1,
            },
            session_id="session-123",
        )
    )
    task_id = created["task_id"]
    monkeypatch.setattr(kb, "supervision_worker_is_healthy", lambda *a, **k: False)
    monkeypatch.setattr(
        kb, "record_supervision_terminal_block", lambda *a, **k: False
    )

    result = json.loads(
        kt._handle_block_self_owned(
            {
                "task_id": task_id,
                "reason": "Must roll back",
                "kind": "needs_input",
            },
            session_id="session-123",
        )
    )
    assert "terminal supervision receipt failed" in result["error"]
    with kb.connect_closing() as conn:
        task = kb.get_task(conn, task_id)
        route = kb.list_notify_subs(conn, task_id)[0]
        assert task is not None and task.status == "ready"
        assert route["supervision_deadline_at"] is not None
        assert not any(
            event.kind == "supervision_receipt"
            for event in kb.list_events(conn, task_id)
        )
