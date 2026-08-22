"""Gateway contract for launch-profile-only MCP client RPCs."""

from dataclasses import asdict
from unittest.mock import MagicMock, patch

import pytest

from tools.mcp_client_access import MCPClientAccessError, MCPClientCallResult


@pytest.fixture()
def server():
    with patch.dict("sys.modules", {
        "hermes_constants": MagicMock(get_hermes_home=MagicMock(return_value="/tmp/hermes_test")),
        "hermes_cli.env_loader": MagicMock(), "hermes_cli.banner": MagicMock(),
        "hermes_state": MagicMock(),
    }):
        import tui_gateway.server as mod
    return mod


@pytest.mark.parametrize("method", ["mcp.client.status", "mcp.client.tools", "mcp.client.call"])
def test_mcp_client_methods_are_registered_and_pool_routed(server, method):
    assert method in server._methods
    assert method in server._LONG_HANDLERS


def test_profile_mismatch_fails_before_service_lookup(server):
    with (
        patch("hermes_cli.profiles.get_active_profile_name", return_value="researcher"),
        patch("tools.mcp_client_access.load_mcp_client_access_policy") as policy,
        patch("tools.mcp_client_access.call_mcp_client_tool") as call,
    ):
        response = server._methods["mcp.client.call"](
            "1", {"server": "convexopps", "tool": "whoami", "arguments": {}, "profile": "other"}
        )
    assert response["error"]["code"] == 4601
    policy.assert_not_called()
    call.assert_not_called()


def test_call_returns_exact_contract(server):
    result = MCPClientCallResult("mcpui_1", "convexopps", "whoami", True, 12, '{"result":"ok"}', False)
    with (
        patch("hermes_cli.profiles.get_active_profile_name", return_value="researcher"),
        patch("tools.mcp_client_access.call_mcp_client_tool", return_value=result) as call,
    ):
        response = server._methods["mcp.client.call"](
            "1", {"server": "convexopps", "tool": "whoami", "arguments": {}, "profile": "researcher"}
        )
    assert response["result"] == asdict(result)
    call.assert_called_once_with("convexopps", "whoami", {})


def test_call_holds_reload_lock_through_policy_and_dispatch(server):
    result = MCPClientCallResult("mcpui_1", "convexopps", "whoami", True, 12, "ok", False)

    def assert_reload_stable(*_args):
        assert server._mcp_reload_lock.locked()
        return result

    with (
        patch("hermes_cli.profiles.get_active_profile_name", return_value="researcher"),
        patch("tools.mcp_client_access.call_mcp_client_tool", side_effect=assert_reload_stable),
    ):
        response = server._methods["mcp.client.call"](
            "1", {"server": "convexopps", "tool": "whoami", "arguments": {}, "profile": "researcher"}
        )
    assert response["result"] == asdict(result)


@pytest.mark.parametrize(
    "method,params",
    [
        ("mcp.client.status", {"server": "x" * 129, "profile": "researcher"}),
        ("mcp.client.tools", {"server": "convex*", "profile": "researcher"}),
        (
            "mcp.client.call",
            {"server": "convexopps", "tool": "who?ami", "arguments": {}, "profile": "researcher"},
        ),
    ],
)
def test_invalid_exact_names_fail_before_service_lookup(server, method, params):
    with (
        patch("hermes_cli.profiles.get_active_profile_name", return_value="researcher"),
        patch("tools.mcp_client_access.get_mcp_client_status") as status,
        patch("tools.mcp_client_access.list_mcp_client_tools") as tools,
        patch("tools.mcp_client_access.call_mcp_client_tool") as call,
    ):
        response = server._methods[method]("1", params)
    assert response["error"]["code"] == 4600
    status.assert_not_called()
    tools.assert_not_called()
    call.assert_not_called()


@pytest.mark.parametrize(
    "symbol,numeric",
    [("MCP_CLIENT_INVALID_PARAMS", 4600), ("MCP_CLIENT_DISABLED", 4602),
     ("MCP_CLIENT_SERVER_UNAVAILABLE", 4603), ("MCP_CLIENT_TOOL_DENIED", 4604),
     ("MCP_CLIENT_NOT_READ_ONLY", 4605), ("MCP_CLIENT_INTERACTIVE_RUNTIME", 4606),
     ("MCP_CLIENT_BUSY", 4607), ("MCP_CLIENT_RESULT_CONTRACT", 4608)],
)
def test_call_maps_stable_service_errors(server, symbol, numeric):
    with (
        patch("hermes_cli.profiles.get_active_profile_name", return_value="researcher"),
        patch("tools.mcp_client_access.call_mcp_client_tool", side_effect=MCPClientAccessError(symbol, "safe")),
    ):
        response = server._methods["mcp.client.call"](
            "1", {"server": "convexopps", "tool": "whoami", "arguments": {}, "profile": "researcher"}
        )
    assert response["error"] == {"code": numeric, "message": "safe"}


def test_status_and_tools_are_introspection_only(server):
    status = {"server": "convexopps", "configured": True, "connected": True}
    tools = [{"name": "whoami", "description": "Identity", "input_schema": {}}]
    with (
        patch("hermes_cli.profiles.get_active_profile_name", return_value="researcher"),
        patch("tools.mcp_client_access.get_mcp_client_status", return_value=status) as get_status,
        patch("tools.mcp_client_access.list_mcp_client_tools", return_value=tools) as get_tools,
        patch("tools.mcp_client_access.call_mcp_client_tool") as call,
    ):
        status_response = server._methods["mcp.client.status"]("1", {"server": "convexopps", "profile": "researcher"})
        tools_response = server._methods["mcp.client.tools"]("2", {"server": "convexopps", "profile": "researcher"})
    assert status_response["result"] == status
    assert tools_response["result"] == {"server": "convexopps", "tools": tools}
    get_status.assert_called_once_with("convexopps")
    get_tools.assert_called_once_with("convexopps")
    call.assert_not_called()
