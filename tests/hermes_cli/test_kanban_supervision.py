from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def board_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


def test_supervised_create_is_atomic_and_duplicate_safe(board_home):
    def create_once() -> str:
        with kb.connect_closing() as conn:
            return kb.create_supervised_task(
                conn,
                title="Ship bounded owner mission",
                body="Implement only the approved slice.",
                acceptance="One task and one durable supervision route.",
                creator_profile="researcher",
                creator_session_id="session-123",
                platform="tui",
                chat_id="bot-chat-key",
                mission_key="mission-42",
                max_runtime_seconds=3600,
                goal_max_turns=8,
                max_retries=1,
                now=1_000,
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        task_ids = list(pool.map(lambda _: create_once(), range(2)))

    assert task_ids[0] == task_ids[1]
    with kb.connect_closing() as conn:
        tasks = conn.execute(
            "SELECT * FROM tasks WHERE idempotency_key = ?",
            ("owner:researcher:session-123:mission-42",),
        ).fetchall()
        routes = conn.execute(
            "SELECT * FROM kanban_notify_subs WHERE task_id = ?",
            (task_ids[0],),
        ).fetchall()

    assert len(tasks) == 1
    assert tasks[0]["created_by"] == "researcher"
    assert tasks[0]["assignee"] == "researcher"
    assert tasks[0]["session_id"] == "session-123"
    assert tasks[0]["workspace_kind"] == "scratch"
    assert tasks[0]["goal_mode"] == 1
    assert len(routes) == 1
    assert routes[0]["notifier_profile"] == "researcher"
    assert routes[0]["supervision_session_id"] == "session-123"

    with kb.connect_closing() as conn:
        with pytest.raises(
            RuntimeError, match="already bound to a different route"
        ):
            kb.create_supervised_task(
                conn,
                title="same mission, escaped route",
                body=None,
                acceptance="The original route remains canonical.",
                creator_profile="researcher",
                creator_session_id="session-123",
                platform="telegram",
                chat_id="different-chat",
                mission_key="mission-42",
                max_runtime_seconds=600,
                goal_max_turns=4,
                max_retries=1,
            )
        routes = [
            row
            for row in kb.list_notify_subs(conn, task_ids[0])
            if row["supervision_session_id"] == "session-123"
        ]
        assert len(routes) == 1
    assert routes[0]["supervision_deadline_generation"] == 1
    assert routes[0]["supervision_deadline_at"] == 1_300

    with kb.connect_closing() as conn:
        conn.execute(
            "CREATE TRIGGER abort_supervision_route BEFORE INSERT ON kanban_notify_subs "
            "WHEN NEW.supervision_session_id = 'session-rollback' "
            "BEGIN SELECT RAISE(ABORT, 'route refused'); END"
        )
        with pytest.raises(Exception, match="route refused"):
            kb.create_supervised_task(
                conn,
                title="Must roll back",
                body=None,
                acceptance="No orphan task.",
                creator_profile="researcher",
                creator_session_id="session-rollback",
                platform="tui",
                chat_id="rollback-key",
                mission_key="mission-rollback",
                max_runtime_seconds=600,
                goal_max_turns=2,
                max_retries=1,
                now=2_000,
            )
        orphan = conn.execute(
            "SELECT id FROM tasks WHERE idempotency_key = ?",
            ("owner:researcher:session-rollback:mission-rollback",),
        ).fetchone()
    assert orphan is None


def test_supervision_lease_receipt_ack_survives_death_and_mid_turn_event(board_home):
    with kb.connect_closing() as conn:
        task_id = kb.create_supervised_task(
            conn,
            title="Lease mission",
            body=None,
            acceptance="Every actionable event is ACKed exactly once.",
            creator_profile="researcher",
            creator_session_id="session-lease",
            platform="tui",
            chat_id="lease-chat",
            mission_key="lease-1",
            max_runtime_seconds=600,
            goal_max_turns=4,
            max_retries=1,
            now=1_000,
        )
        initial_cursor = kb.list_notify_subs(conn, task_id)[0]["last_event_id"]
        with kb.write_txn(conn):
            kb._append_event(conn, task_id, "crashed", {"attempt": 1})
        first_event_id = kb.list_events(conn, task_id)[-1].id

    def claim_once(now: int):
        with kb.connect_closing() as conn:
            return kb.lease_supervision_wake(
                conn,
                task_id=task_id,
                creator_profile="researcher",
                creator_session_id="session-lease",
                now=now,
                lease_seconds=30,
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda _: claim_once(1_100), range(2)))
    leased = [claim for claim in claims if claim is not None]
    assert len(leased) == 1
    first_claim = leased[0]
    assert first_claim.pending_event_id == first_event_id
    assert first_claim.event.kind == "crashed"

    with kb.connect_closing() as conn:
        route = kb.list_notify_subs(conn, task_id)[0]
        assert route["last_event_id"] == initial_cursor
        with kb.write_txn(conn):
            kb._append_event(conn, task_id, "timed_out", {"attempt": 2})
        second_event_id = kb.list_events(conn, task_id)[-1].id

        assert not kb.finalize_supervision_wake(
            conn,
            task_id=task_id,
            creator_profile="researcher",
            creator_session_id="session-lease",
            lease_token="wrong-token",
            disposition="inspected",
            now=1_120,
        )
        assert kb.finalize_supervision_wake(
            conn,
            task_id=task_id,
            creator_profile="researcher",
            creator_session_id="session-lease",
            lease_token=first_claim.lease_token,
            disposition="inspected",
            now=1_120,
        )
        assert not kb.finalize_supervision_wake(
            conn,
            task_id=task_id,
            creator_profile="researcher",
            creator_session_id="session-lease",
            lease_token=first_claim.lease_token,
            disposition="duplicate-finalize",
            now=1_121,
        )

        route = kb.list_notify_subs(conn, task_id)[0]
        assert route["last_event_id"] == first_event_id
        assert route["supervision_pending_event_id"] is None
        assert route["supervision_deadline_generation"] == 2
        assert route["supervision_deadline_at"] == 1_420
        receipts = [
            event for event in kb.list_events(conn, task_id)
            if event.kind == "supervision_receipt"
        ]
        assert len(receipts) == 1
        assert receipts[0].payload["event_sequence"] == first_event_id

    death_claim = claim_once(1_121)
    assert death_claim is not None
    assert death_claim.pending_event_id == second_event_id
    assert claim_once(1_150) is None
    redelivered = claim_once(1_152)
    assert redelivered is not None
    assert redelivered.pending_event_id == second_event_id
    assert redelivered.lease_token != death_claim.lease_token
    assert redelivered.attempt == 2

    with kb.connect_closing() as conn:
        assert not kb.finalize_supervision_wake(
            conn,
            task_id=task_id,
            creator_profile="researcher",
            creator_session_id="session-lease",
            lease_token=death_claim.lease_token,
            disposition="late-dead-watcher",
            now=1_153,
        )
        assert kb.finalize_supervision_wake(
            conn,
            task_id=task_id,
            creator_profile="researcher",
            creator_session_id="session-lease",
            lease_token=redelivered.lease_token,
            disposition="redelivered",
            now=1_153,
        )
        route = kb.list_notify_subs(conn, task_id)[0]
        assert route["last_event_id"] == second_event_id
        receipts = [
            event for event in kb.list_events(conn, task_id)
            if event.kind == "supervision_receipt"
        ]
        assert len(receipts) == 2


