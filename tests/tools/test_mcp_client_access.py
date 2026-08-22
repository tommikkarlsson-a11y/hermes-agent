"""Behavioral tests for the default-off MCP client access policy."""

import json
import logging
import threading

from unittest.mock import patch

import pytest

from tools.mcp_client_access import (
    MCPClientAccessError,
    MCPClientAccessPolicy,
    _is_mcp_client_runtime_noninteractive,
    call_mcp_client_tool,
    list_mcp_client_tools,
    load_mcp_client_access_policy,
)
from tools.mcp_tool import MCPToolProvenance
from tools.registry import registry


def _config(client_access=None):
    server = {} if client_access is None else {"client_access": client_access}
    return {"mcp_servers": {"convexopps": server}}


@pytest.mark.parametrize(
    "block",
    [None, {}, {"enabled": False, "tools": ["whoami"]}, {"enabled": 1, "tools": ["whoami"]}],
)
def test_policy_is_default_off(block):
    with patch("hermes_cli.config.load_config", return_value=_config(block)):
        policy = load_mcp_client_access_policy("convexopps")
    assert policy.enabled is False
    assert policy.ordered_tools == ()
    assert policy.allows("whoami") is False


def test_valid_policy_preserves_order_and_deduplicates_first_occurrence():
    block = {"enabled": True, "tools": ["search_companies", "whoami", "search_companies"]}
    with patch("hermes_cli.config.load_config", return_value=_config(block)):
        policy = load_mcp_client_access_policy("convexopps")
    assert policy.enabled is True
    assert policy.ordered_tools == ("search_companies", "whoami")
    assert policy.allows("whoami") is True
    assert policy.allows("WhoAmI") is False


@pytest.mark.parametrize("tools", ["whoami", {}, None, 42])
def test_invalid_tools_container_fails_closed(tools):
    block = {"enabled": True, "tools": tools}
    with patch("hermes_cli.config.load_config", return_value=_config(block)):
        policy = load_mcp_client_access_policy("convexopps")
    assert policy.enabled is True
    assert policy.ordered_tools == ()


def test_invalid_entries_are_rejected_without_affecting_valid_entries():
    block = {
        "enabled": True,
        "tools": ["whoami", "", "   ", 7, "search*", "search?", "tool[name]", "x" * 129],
    }
    with patch("hermes_cli.config.load_config", return_value=_config(block)):
        policy = load_mcp_client_access_policy("convexopps")
    assert policy.ordered_tools == ("whoami",)


def test_reads_current_launch_profile_config_without_override(monkeypatch):
    monkeypatch.setenv("HERMES_HOME", "/launch-profile-home")
    observed = {}

    def fake_load_config(*args, **kwargs):
        observed["args"] = args
        observed["kwargs"] = kwargs
        observed["home"] = __import__("os").environ.get("HERMES_HOME")
        return _config({"enabled": True, "tools": ["whoami"]})

    with patch("hermes_cli.config.load_config", side_effect=fake_load_config):
        policy = load_mcp_client_access_policy("convexopps")
    assert policy.ordered_tools == ("whoami",)
    assert observed == {"args": (), "kwargs": {}, "home": "/launch-profile-home"}


def test_catalog_is_policy_ordered_exact_provenance_read_only_intersection():
    names = ["test_mcp_console_whoami", "test_mcp_console_search", "test_mcp_console_utility"]
    schemas = {
        names[0]: {"name": names[0], "description": "Identity", "parameters": {"type": "object"}},
        names[1]: {"name": names[1], "description": "Search", "parameters": {"type": "object"}},
        names[2]: {"name": names[2], "description": "Utility", "parameters": {"type": "object"}},
    }
    for name in names:
        registry.register(name=name, toolset="mcp-convexopps", schema=schemas[name], handler=lambda args: "ok")
    provenance = {
        names[0]: MCPToolProvenance("convexopps", "whoami"),
        names[1]: MCPToolProvenance("other-server", "search_companies"),
        # Generated utility intentionally has no raw provenance.
    }
    block = {"enabled": True, "tools": ["search_companies", "whoami", "missing"]}
    try:
        with (
            patch("hermes_cli.config.load_config", return_value=_config(block)),
            patch("tools.mcp_tool.get_mcp_tool_provenance", side_effect=provenance.get),
            patch("tools.mcp_tool.is_mcp_tool_read_only", side_effect=lambda server, tool: tool == "whoami"),
        ):
            catalog = list_mcp_client_tools("convexopps")
    finally:
        for name in names:
            registry.deregister(name)
    assert catalog == [{"name": "whoami", "description": "Identity", "input_schema": {"type": "object"}}]


