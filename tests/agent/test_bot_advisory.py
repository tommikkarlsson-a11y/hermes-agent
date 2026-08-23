from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent import bot_advisory


@pytest.mark.parametrize(
    "message",
    [
        f"{bot_advisory.EXPLICIT_ADVISORY_MARKER}\nMessage from 🤖 coder (@coder): FYI",
        "Message from 🤖 coder (@coder): legacy FYI",
    ],
)
def test_explicit_and_legacy_markers_are_advisory(message):
    assert bot_advisory.is_advisory_message(message) is True


@pytest.mark.parametrize(
    "message",
    [
        "Human quoted Message from 🤖 coder (@coder): in the middle",
        "Message from a colleague: ordinary human text",
        "",
    ],
)
def test_non_markers_do_not_activate_advisory_mode(message):
    assert bot_advisory.is_advisory_message(message) is False


def test_turn_marker_is_recomputed_and_cleared_each_turn():
    agent = SimpleNamespace()
    assert bot_advisory.mark_advisory_turn(
        agent, "Message from 🤖 coder (@coder): FYI"
    ) is True
    assert agent._bot_advisory_turn is True
    assert bot_advisory.is_advisory_turn_context() is True

    assert bot_advisory.mark_advisory_turn(agent, "ordinary user request") is False
    assert agent._bot_advisory_turn is False
    assert bot_advisory.is_advisory_turn_context() is False


def test_advisory_turn_is_not_dispatcher_owned():
    from agent.delegation_context import (
        is_dispatcher_owned_worker_context,
        is_non_dispatcher_child_process_context,
    )

    agent = SimpleNamespace()
    bot_advisory.mark_advisory_turn(agent, "Message from 🤖 coder (@coder): FYI")
    assert is_dispatcher_owned_worker_context() is False
    assert is_non_dispatcher_child_process_context() is True
    bot_advisory.mark_advisory_turn(agent, "ordinary user request")


@pytest.mark.parametrize("tool_name", sorted(bot_advisory.READ_ONLY_TOOL_ALLOWLIST))
def test_fixed_builtin_read_only_allowlist_is_allowed(tool_name):
    agent = SimpleNamespace(_bot_advisory_turn=True)
    assert bot_advisory.advisory_tool_block(agent, tool_name) is None


@pytest.mark.parametrize(
    "tool_name",
    [
        "terminal",
        "execute_code",
        "write_file",
        "patch",
        "memory",
        "kanban_create_self_owned",
        "message_agent",
        "tool_call",
        "tool_search",
        "mcp__github__get_issue",
        "unknown_future_tool",
    ],
)
def test_advisory_turn_denies_mutating_mcp_peer_indirect_and_unknown_tools(tool_name):
    agent = SimpleNamespace(_bot_advisory_turn=True)
    block = bot_advisory.advisory_tool_block(agent, tool_name)
    assert block is not None
    assert "advisory" in block.lower()
    assert tool_name in block


def test_normal_turn_keeps_existing_tool_behavior():
    agent = SimpleNamespace(_bot_advisory_turn=False)
    assert bot_advisory.advisory_tool_block(agent, "terminal") is None


def test_shared_tool_middleware_blocks_before_execute(monkeypatch):
    import json

    from agent import tool_executor

    agent = SimpleNamespace(
        _bot_advisory_turn=True,
        session_id="bot-chat",
        _current_turn_id="turn-1",
        _current_api_request_id="request-1",
    )
    monkeypatch.setattr(
        tool_executor, "_emit_terminal_post_tool_call", lambda *args, **kwargs: None
    )

    executed = False

    def execute(_args):
        nonlocal executed
        executed = True
        return "should not run"

    result = tool_executor._run_agent_tool_execution_middleware(
        agent,
        function_name="terminal",
        function_args={"command": "touch forbidden"},
        effective_task_id="",
        tool_call_id="tool-1",
        execute=execute,
    )

    assert executed is False
    assert result.blocked is True
    assert "advisory" in json.loads(result.result)["error"].lower()