def test_healthy_exact_profile_worker_suppresses_deadline_and_rearms(board_home):
    with kb.connect_closing() as conn:
        task_id = kb.create_supervised_task(
            conn,
            title="Deadline mission",
            body=None,
            acceptance="No concurrent owner and worker turns.",
            creator_profile="researcher",
            creator_session_id="session-deadline",
            platform="tui",
            chat_id="deadline-chat",
            mission_key="deadline-1",
            max_runtime_seconds=600,
            goal_max_turns=4,
            max_retries=1,
            now=1_000,
        )
        with kb.write_txn(conn):
            run_cur = conn.execute(
                "INSERT INTO task_runs (task_id, profile, status, claim_lock, "
                "claim_expires, worker_pid, max_runtime_seconds, "
                "last_heartbeat_at, started_at) "
                "VALUES (?, 'researcher', 'running', 'host:4242', 1500, "
                "4242, 600, 1290, 1200)",
                (task_id,),
            )
            conn.execute(
                "UPDATE tasks SET status = 'running', claim_lock = 'host:4242', "
                "claim_expires = 1500, worker_pid = 4242, "
                "last_heartbeat_at = 1290, started_at = 1200, "
                "current_run_id = ? WHERE id = ?",
                (run_cur.lastrowid, task_id),
            )
            kb._append_event(
                conn, task_id, "claimed", {"claimer": "host:4242"},
                run_id=run_cur.lastrowid,
            )
            kb._append_event(
                conn, task_id, "spawned", {"pid": 4242},
                run_id=run_cur.lastrowid,
            )

        suppressed = kb.poll_supervision_wake(
            conn,
            task_id=task_id,
            creator_profile="researcher",
            creator_session_id="session-deadline",
            now=1_301,
            pid_alive_fn=lambda pid: pid == 4242,
        )
        assert suppressed is None
        route = kb.list_notify_subs(conn, task_id)[0]
        assert route["supervision_deadline_generation"] == 2
        assert route["supervision_deadline_at"] == 1_601
        assert route["supervision_lease_token"] is None
        assert not any(
            event.kind == "supervision_receipt"
            for event in kb.list_events(conn, task_id)
        )

        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_runs SET profile = 'ops' WHERE id = ?",
                (run_cur.lastrowid,),
            )
        wake = kb.poll_supervision_wake(
            conn,
            task_id=task_id,
            creator_profile="researcher",
            creator_session_id="session-deadline",
            now=1_602,
            pid_alive_fn=lambda pid: pid == 4242,
        )
        assert wake is not None
        assert wake.pending_event_id == 0
        assert wake.event is None


