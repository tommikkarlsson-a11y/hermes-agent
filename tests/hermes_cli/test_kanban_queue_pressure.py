from __future__ import annotations

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def conn(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _: True)
    kb.init_db()
    connection = kb.connect()
    try:
        yield connection
    finally:
        connection.close()


def _subscribed_task(conn, *, status="ready", assignee="alice", now=1_000):
    task_id = kb.create_task(
        conn,
        title=f"{status} task",
        assignee=assignee,
        initial_status="running",
    )
    conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, task_id))
    conn.commit()
    kb.add_notify_sub(conn, task_id=task_id, platform="slack", chat_id="C1")
    conn.execute(
        "UPDATE tasks SET created_at = ? WHERE id = ?",
        (now - 100, task_id),
    )
    conn.execute(
        "UPDATE task_events SET created_at = ? WHERE task_id = ?",
        (now - 100, task_id),
    )
    conn.commit()
    return task_id


def _queue_kinds(conn, task_id):
    return [
        row["kind"]
        for row in conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ? "
            "AND kind LIKE 'queue_pressure%' ORDER BY id",
            (task_id,),
        )
    ]


def test_pressure_requires_more_than_two_live_intervals_and_deduplicates(conn):
    task_id = _subscribed_task(conn, now=1_000)
    conn.execute(
        "UPDATE tasks SET created_at = 980 WHERE id = ?",
        (task_id,),
    )
    conn.execute(
        "UPDATE task_events SET created_at = 980 WHERE task_id = ?",
        (task_id,),
    )
    conn.commit()

    assert kb.reconcile_queue_pressure(
        conn, dispatch_interval_seconds=10, now=1_000
    ) == []

    assert kb.reconcile_queue_pressure(
        conn, dispatch_interval_seconds=10, now=1_001
    ) == [(task_id, "queue_pressure")]
    assert kb.reconcile_queue_pressure(
        conn, dispatch_interval_seconds=10, now=1_100
    ) == []
    assert _queue_kinds(conn, task_id) == ["queue_pressure"]


def test_global_and_profile_saturation_are_not_queue_faults(conn):
    global_task = _subscribed_task(conn, assignee="alice")
    profile_task = _subscribed_task(conn, assignee="alice")
    busy_id = kb.create_task(
        conn,
        title="busy worker",
        assignee="alice",
        initial_status="running",
    )
    conn.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (busy_id,))
    conn.commit()

    assert kb.reconcile_queue_pressure(
        conn,
        dispatch_interval_seconds=10,
        max_in_progress=1,
        max_in_progress_per_profile=4,
        now=1_000,
    ) == []
    assert kb.reconcile_queue_pressure(
        conn,
        dispatch_interval_seconds=10,
        max_in_progress=4,
        max_in_progress_per_profile=1,
        now=1_000,
    ) == []
    assert _queue_kinds(conn, global_task) == []
    assert _queue_kinds(conn, profile_task) == []


@pytest.mark.parametrize("status", ["todo", "review", "blocked"])
def test_non_ready_work_is_not_queue_pressure(conn, status):
    task_id = _subscribed_task(conn, status=status)

    assert kb.reconcile_queue_pressure(
        conn,
        dispatch_interval_seconds=10,
        max_in_progress=4,
        max_in_progress_per_profile=1,
        now=1_000,
    ) == []
    assert _queue_kinds(conn, task_id) == []


def test_pressure_emits_one_recovery_when_claim_delay_clears(conn):
    task_id = _subscribed_task(conn)
    assert kb.reconcile_queue_pressure(
        conn,
        dispatch_interval_seconds=10,
        max_in_progress=4,
        max_in_progress_per_profile=1,
        now=1_000,
    ) == [(task_id, "queue_pressure")]

    conn.execute(
        "UPDATE tasks SET status = 'running', claim_lock = 'worker' WHERE id = ?",
        (task_id,),
    )
    conn.commit()
    assert kb.reconcile_queue_pressure(
        conn,
        dispatch_interval_seconds=10,
        max_in_progress=4,
        max_in_progress_per_profile=1,
        now=1_001,
    ) == [(task_id, "queue_pressure_recovered")]
    assert kb.reconcile_queue_pressure(
        conn,
        dispatch_interval_seconds=10,
        max_in_progress=4,
        max_in_progress_per_profile=1,
        now=1_002,
    ) == []
    assert _queue_kinds(conn, task_id) == [
        "queue_pressure",
        "queue_pressure_recovered",
    ]
