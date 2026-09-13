"""Native controller routing: real registry, temp DB and denial/race checks."""
from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def routing_env(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text("toolsets: [kanban]\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "controller")
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from tools import kanban_tools as kt
    from tools.registry import invalidate_check_fn_cache, registry

    invalidate_check_fn_cache()
    board = "routing-test"
    monkeypatch.setenv("HERMES_KANBAN_BOARD", board)
    with kbc.connect_closing(board=board) as conn:
        tid = kb.create_task(
            conn, title="existing research", assignee="old-reviewer",
            initial_status="blocked", model_override="old-model",
            provider_override="old-provider", reasoning_effort="high",
        )
    yield kb, kbc, kt, registry, board, tid
    invalidate_check_fn_cache()


def call(env, name, **args):
    return json.loads(env[3].dispatch(name, {"task_id": env[5], "board": env[4], **args}))


def snapshot(env):
    with env[1].connect_closing(board=env[4]) as conn:
        row = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (env[5],)).fetchone())
        events = [dict(r) for r in conn.execute(
            "SELECT * FROM task_events WHERE task_id=? ORDER BY id", (env[5],)
        )]
    return row, events


def test_reassign_changes_only_owner_and_native_routing_event(routing_env):
    before, _ = snapshot(routing_env)
    result = call(routing_env, "kanban_reassign", assignee="new-reviewer")
    assert result.get("ok") is True, result
    after, events = snapshot(routing_env)
    assert result["assignee"] == after["assignee"] == "new-reviewer"
    for field in ("status", "body", "model_override", "provider_override", "reasoning_effort", "current_run_id"):
        assert after[field] == before[field]
    assert events[-1]["kind"] == "assigned"


def test_set_and_explicit_clear_preserve_reasoning_and_blocked_state(routing_env):
    result = call(routing_env, "kanban_set_model", model="new-model", provider="new-provider")
    assert result.get("ok") is True, result
    row, _ = snapshot(routing_env)
    assert (row["model_override"], row["provider_override"]) == ("new-model", "new-provider")
    result = call(routing_env, "kanban_set_model", clear_override=True)
    assert result.get("ok") is True, result
    row, events = snapshot(routing_env)
    assert row["model_override"] is None and row["provider_override"] is None
    assert row["reasoning_effort"] == "high" and row["status"] == "blocked"
    assert events[-1]["kind"] == "model_override_set"


@pytest.mark.parametrize("name,args", [
    ("kanban_reassign", {"assignee": "new-reviewer"}),
    ("kanban_set_model", {"clear_override": True}),
])
@pytest.mark.parametrize("context", ["worker", "child", "child-process", "not-opted-in"])
def test_stale_registry_dispatch_denies_without_any_mutation(routing_env, monkeypatch, name, args, context):
    from agent.delegation_context import delegated_child_context
    from contextlib import nullcontext

    before = snapshot(routing_env)
    if context == "worker":
        monkeypatch.setenv("HERMES_KANBAN_TASK", routing_env[5])
    elif context == "child-process":
        monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", "1")
    elif context == "not-opted-in":
        monkeypatch.setattr(routing_env[2], "load_config", lambda: {})
    with delegated_child_context("fixture-child") if context == "child" else nullcontext():
        result = call(routing_env, name, **args)
    assert "error" in result, result
    assert snapshot(routing_env) == before


@pytest.mark.parametrize("args", [
    {}, {"model": ""}, {"model": "   "}, {"model": []}, {"provider": "provider"},
    {"clear_override": "true"}, {"clear_override": 1},
    {"clear_override": True, "model": "model"},
    {"clear_override": True, "provider": "provider"},
    {"model": "model", "provider": []},
])
def test_ambiguous_model_arguments_do_not_clear_existing_values(routing_env, args):
    before = snapshot(routing_env)
    result = call(routing_env, "kanban_set_model", **args)
    assert "error" in result, result
    assert snapshot(routing_env) == before


@pytest.mark.parametrize("assignee", [None, "", "   ", [], 42])
def test_reassign_requires_nonempty_string(routing_env, assignee):
    before = snapshot(routing_env)
    result = call(routing_env, "kanban_reassign", assignee=assignee)
    assert "error" in result
    assert snapshot(routing_env) == before


@pytest.mark.parametrize("name,args", [
    ("kanban_reassign", {"assignee": "new-reviewer"}),
    ("kanban_set_model", {"clear_override": True}),
])
@pytest.mark.parametrize("protection", ["running", "done", "archived", "claim", "run", "linked", "marker", "workflow"])
def test_protected_targets_remain_unchanged(routing_env, name, args, protection):
    kb, kbc, _, _, board, tid = routing_env
    with kbc.connect_closing(board=board) as conn:
        if protection in {"running", "done", "archived"}:
            conn.execute("UPDATE tasks SET status=? WHERE id=?", (protection, tid))
        elif protection == "claim":
            conn.execute("UPDATE tasks SET claim_lock='fixture:claim' WHERE id=?", (tid,))
        elif protection == "run":
            conn.execute("UPDATE tasks SET current_run_id=12345 WHERE id=?", (tid,))
        elif protection == "linked":
            parent = kb.create_task(conn, title="parent", assignee="owner")
            kb.link_tasks(conn, parent_id=parent, child_id=tid)
        elif protection == "marker":
            conn.execute("UPDATE tasks SET body='[supervised-delivery:v1] sealed' WHERE id=?", (tid,))
        else:
            conn.execute("UPDATE tasks SET workflow_template_id='controlled' WHERE id=?", (tid,))
        conn.commit()
    before = snapshot(routing_env)
    result = call(routing_env, name, **args)
    assert "error" in result, result
    assert snapshot(routing_env) == before