def test_typed_terminal_block_requires_explicit_supervision_rearm(board_home):
    with kb.connect_closing() as conn:
        task_id = kb.create_supervised_task(
            conn,
            title="Blocked mission",
            body=None,
            acceptance="A terminal owner block never self-restarts.",
            creator_profile="researcher",
            creator_session_id="session-block",
            platform="tui",
            chat_id="block-chat",
            mission_key="block-1",
            max_runtime_seconds=600,
            goal_max_turns=4,
            max_retries=1,
            now=1_000,
        )
        assert kb.block_task(
            conn,
            task_id,
            reason="Missing authority",
            kind="needs_input",
        )
        assert kb.record_supervision_terminal_block(
            conn,
            task_id=task_id,
            creator_profile="researcher",
            creator_session_id="session-block",
            reason="Missing authority",
            kind="needs_input",
            now=1_050,
        )
        assert not kb.record_supervision_terminal_block(
            conn,
            task_id=task_id,
            creator_profile="researcher",
            creator_session_id="session-block",
            reason="Duplicate caller retry",
            kind="needs_input",
            now=1_051,
        )

        route = kb.list_notify_subs(conn, task_id)[0]
        assert route["supervision_deadline_at"] is None
        assert route["supervision_pending_event_id"] is None
        terminal_receipts = [
            event for event in kb.list_events(conn, task_id)
            if event.kind == "supervision_receipt"
            and event.payload.get("terminal") is True
        ]
        assert len(terminal_receipts) == 1
        assert terminal_receipts[0].payload["disposition"] == "self_blocked"
        assert terminal_receipts[0].payload["block_kind"] == "needs_input"

        # The broad legacy unblock remains compatible but is deliberately not
        # authority to reactivate the exact creator-session supervision route.
        assert kb.unblock_task(conn, task_id)
        assert kb.poll_supervision_wake(
            conn,
            task_id=task_id,
            creator_profile="researcher",
            creator_session_id="session-block",
            now=2_000,
        ) is None
        route = kb.list_notify_subs(conn, task_id)[0]
        assert route["supervision_deadline_at"] is None

        assert kb.rearm_supervision(
            conn,
            task_id=task_id,
            creator_profile="researcher",
            creator_session_id="session-block",
            now=2_000,
        )
        route = kb.list_notify_subs(conn, task_id)[0]
        assert route["supervision_deadline_generation"] == 2
        assert route["supervision_deadline_at"] == 2_300
        assert kb.poll_supervision_wake(
            conn,
            task_id=task_id,
            creator_profile="researcher",
            creator_session_id="session-block",
            now=2_301,
        ) is not None


def test_dispatcher_records_one_mission_wide_recovery_marker(board_home, monkeypatch):
    monkeypatch.setattr(kb.time, "time", lambda: 2_000)
    with kb.connect_closing() as conn:
        supervised_id = kb.create_supervised_task(
            conn,
            title="Recover supervised mission",
            body=None,
            acceptance="Recovery is visible once before dispatcher retry.",
            creator_profile="researcher",
            creator_session_id="session-recovery",
            platform="tui",
            chat_id="recovery-chat",
            mission_key="recovery-1",
            max_runtime_seconds=600,
            goal_max_turns=4,
            max_retries=2,
            now=1_000,
        )
        legacy_id = kb.create_task(conn, title="Recover legacy task", assignee="ops")
        with kb.write_txn(conn):
            for task_id in (supervised_id, legacy_id):
                conn.execute(
                    "UPDATE tasks SET status = 'running', claim_lock = 'remote:7', "
                    "claim_expires = 1_900, worker_pid = NULL WHERE id = ?",
                    (task_id,),
                )

        assert kb.release_stale_claims(conn) == 2
        supervised_events = kb.list_events(conn, supervised_id)
        supervised_kinds = [event.kind for event in supervised_events]
        assert supervised_kinds.count("supervision_recovery") == 1
        assert supervised_kinds.index("supervision_recovery") < supervised_kinds.index(
            "reclaimed"
        )
        marker = next(
            event for event in supervised_events
            if event.kind == "supervision_recovery"
        )
        assert marker.payload["recovery_kind"] == "reclaimed"
        assert not any(
            event.kind == "supervision_recovery"
            for event in kb.list_events(conn, legacy_id)
        )

        # A later recovery in the same supervised mission reuses the original
        # mission-wide marker rather than producing a marker per attempt.
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'running', claim_lock = 'remote:8', "
                "claim_expires = 1_900 WHERE id = ?",
                (supervised_id,),
            )
        assert kb.release_stale_claims(conn) == 1
        assert [
            event.kind for event in kb.list_events(conn, supervised_id)
        ].count("supervision_recovery") == 1
