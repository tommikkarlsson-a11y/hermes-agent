"""Controller-only exact-run false-completion invalidation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb


_WORKER_IDENTITY_ENV = (
    "HERMES_KANBAN_TASK",
    "HERMES_KANBAN_RUN_ID",
    "HERMES_KANBAN_CLAIM_LOCK",
    "HERMES_KANBAN_WORKSPACE",
)


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for name in _WORKER_IDENTITY_ENV:
        monkeypatch.delenv(name, raising=False)
    kb.init_db()
    return home


def _complete_with_run(conn, *, title: str = "completed task") -> tuple[str, int]:
    task_id = kb.create_task(conn, title=title, assignee="builder")
    claimed = kb.claim_task(conn, task_id)
    assert claimed is not None
    assert kb.complete_task(
        conn,
        task_id,
        result="accepted result",
        summary="original summary",
        metadata={"proof": "preserve me"},
        expected_run_id=claimed.current_run_id,
    )
    run = kb.latest_run(conn, task_id)
    assert run is not None
    return task_id, run.id


def _snapshot(conn, task_id: str) -> dict:
    task = conn.execute(
        "SELECT * FROM tasks WHERE id = ?", (task_id,),
    ).fetchone()
    runs = conn.execute(
        "SELECT * FROM task_runs WHERE task_id = ? ORDER BY id", (task_id,),
    ).fetchall()
    events = conn.execute(
        "SELECT * FROM task_events WHERE task_id = ? ORDER BY id", (task_id,),
    ).fetchall()
    comments = conn.execute(
        "SELECT * FROM task_comments WHERE task_id = ? ORDER BY id", (task_id,),
    ).fetchall()
    return {
        "task": dict(task) if task else None,
        "runs": [dict(row) for row in runs],
        "events": [dict(row) for row in events],
        "comments": [dict(row) for row in comments],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hermes")
    sub = parser.add_subparsers(dest="command")
    kc.build_parser(sub)
    return parser


def test_exact_run_invalidation_clears_acceptance_but_preserves_history(
    kanban_home,
):
    secret = "sk-proj-1234567890abcdefghijklmnopqrstuvwxyz"
    with kb.connect_closing() as conn:
        task_id, run_id = _complete_with_run(conn)

        result = kb.invalidate_completed_task(
            conn,
            task_id,
            superseded_run_id=run_id,
            reason=f"false completion; leaked credential {secret}",
            author="controller",
        )

        task = kb.get_task(conn, task_id)
        run = kb.get_run(conn, run_id)
        assert task is not None and run is not None
        assert task.status == "blocked"
        assert task.result is None
        assert task.completed_at is None
        assert task.current_run_id is None
        assert task.claim_lock is None
        assert task.claim_expires is None
        assert task.worker_pid is None
        assert task.last_heartbeat_at is None
        assert task.block_kind == "needs_input"
        assert task.block_recurrences == 0
        assert run.status == "invalidated"
        assert run.outcome == "invalidated"
        assert run.summary == "original summary"
        assert run.metadata == {"proof": "preserve me"}

        invalidated = [
            event for event in kb.list_events(conn, task_id)
            if event.kind == "completion_invalidated"
        ]
        assert len(invalidated) == 1
        payload = invalidated[0].payload
        assert payload is not None
        assert invalidated[0].run_id == run_id
        assert payload["superseded_run_id"] == run_id
        assert payload["prior_status"] == "done"
        assert payload["new_status"] == "blocked"
        assert payload["author"] == "controller"
        assert secret not in payload["reason"]
        assert payload["reason"] != (
            f"false completion; leaked credential {secret}"
        )

        blocked = [
            event for event in kb.list_events(conn, task_id)
            if event.kind == "blocked"
        ]
        assert len(blocked) == 1
        assert blocked[0].run_id == run_id
        assert blocked[0].payload == {
            "reason": payload["reason"],
            "kind": "needs_input",
            "recurrences": 0,
            "source_status": "ready",
            "superseded_run_id": run_id,
        }
        assert kb.recompute_ready(conn) == 0
        sticky_task = kb.get_task(conn, task_id)
        assert sticky_task is not None and sticky_task.status == "blocked"

        comments = kb.list_comments(conn, task_id)
        operator_comment = comments[-1]
        assert operator_comment.author == "controller"
        assert str(run_id) in operator_comment.body
        assert secret not in operator_comment.body
        assert payload["reason"] in operator_comment.body

        assert result["task_id"] == task_id
        assert result["superseded_run_id"] == run_id
        assert result["status"] == "blocked"
        assert result["run_status"] == "invalidated"
        assert result["run_outcome"] == "invalidated"
        assert result["reason"] == payload["reason"]
        assert result["descendants"] == []
        assert result["terminations"] == []


def test_invalid_requests_make_zero_mutation(kanban_home, monkeypatch):
    with kb.connect_closing() as conn:
        task_id, run_id = _complete_with_run(conn, title="target")
        other_id, other_run_id = _complete_with_run(conn, title="other")

        before = _snapshot(conn, task_id)
        with pytest.raises(ValueError, match="does not belong"):
            kb.invalidate_completed_task(
                conn,
                task_id,
                superseded_run_id=other_run_id,
                reason="wrong task run",
                author="controller",
            )
        assert _snapshot(conn, task_id) == before

        conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, started_at, ended_at, "
            "outcome, summary, metadata) VALUES (?, ?, 'done', ?, ?, 'completed', ?, ?)",
            (task_id, "builder", 9999999998, 9999999999, "newer", '{"v": 2}'),
        )
        conn.commit()
        before = _snapshot(conn, task_id)
        with pytest.raises(ValueError, match="latest accepted completed run"):
            kb.invalidate_completed_task(
                conn,
                task_id,
                superseded_run_id=run_id,
                reason="stale run",
                author="controller",
            )
        assert _snapshot(conn, task_id) == before

        latest = kb.latest_run(conn, task_id)
        assert latest is not None
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (task_id,))
        conn.commit()
        before = _snapshot(conn, task_id)
        with pytest.raises(ValueError, match="currently done"):
            kb.invalidate_completed_task(
                conn,
                task_id,
                superseded_run_id=latest.id,
                reason="not done",
                author="controller",
            )
        assert _snapshot(conn, task_id) == before

        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (task_id,))
        conn.commit()
        monkeypatch.setenv("HERMES_KANBAN_TASK", other_id)
        before = _snapshot(conn, task_id)
        with pytest.raises(RuntimeError, match="controller-only"):
            kb.invalidate_completed_task(
                conn,
                task_id,
                superseded_run_id=latest.id,
                reason="worker denied",
                author="controller",
            )
        assert _snapshot(conn, task_id) == before


def test_repeated_invalidation_and_blank_fields_make_zero_mutation(kanban_home):
    with kb.connect_closing() as conn:
        task_id, run_id = _complete_with_run(conn)
        with pytest.raises(ValueError, match="reason is required"):
            kb.invalidate_completed_task(
                conn,
                task_id,
                superseded_run_id=run_id,
                reason="   ",
                author="controller",
            )
        with pytest.raises(ValueError, match="author is required"):
            kb.invalidate_completed_task(
                conn,
                task_id,
                superseded_run_id=run_id,
                reason="false completion",
                author="   ",
            )

        kb.invalidate_completed_task(
            conn,
            task_id,
            superseded_run_id=run_id,
            reason="false completion",
            author="controller",
        )
        before = _snapshot(conn, task_id)
        with pytest.raises(ValueError, match="currently done"):
            kb.invalidate_completed_task(
                conn,
                task_id,
                superseded_run_id=run_id,
                reason="repeat",
                author="controller",
            )
        assert _snapshot(conn, task_id) == before


def test_internal_failure_rolls_back_task_run_event_comment_and_descendants(
    kanban_home, monkeypatch,
):
    with kb.connect_closing() as conn:
        task_id, run_id = _complete_with_run(conn, title="parent")
        child_id = kb.create_task(
            conn, title="child", assignee="builder", parents=[task_id],
        )
        assert kb.complete_task(conn, child_id, result="child result")
        before_parent = _snapshot(conn, task_id)
        before_child = _snapshot(conn, child_id)

        def fail_after_parent_mutation(inner_conn, inner_task_id, *, author):
            task = kb.get_task(inner_conn, inner_task_id)
            run = kb.get_run(inner_conn, run_id)
            assert task is not None and task.status == "blocked"
            assert run is not None and run.status == "invalidated"
            raise RuntimeError("synthetic descendant invalidation failure")

        monkeypatch.setattr(
            kb,
            "invalidate_descendants_for_parent_reopen",
            fail_after_parent_mutation,
        )
        with pytest.raises(RuntimeError, match="synthetic descendant"):
            kb.invalidate_completed_task(
                conn,
                task_id,
                superseded_run_id=run_id,
                reason="must roll back",
                author="controller",
            )

        assert _snapshot(conn, task_id) == before_parent
        assert _snapshot(conn, child_id) == before_child


def test_descendants_regate_and_running_termination_is_post_commit(
    kanban_home, monkeypatch,
):
    with kb.connect_closing() as conn:
        parent_id, parent_run_id = _complete_with_run(conn, title="parent")

        done_id = kb.create_task(
            conn, title="done child", assignee="builder", parents=[parent_id],
        )
        assert kb.complete_task(conn, done_id, result="stale child result")

        running_id = kb.create_task(
            conn, title="running child", assignee="builder", parents=[parent_id],
        )
        running = kb.claim_task(conn, running_id)
        assert running is not None
        kb._set_worker_pid(conn, running_id, 424242)

        blocked_id = kb.create_task(
            conn, title="blocked child", assignee="builder", parents=[parent_id],
        )
        assert kb.block_task(conn, blocked_id, reason="human decision")

        calls: list[tuple[int | None, str | None]] = []

        def fake_terminate(pid, claim_lock, **kwargs):
            side = kb.connect()
            try:
                parent = kb.get_task(side, parent_id)
                events = kb.list_events(side, parent_id)
            finally:
                side.close()
            assert parent is not None and parent.status == "blocked"
            assert any(event.kind == "completion_invalidated" for event in events)
            calls.append((pid, claim_lock))
            return {"terminated": True, "pid": pid}

        monkeypatch.setattr(kb, "_terminate_reclaimed_worker", fake_terminate)
        result = kb.invalidate_completed_task(
            conn,
            parent_id,
            superseded_run_id=parent_run_id,
            reason="parent evidence was false",
            author="controller",
        )

        assert {entry["id"] for entry in result["descendants"]} == {
            done_id,
            running_id,
        }
        assert calls and calls[0][0] == 424242
        assert result["terminations"] == [
            {"worker_pid": 424242, "terminated": True}
        ]

        done = kb.get_task(conn, done_id)
        running_task = kb.get_task(conn, running_id)
        blocked = kb.get_task(conn, blocked_id)
        assert done is not None and done.status == "todo"
        assert running_task is not None and running_task.status == "todo"
        assert running_task.current_run_id is None
        running_run = kb.latest_run(conn, running_id)
        assert running_run is not None and running_run.outcome == "reclaimed"
        assert blocked is not None and blocked.status == "blocked"
        assert not any(
            event.kind == "descendant_invalidated"
            for event in kb.list_events(conn, blocked_id)
        )

        # Preserved invalidated-run history must no longer be accepted as a
        # parent handoff or recent completed-work summary.
        context = kb.build_worker_context(conn, done_id)
        assert "original summary" not in context


def test_cli_requires_exact_board_and_refuses_worker_identity(
    kanban_home, monkeypatch, capsys,
):
    with kb.connect_closing() as conn:
        task_id, run_id = _complete_with_run(conn)
        before = _snapshot(conn, task_id)

    parser = _parser()
    args = parser.parse_args([
        "kanban", "invalidate-completion", task_id,
        "--run-id", str(run_id), "--reason", "false completion", "--json",
    ])
    assert kc.kanban_command(args) == 2
    assert "explicit --board" in capsys.readouterr().err
    with kb.connect_closing() as conn:
        assert _snapshot(conn, task_id) == before

    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_worker")
    args = parser.parse_args([
        "kanban", "--board", "default", "invalidate-completion", task_id,
        "--run-id", str(run_id), "--reason", "false completion", "--json",
    ])
    assert kc.kanban_command(args) == 1
    assert "controller-only" in capsys.readouterr().err
    with kb.connect_closing() as conn:
        assert _snapshot(conn, task_id) == before


def test_cli_json_readback_and_help(kanban_home, capsys):
    secret = "sk-proj-1234567890abcdefghijklmnopqrstuvwxyz"
    with kb.connect_closing() as conn:
        task_id, run_id = _complete_with_run(conn)

    parser = _parser()
    with pytest.raises(SystemExit) as help_exit:
        parser.parse_args(["kanban", "invalidate-completion", "--help"])
    assert help_exit.value.code == 0
    help_text = capsys.readouterr().out
    assert "--run-id" in help_text
    assert "--reason" in help_text
    assert "--json" in help_text

    args = parser.parse_args([
        "kanban", "--board", "default", "invalidate-completion", task_id,
        "--run-id", str(run_id), "--reason", f"false completion {secret}", "--json",
    ])
    assert kc.kanban_command(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert secret not in payload["reason"]
    assert payload == {
        "task_id": task_id,
        "superseded_run_id": run_id,
        "status": "blocked",
        "run_status": "invalidated",
        "run_outcome": "invalidated",
        "reason": payload["reason"],
        "author": payload["author"],
        "descendants": [],
        "terminations": [],
    }