@pytest.mark.parametrize("name,args,native_name", [
    ("kanban_reassign", {"assignee": "new-reviewer"}, "reassign_task"),
    ("kanban_set_model", {"clear_override": True}, "set_model_override"),
])
@pytest.mark.parametrize("new_status", ["running", "done"])
def test_second_connection_race_is_rejected_inside_native_transaction(routing_env, monkeypatch, name, args, native_name, new_status):
    kb, kbc, _, _, board, tid = routing_env
    native = getattr(kb, native_name)
    before_row, before_events = snapshot(routing_env)

    def race(conn, target, *values, **kwargs):
        with kbc.connect_closing(board=board) as other:
            other.execute("UPDATE tasks SET status=? WHERE id=?", (new_status, tid))
            other.commit()
        return native(conn, target, *values, **kwargs)

    monkeypatch.setattr(kb, native_name, race)
    result = call(routing_env, name, **args)
    assert "error" in result, result
    row, events = snapshot(routing_env)
    assert row["status"] == new_status
    for field in ("assignee", "model_override", "provider_override", "reasoning_effort"):
        assert row[field] == before_row[field]
    assert events == before_events


def test_explicit_board_never_changes_same_id_on_another_board(routing_env):
    kb, kbc, _, registry, board, tid = routing_env
    before = snapshot(routing_env)
    result = json.loads(registry.dispatch("kanban_reassign", {
        "task_id": tid, "board": "different-board", "assignee": "new-reviewer",
    }))
    assert "error" in result
    assert snapshot(routing_env) == before


def test_schema_gating_matches_existing_controller_tools(routing_env, monkeypatch):
    from tools.registry import invalidate_check_fn_cache
    from toolsets import resolve_toolset
    registry = routing_env[3]
    tools = set(resolve_toolset("hermes-cli"))

    def names():
        invalidate_check_fn_cache()
        return {d["function"]["name"] for d in registry.get_definitions(tools, quiet=True)}

    expected = {"kanban_reassign", "kanban_set_model"}
    assert expected <= names()
    monkeypatch.setenv("HERMES_KANBAN_TASK", routing_env[5])
    assert not expected & names()
    monkeypatch.delenv("HERMES_KANBAN_TASK")
    monkeypatch.setattr(routing_env[2], "load_config", lambda: {})
    assert not expected & names()


def test_review_resumption_preserves_original_implementer_and_real_review_route(routing_env, monkeypatch):
    kb, kbc, _, _, board, tid = routing_env

    def worker_call(name, **args):
        row, _ = snapshot(routing_env)
        assert row["status"] == "running" and row["current_run_id"] is not None
        with monkeypatch.context() as worker:
            worker.setenv("HERMES_KANBAN_TASK", tid)
            worker.setenv("HERMES_KANBAN_RUN_ID", str(row["current_run_id"]))
            worker.setenv("HERMES_PROFILE", row["assignee"])
            return call(routing_env, name, **args)
    with kbc.connect_closing(board=board) as conn:
        kb.assign_task(conn, tid, "original-researcher")
        kb.unblock_task(conn, tid)
        kb.claim_task(conn, tid)
    result = worker_call("kanban_request_review", summary="fixture research finished", reviewer="old-reviewer")
    assert result.get("ok") is True, result
    with kbc.connect_closing(board=board) as conn:
        assert kb.claim_review_task(conn, tid) is not None
    result = worker_call("kanban_block", reason="reviewer missing lifecycle tool", kind="capability")
    assert result.get("ok") is True, result
    _, old_events = snapshot(routing_env)
    old_requests = [e for e in old_events if e["kind"] == "review_requested"]
    assert old_requests
    assert call(routing_env, "kanban_reassign", assignee="controller").get("ok") is True
    assert call(routing_env, "kanban_set_model", clear_override=True).get("ok") is True
    result = call(routing_env, "kanban_unblock")
    assert result.get("ok") is True and result["status"] == "review", result
    row, events = snapshot(routing_env)
    assert row["assignee"] == "controller"
    assert [e for e in events if e["kind"] == "review_requested"] == old_requests
    with kbc.connect_closing(board=board) as conn:
        assert kb.claim_review_task(conn, tid) is not None
    result = worker_call("kanban_request_changes", reason="fixture independent review requires corrections")
    assert result.get("ok") is True, result
    row, events = snapshot(routing_env)
    assert row["assignee"] == "original-researcher" and row["status"] == "ready"
    verdict = json.loads([e for e in events if e["kind"] == "changes_requested"][-1]["payload"])
    assert verdict["reviewer"] == "controller"


def test_native_defaults_keep_existing_running_model_behavior(routing_env):
    kb, kbc, _, _, board, tid = routing_env
    with kbc.connect_closing(board=board) as conn:
        kb.unblock_task(conn, tid)
        kb.claim_task(conn, tid)
        assert kb.set_model_override(conn, tid, "next-run-model", "provider") is True
        row = kb.get_task(conn, tid)
        assert row.status == "running" and row.model_override == "next-run-model"


def test_strict_native_reassign_never_reclaims_first(routing_env):
    before = snapshot(routing_env)
    kb, kbc, _, _, board, tid = routing_env
    with kbc.connect_closing(board=board) as conn:
        with pytest.raises(ValueError, match="cannot reclaim"):
            kb.reassign_task(conn, tid, "new-reviewer", reclaim_first=True, strict_recovery=True)
    assert snapshot(routing_env) == before
