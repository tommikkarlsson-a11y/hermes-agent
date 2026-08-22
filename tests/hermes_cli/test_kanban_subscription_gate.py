"""Fixed initial subscription gate for SlackSparraus intake."""

from __future__ import annotations

import json
import threading
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


def test_concurrent_idempotent_creation_returns_one_task(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    barrier = threading.Barrier(2)
    ids = iter(("t_11111111", "t_22222222"))
    ids_lock = threading.Lock()

    def synchronized_task_id():
        with ids_lock:
            task_id = next(ids)
        barrier.wait(timeout=5)
        return task_id

    monkeypatch.setattr(kb, "_new_task_id", synchronized_task_id)

    def create_once(_index):
        connection = kb.connect()
        try:
            return kb.create_task(
                connection,
                title="Same intake",
                idempotency_key="slack-message-1",
                initial_status="blocked",
                initial_block_kind="subscription_gate",
            )
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        task_ids = list(pool.map(create_once, (0, 1)))

    assert task_ids[0] == task_ids[1]
    connection = kb.connect()
    try:
        count = connection.execute(
            "SELECT COUNT(*) FROM tasks WHERE idempotency_key = 'slack-message-1'"
        ).fetchone()[0]
    finally:
        connection.close()
    assert count == 1


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