def test_catalog_rejects_oversized_server_controlled_input_schema():
    name = "test_mcp_console_oversized_schema"
    registry.register(
        name=name,
        toolset="mcp-convexopps",
        schema={
            "name": name,
            "description": "Identity",
            "parameters": {"type": "object", "description": "x" * 70_000},
        },
        handler=lambda args: "ok",
    )
    block = {"enabled": True, "tools": ["whoami"]}
    try:
        with (
            patch("hermes_cli.config.load_config", return_value=_config(block)),
            patch(
                "tools.mcp_tool.get_mcp_tool_provenance",
                side_effect=lambda registry_name: MCPToolProvenance("convexopps", "whoami")
                if registry_name == name else None,
            ),
            patch("tools.mcp_tool.is_mcp_tool_read_only", return_value=True),
        ):
            with pytest.raises(MCPClientAccessError) as exc:
                list_mcp_client_tools("convexopps")
    finally:
        registry.deregister(name)
    assert exc.value.code == "MCP_CLIENT_RESULT_CONTRACT"
    assert "input schema" in str(exc.value)


@pytest.mark.parametrize(
    "sampling,elicitation",
    [({}, {"enabled": False}), ({"enabled": True}, {"enabled": False}),
     ({"enabled": False}, {}), ({"enabled": False}, {"enabled": True})],
)
def test_noninteractive_requires_explicit_false_false(sampling, elicitation):
    config = _config({"enabled": True, "tools": ["whoami"]})
    config["mcp_servers"]["convexopps"].update(
        {"sampling": sampling, "elicitation": elicitation}
    )
    with (
        patch("hermes_cli.config.load_config", return_value=config),
        patch("tools.mcp_tool.get_mcp_client_runtime_state", return_value={"live": False}),
    ):
        assert _is_mcp_client_runtime_noninteractive("convexopps") is False


def test_noninteractive_rejects_stale_live_handlers_but_allows_lazy_server():
    config = _config({"enabled": True, "tools": ["whoami"]})
    config["mcp_servers"]["convexopps"].update(
        {"sampling": {"enabled": False}, "elicitation": {"enabled": False}}
    )
    with patch("hermes_cli.config.load_config", return_value=config):
        with patch(
            "tools.mcp_tool.get_mcp_client_runtime_state",
            return_value={"live": True, "sampling_handler": True, "elicitation_handler": False},
        ):
            assert _is_mcp_client_runtime_noninteractive("convexopps") is False
        with patch("tools.mcp_tool.get_mcp_client_runtime_state", return_value={"live": False}):
            assert _is_mcp_client_runtime_noninteractive("convexopps") is True


def _call_patches(registry_name, raw_name="whoami", *, read_only=True, interactive=False):
    config = _config({"enabled": True, "tools": [raw_name]})
    config["mcp_servers"]["convexopps"].update(
        {"sampling": {"enabled": False}, "elicitation": {"enabled": False}}
    )
    return (
        patch("hermes_cli.config.load_config", return_value=config),
        patch(
            "tools.mcp_tool.get_mcp_tool_provenance",
            side_effect=lambda name: MCPToolProvenance("convexopps", raw_name)
            if name == registry_name else None,
        ),
        patch("tools.mcp_tool.is_mcp_tool_read_only", return_value=read_only),
        patch(
            "tools.mcp_tool.get_mcp_client_runtime_state",
            return_value={"live": True, "sampling_handler": interactive, "elicitation_handler": False},
        ),
    )


def test_call_dispatches_exact_registry_handler_and_normalizes_result():
    name = "test_mcp_client_call_whoami"
    calls = []
    registry.register(
        name=name, toolset="mcp-convexopps",
        schema={"name": name, "description": "Identity", "parameters": {"type": "object"}},
        handler=lambda args: calls.append(args) or json.dumps({"result": "ok"}),
    )
    try:
        patches = _call_patches(name)
        with patches[0], patches[1], patches[2], patches[3]:
            result = call_mcp_client_tool("convexopps", "whoami", {"include_email": False})
    finally:
        registry.deregister(name)
    assert calls == [{"include_email": False}]
    assert result.ok is True
    assert result.result_text == '{"result": "ok"}'
    assert result.truncated is False


@pytest.mark.parametrize(
    "arguments",
    [[], "bad", None, {"value": float("nan")}, {"value": "x" * 65_537}],
)
def test_invalid_or_oversized_arguments_never_dispatch(arguments):
    with pytest.raises(MCPClientAccessError) as exc:
        call_mcp_client_tool("convexopps", "whoami", arguments)
    assert exc.value.code == "MCP_CLIENT_INVALID_PARAMS"


def test_policy_readonly_and_interactive_denials_never_dispatch():
    name = "test_mcp_client_denial"
    called = []
    registry.register(
        name=name, toolset="mcp-convexopps", schema={"name": name, "parameters": {}},
        handler=lambda args: called.append(args) or "ok",
    )
    try:
        patches = _call_patches(name, read_only=False)
        with patches[0], patches[1], patches[2], patches[3]:
            with pytest.raises(MCPClientAccessError) as exc:
                call_mcp_client_tool("convexopps", "whoami", {})
        assert exc.value.code == "MCP_CLIENT_NOT_READ_ONLY"
        patches = _call_patches(name, interactive=True)
        with patches[0], patches[1], patches[2], patches[3]:
            with pytest.raises(MCPClientAccessError) as exc:
                call_mcp_client_tool("convexopps", "whoami", {})
        assert exc.value.code == "MCP_CLIENT_INTERACTIVE_RUNTIME"
    finally:
        registry.deregister(name)
    assert called == []


