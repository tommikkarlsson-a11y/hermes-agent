"""Contract tests for the direct POSIX stdio MCP child watchdog."""

import os
import sys

import pytest

from tools import mcp_stdio_watchdog, mcp_tool


def test_is_orphaned_is_false_while_direct_parent_is_unchanged():
    original_ppid = 1234

    assert mcp_stdio_watchdog._is_orphaned(
        original_ppid,
        getppid=lambda: original_ppid,
    ) is False


@pytest.mark.skipif(os.name != "posix", reason="watchdog wrapping is POSIX-only")
def test_wrap_command_uses_stable_parent_pid_and_preserves_command_tail():
    parent_pid = os.getpid()
    command = "/opt/hermes/bin/mcp-server"
    command_args = ["--label", "value with spaces", "--", "literal-tail"]

    wrapped_command, wrapped_args = mcp_tool._wrap_command_with_watchdog(
        command,
        command_args,
    )

    assert wrapped_command == sys.executable
    assert wrapped_args == [
        os.path.join(os.path.dirname(mcp_tool.__file__), "mcp_stdio_watchdog.py"),
        "--ppid",
        str(parent_pid),
        "--",
        command,
        *command_args,
    ]
    assert "--create-time" not in wrapped_args


@pytest.mark.parametrize(
    ("tracked_pids", "live_pids", "expected_dead"),
    [
        (set(), set(), False),
        ({101}, {101}, False),
        ({101, 202}, {202}, False),
        ({101, 202}, set(), True),
    ],
)
def test_stdio_children_dead_requires_every_tracked_child_to_exit(
    monkeypatch,
    tracked_pids,
    live_pids,
    expected_dead,
):
    task = mcp_tool.MCPServerTask("test-server")
    task._stdio_child_pids = tracked_pids
    monkeypatch.setattr(
        "psutil.pid_exists",
        lambda pid: pid in live_pids,
    )

    assert task._stdio_children_dead() is expected_dead


def test_stdio_children_dead_is_not_applied_to_http(monkeypatch):
    task = mcp_tool.MCPServerTask("test-server")
    task._config = {"url": "https://example.invalid/mcp"}
    task._stdio_child_pids = {101}
    monkeypatch.setattr("psutil.pid_exists", lambda _pid: False)

    assert task._stdio_children_dead() is False
