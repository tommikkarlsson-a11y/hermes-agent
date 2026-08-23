from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from gateway.config import Platform
from gateway.run import GatewayRunner
from gateway.run import _kanban_supervision_identity_matches
from gateway.kanban_watchers import (
    _supervision_adapter_session_key,
    _supervision_source,
)
from hermes_cli import kanban_db as kb


class _Adapter:
    supports_async_delivery = True

    def __init__(self):
        self.handled = []

    async def send(self, chat_id, text, metadata=None):
        raise AssertionError("supervised wake-only rows must not send a passive ping")

    async def handle_message(self, event):
        self.handled.append(event)


def _runner(adapter):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._kanban_sub_fail_counts = {}
    runner._kanban_dispatcher_lock_handle = object()
    runner._kanban_notifier_profile = "default"
    return runner


async def _one_tick(monkeypatch, runner):
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return None
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_notifier_watcher(interval=1)


def _create_supervised_gateway_task() -> tuple[str, int]:
    with kb.connect_closing() as conn:
        task_id = kb.create_supervised_task(
            conn,
            title="Gateway supervision",
            body=None,
            acceptance="The creator turn durably ACKs the event.",
            creator_profile="default",
            creator_session_id="creator-session",
            platform="telegram",
            chat_id="creator-chat",
            mission_key="gateway-runtime",
            max_runtime_seconds=600,
            goal_max_turns=4,
            max_retries=1,
        )
        with kb.write_txn(conn):
            kb._append_event(conn, task_id, "crashed", {"attempt": 1})
        event_id = kb.list_events(conn, task_id)[-1].id
    return task_id, event_id


def test_gateway_supervised_wake_claim_does_not_ack_on_invocation(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    task_id, event_id = _create_supervised_gateway_task()

    adapter = _Adapter()
    asyncio.run(_one_tick(monkeypatch, _runner(adapter)))

    assert len(adapter.handled) == 1
    supervision = adapter.handled[0].metadata["kanban_supervision"]
    assert supervision["task_id"] == task_id
    assert supervision["pending_event_id"] == event_id
    with kb.connect_closing() as conn:
        route = kb.list_notify_subs(conn, task_id)[0]
        assert route["last_event_id"] < event_id
        assert route["supervision_pending_event_id"] == event_id
        assert route["supervision_lease_token"] == supervision["lease_token"]
        assert not any(
            event.kind == "supervision_receipt"
            for event in kb.list_events(conn, task_id)
        )


def test_gateway_running_creator_turn_prevents_expired_lease_replacement(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    task_id, _event_id = _create_supervised_gateway_task()
    with kb.connect_closing() as conn:
        first = kb.poll_supervision_wake(
            conn,
            task_id=task_id,
            creator_profile="default",
            creator_session_id="creator-session",
        )
        assert first is not None
        conn.execute(
            "UPDATE kanban_notify_subs SET supervision_lease_expires_at = 0 "
            "WHERE task_id = ?",
            (task_id,),
        )
        sub = kb.list_notify_subs(conn, task_id)[0]

    adapter = _Adapter()
    adapter.config = SimpleNamespace(extra={})
    source = _supervision_source(Platform.TELEGRAM, sub, "default", adapter)
    key = _supervision_adapter_session_key(adapter, source)
    adapter._active_sessions = {key: object()}

    asyncio.run(_one_tick(monkeypatch, _runner(adapter)))

    assert adapter.handled == []
    with kb.connect_closing() as conn:
        route = kb.list_notify_subs(conn, task_id)[0]
        assert route["supervision_lease_token"] == first.lease_token
        assert route["supervision_attempts"] == 1


def test_gateway_post_turn_finalizes_supervision_with_lease_cas(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    task_id, event_id = _create_supervised_gateway_task()
    with kb.connect_closing() as conn:
        claim = kb.poll_supervision_wake(
            conn,
            task_id=task_id,
            creator_profile="default",
            creator_session_id="creator-session",
        )
    assert claim is not None

    class _Store:
        async def get_or_create_session(self, source, touch_activity=False):
            return SimpleNamespace(session_id="creator-session")

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.session_store = object()
    store = _Store()
    store._store = runner.session_store
    runner._async_session_store = store

    async def _noop(*args, **kwargs):
        return None

    runner._post_turn_goal_continuation = _noop
    runner._post_turn_loop_completion = _noop
    event = SimpleNamespace(
        metadata={
            "kanban_supervision": {
                "task_id": task_id,
                "creator_profile": "default",
                "creator_session_id": "creator-session",
                "pending_event_id": event_id,
                "lease_token": claim.lease_token,
                "board": kb.DEFAULT_BOARD,
            }
        }
    )
    source = SimpleNamespace(profile="default")

    asyncio.run(
        runner._run_post_turn_hooks(
            agent_result={"final_response": "Inspected canonical task state."},
            source=source,
            is_internal=True,
            event=event,
        )
    )

    with kb.connect_closing() as conn:
        route = kb.list_notify_subs(conn, task_id)[0]
        assert route["last_event_id"] == event_id
        assert route["supervision_pending_event_id"] is None
        assert route["supervision_lease_token"] is None
        receipts = [
            event for event in kb.list_events(conn, task_id)
            if event.kind == "supervision_receipt"
        ]
        assert len(receipts) == 1
        assert receipts[0].payload["event_sequence"] == event_id


def test_gateway_supervision_identity_is_checked_before_agent_execution():
    supervision = {
        "creator_profile": "default",
        "creator_session_id": "creator-session",
    }
    assert _kanban_supervision_identity_matches(
        supervision,
        session_id="creator-session",
        profile="default",
    )
    assert not _kanban_supervision_identity_matches(
        supervision,
        session_id="rebound-session",
        profile="default",
    )
    assert not _kanban_supervision_identity_matches(
        supervision,
        session_id="creator-session",
        profile="ops",
    )


def test_gateway_interrupted_supervision_turn_leaves_lease_unacked(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    task_id, event_id = _create_supervised_gateway_task()
    with kb.connect_closing() as conn:
        claim = kb.poll_supervision_wake(
            conn,
            task_id=task_id,
            creator_profile="default",
            creator_session_id="creator-session",
        )
    assert claim is not None

    class _Store:
        async def get_or_create_session(self, source, touch_activity=False):
            return SimpleNamespace(session_id="creator-session")

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.session_store = object()
    store = _Store()
    store._store = runner.session_store
    runner._async_session_store = store

    async def _noop(*args, **kwargs):
        return None

    runner._post_turn_goal_continuation = _noop
    runner._post_turn_loop_completion = _noop
    event = SimpleNamespace(
        metadata={
            "kanban_supervision": {
                "task_id": task_id,
                "creator_profile": "default",
                "creator_session_id": "creator-session",
                "pending_event_id": event_id,
                "lease_token": claim.lease_token,
                "board": kb.DEFAULT_BOARD,
            }
        },
        _kanban_supervision_turn_session_id="creator-session",
    )
    asyncio.run(
        runner._run_post_turn_hooks(
            agent_result={"interrupted": True, "final_response": ""},
            source=SimpleNamespace(profile="default"),
            is_internal=True,
            event=event,
        )
    )

    with kb.connect_closing() as conn:
        route = kb.list_notify_subs(conn, task_id)[0]
        assert route["last_event_id"] < event_id
        assert route["supervision_pending_event_id"] == event_id
        assert route["supervision_lease_token"] == claim.lease_token
        assert not any(
            item.kind == "supervision_receipt"
            for item in kb.list_events(conn, task_id)
        )