def test_policy_revocation_is_rechecked_immediately_before_dispatch():
    name = "test_mcp_client_policy_revoked"
    called = []
    registry.register(
        name=name,
        toolset="mcp-convexopps",
        schema={"name": name, "parameters": {}},
        handler=lambda args: called.append(args) or "ok",
    )
    try:
        with (
            patch(
                "tools.mcp_client_access.load_mcp_client_access_policy",
                side_effect=[
                    MCPClientAccessPolicy(True, ("whoami",)),
                    MCPClientAccessPolicy(False, ()),
                ],
            ),
            patch("tools.mcp_client_access._find_registry_tool", return_value=name),
            patch("tools.mcp_tool.is_mcp_tool_read_only", return_value=True),
            patch("tools.mcp_client_access._is_mcp_client_runtime_noninteractive", return_value=True),
        ):
            with pytest.raises(MCPClientAccessError) as exc:
                call_mcp_client_tool("convexopps", "whoami", {})
    finally:
        registry.deregister(name)
    assert exc.value.code == "MCP_CLIENT_DISABLED"
    assert called == []


def test_concurrent_second_call_is_busy_without_queueing():
    name = "test_mcp_client_busy"
    entered = threading.Event()
    release = threading.Event()

    def handler(args):
        entered.set()
        assert release.wait(5)
        return '{"result":"ok"}'

    registry.register(name=name, toolset="mcp-convexopps", schema={"name": name, "parameters": {}}, handler=handler)
    patches = _call_patches(name)
    first = []
    try:
        with patches[0], patches[1], patches[2], patches[3]:
            thread = threading.Thread(target=lambda: first.append(call_mcp_client_tool("convexopps", "whoami", {})))
            thread.start()
            assert entered.wait(2)
            with pytest.raises(MCPClientAccessError) as exc:
                call_mcp_client_tool("convexopps", "whoami", {})
            assert exc.value.code == "MCP_CLIENT_BUSY"
            release.set()
            thread.join(5)
    finally:
        release.set()
        registry.deregister(name)
    assert len(first) == 1


def test_result_is_utf8_safely_truncated_to_one_mibibyte():
    name = "test_mcp_client_large_result"
    payload = "🙂" * 300_000
    registry.register(name=name, toolset="mcp-convexopps", schema={"name": name, "parameters": {}}, handler=lambda args: payload)
    try:
        patches = _call_patches(name)
        with patches[0], patches[1], patches[2], patches[3]:
            result = call_mcp_client_tool("convexopps", "whoami", {})
    finally:
        registry.deregister(name)
    assert result.truncated is True
    assert result.result_text.endswith("\n… [truncated]")
    assert len(result.result_text.encode("utf-8")) <= 1_048_576


def test_result_strips_backend_session_credential_metadata():
    name = "test_mcp_client_session_metadata"
    payload = {
        "result": {"company": "NOKIA"},
        "structuredContent": {
            "company": "NOKIA",
            "_meta": {
                "schema_version": "5",
                "session": {
                    "token_expires_at": "2030-01-01T00:00:00Z",
                    "refresh_required_before": "2029-12-31T23:00:00Z",
                    "state": "ok",
                },
            },
        },
    }
    registry.register(
        name=name,
        toolset="mcp-convexopps",
        schema={"name": name, "parameters": {}},
        handler=lambda args: json.dumps(payload),
    )
    try:
        patches = _call_patches(name)
        with patches[0], patches[1], patches[2], patches[3]:
            result = call_mcp_client_tool("convexopps", "whoami", {})
    finally:
        registry.deregister(name)
    decoded = json.loads(result.result_text)
    assert decoded["result"] == {"company": "NOKIA"}
    assert decoded["structuredContent"]["company"] == "NOKIA"
    assert decoded["structuredContent"]["_meta"] == {"schema_version": "5"}
    assert "token_expires_at" not in result.result_text
    assert "refresh_required_before" not in result.result_text


def test_exception_credentials_are_redacted_and_audit_log_omits_arguments(caplog):
    name = "test_mcp_client_secret_error"
    canary = "mcp-client-canary-7d3f9a1c5e"

    def handler(args):
        raise RuntimeError(f"upstream failed Bearer {canary} token={canary}")

    registry.register(name=name, toolset="mcp-convexopps", schema={"name": name, "parameters": {}}, handler=handler)
    try:
        patches = _call_patches(name)
        with caplog.at_level(logging.INFO), patches[0], patches[1], patches[2], patches[3]:
            result = call_mcp_client_tool("convexopps", "whoami", {"private_argument": canary})
    finally:
        registry.deregister(name)
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert canary not in result.result_text
    assert canary not in logged
    assert "private_argument" not in logged
    assert result.ok is False
