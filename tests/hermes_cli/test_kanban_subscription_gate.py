"""Fixed initial subscription gate for SlackSparraus intake."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    connection = kb.connect()
    try:
        yield connection
    finally:
        connection.close()


def test_create_task_can_start_in_typed_subscription_gate(conn):
    task_id = kb.create_task(
        conn,
        title="Slack intake",
        initial_status="blocked",
        initial_block_kind="subscription_gate",
    )

    task = kb.get_task(conn, task_id)
    assert task is not None
    assert (task.status, task.block_kind) == ("blocked", "subscription_gate")


def _origin_subscription(chat_id="chat-1"):
    return {
        "platform": "slack",
        "chat_id": chat_id,
        "thread_id": "thread-1",
        "user_id": "user-1",
        "chat_type": "channel",
        "notifier_profile": "orchestrator",
        "delivery_mode": "notify+wake",
    }


def test_admitted_create_commits_task_and_origin_subscription_together(conn):
    task_id, subscribed = kb.create_admitted_task(
        conn,
        subscription=_origin_subscription(),
        title="Admitted mission",
        assignee="coder",
        idempotency_key="admitted-mission-1",
        created_by="orchestrator",
        session_id="session-1",
    )

    task = kb.get_task(conn, task_id)
    assert task is not None
    assert (task.status, task.block_kind, subscribed) == ("ready", None, True)
    sub = conn.execute(
        "SELECT platform, chat_id, thread_id FROM kanban_notify_subs WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    assert tuple(sub) == ("slack", "chat-1", "thread-1")
    events = [event.kind for event in kb.list_events(conn, task_id)]
    assert events == ["created", "admission_gate_released"]


def test_admitted_create_fails_closed_in_subscription_gate(conn):
    task_id, subscribed = kb.create_admitted_task(
        conn,
        subscription=None,
        title="Unroutable mission",
        assignee="coder",
        idempotency_key="unroutable-mission-1",
        created_by="orchestrator",
        session_id="session-1",
    )

    task = kb.get_task(conn, task_id)
    assert task is not None
    assert (task.status, task.block_kind, subscribed) == (
        "blocked",
        "subscription_gate",
        False,
    )
    assert kb.claim_task(conn, task_id, claimer="dispatcher") is None
    assert kb.list_runs(conn, task_id) == []
    events = [event.kind for event in kb.list_events(conn, task_id)]
    assert events == ["created", "subscription_gate"]


def test_admitted_create_subscription_error_persists_zero_run_gate(conn, monkeypatch):
    def fail_subscription(*args, **kwargs):
        raise RuntimeError("route unavailable")

    monkeypatch.setattr(kb, "add_notify_sub", fail_subscription)
    task_id, subscribed = kb.create_admitted_task(
        conn,
        subscription=_origin_subscription(),
        title="Route failure",
        assignee="coder",
        idempotency_key="route-failure-1",
    )

    task = kb.get_task(conn, task_id)
    assert task is not None
    assert (task.status, task.block_kind, subscribed) == (
        "blocked",
        "subscription_gate",
        False,
    )
    assert kb.list_runs(conn, task_id) == []


def test_admitted_create_id_collision_retries_without_a_run(conn, monkeypatch):
    occupied = kb.create_task(conn, title="Occupied")
    ids = iter((occupied, "t_abcdef12"))
    monkeypatch.setattr(kb, "_new_task_id", lambda: next(ids))

    task_id, subscribed = kb.create_admitted_task(
        conn,
        subscription=_origin_subscription(),
        title="Collision winner",
        assignee="coder",
        idempotency_key="collision-winner-1",
    )

    assert (task_id, subscribed) == ("t_abcdef12", True)
    assert kb.list_runs(conn, task_id) == []


def test_admitted_create_rejects_mismatching_idempotent_contract(conn):
    task_id, subscribed = kb.create_admitted_task(
        conn,
        subscription=_origin_subscription(),
        title="Canonical mission",
        assignee="coder",
        idempotency_key="canonical-mission-1",
    )
    assert subscribed is True

    with pytest.raises(RuntimeError, match="contract collision"):
        kb.create_admitted_task(
            conn,
            subscription=_origin_subscription(),
            title="Escaped mission",
            assignee="ops",
            idempotency_key="canonical-mission-1",
        )

    task = kb.get_task(conn, task_id)
    assert task is not None
    assert (task.title, task.assignee, task.status) == (
        "Canonical mission",
        "coder",
        "ready",
    )
    assert kb.list_runs(conn, task_id) == []


def test_matching_retry_releases_subscription_gate(conn):
    kwargs = {
        "title": "Recover route",
        "assignee": "coder",
        "idempotency_key": "recover-route-1",
    }
    task_id, subscribed = kb.create_admitted_task(
        conn, subscription=None, **kwargs
    )
    assert subscribed is False

    retry_id, subscribed = kb.create_admitted_task(
        conn, subscription=_origin_subscription(), **kwargs
    )
    task = kb.get_task(conn, task_id)
    assert task is not None
    assert (retry_id, subscribed, task.status, task.block_kind) == (
        task_id,
        True,
        "ready",
        None,
    )


def test_admitted_retry_preserves_pending_notification_cursor(conn):
    kwargs = {
        "title": "Preserve cursor",
        "assignee": "coder",
        "idempotency_key": "preserve-cursor-1",
    }
    task_id, _ = kb.create_admitted_task(
        conn, subscription=_origin_subscription(), **kwargs
    )
    before = kb.list_notify_subs(conn, task_id)[0]["last_event_id"]
    with kb.write_txn(conn):
        kb._append_event(conn, task_id, "done", {"result": "PASS"})
    pending_event = kb.list_events(conn, task_id)[-1].id

    retry_id, subscribed = kb.create_admitted_task(
        conn, subscription=_origin_subscription(), **kwargs
    )
    after = kb.list_notify_subs(conn, task_id)[0]["last_event_id"]
    assert (retry_id, subscribed, after) == (task_id, True, before)
    assert after < pending_event


def test_admitted_retry_rejects_different_origin_route(conn):
    kwargs = {
        "title": "Canonical route",
        "assignee": "coder",
        "idempotency_key": "canonical-route-1",
    }
    task_id, _ = kb.create_admitted_task(
        conn, subscription=_origin_subscription("chat-1"), **kwargs
    )

    with pytest.raises(RuntimeError, match="different origin route"):
        kb.create_admitted_task(
            conn, subscription=_origin_subscription("chat-2"), **kwargs
        )
    assert len(kb.list_notify_subs(conn, task_id)) == 1


def test_release_subscription_gate_is_exact_compare_and_swap(conn):
    task_id = kb.create_task(
        conn,
        title="Slack intake",
        initial_status="blocked",
        initial_block_kind="subscription_gate",
    )

    assert kb.release_subscription_gate(conn, task_id) is True
    assert kb.claim_task(conn, task_id, claimer="idea-spike") is not None
    assert kb.block_task(
        conn,
        task_id,
        reason="Need scope",
        kind="needs_input",
        input_request={"choices": ["Narrow", "Broad"]},
    ) is True
    assert kb.release_subscription_gate(conn, task_id) is False

    task = kb.get_task(conn, task_id)
    assert task is not None
    assert (task.status, task.block_kind) == ("blocked", "needs_input")
    event = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'blocked' "
        "ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    assert json.loads(event["payload"])["input_request"] == {
        "choices": ["Narrow", "Broad"]
    }


def test_concurrent_matching_admission_returns_one_task_and_subscription(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))

    def create_once(_index):
        connection = kb.connect()
        try:
            return kb.create_admitted_task(
                connection,
                subscription=_origin_subscription(),
                title="Same intake",
                assignee="coder",
                idempotency_key="slack-message-1",
            )
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create_once, (0, 1)))

    assert results[0] == results[1]
    task_id = results[0][0]
    connection = kb.connect()
    try:
        count = connection.execute(
            "SELECT COUNT(*) FROM tasks WHERE idempotency_key = 'slack-message-1'"
        ).fetchone()[0]
        sub_count = connection.execute(
            "SELECT COUNT(*) FROM kanban_notify_subs WHERE task_id = ?",
            (task_id,),
        ).fetchone()[0]
        task = kb.get_task(connection, task_id)
    finally:
        connection.close()
    assert count == 1
    assert sub_count == 1
    assert task is not None and (task.status, task.block_kind) == ("ready", None)


@pytest.mark.parametrize(
    "status,kind",
    [
        ("running", "subscription_gate"),
        ("blocked", "needs_input"),
    ],
)
def test_create_task_rejects_unsupported_initial_block_kind(conn, status, kind):
    with pytest.raises(ValueError):
        kb.create_task(
            conn,
            title="Invalid gate",
            initial_status=status,
            initial_block_kind=kind,
        )
