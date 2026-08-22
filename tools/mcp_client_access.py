"""Default-off policy for trusted local MCP client surfaces.

This module grants a narrow execution capability over Hermes's existing MCP
registry.  It does not create or own an MCP transport or OAuth client.
"""

from dataclasses import dataclass
import json
import logging
import threading
import time
import uuid


_MAX_NAME_CHARS = 128
_GLOB_CHARS = frozenset("*?[")
_MAX_DESCRIPTION_CHARS = 4096
_MAX_INPUT_SCHEMA_BYTES = 65_536
_MAX_ARGUMENT_BYTES = 65_536
_MAX_RESULT_BYTES = 1_048_576
_RESULT_TRUNCATION_MARKER = "\n… [truncated]"
logger = logging.getLogger(__name__)
_client_locks_guard = threading.Lock()
_client_locks: dict[str, threading.Lock] = {}


class MCPClientAccessError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class MCPClientCallResult:
    request_id: str
    server: str
    tool: str
    ok: bool
    duration_ms: int
    result_text: str
    truncated: bool


@dataclass(frozen=True)
class MCPClientAccessPolicy:
    enabled: bool
    ordered_tools: tuple[str, ...]

    def allows(self, raw_tool_name: str) -> bool:
        return self.enabled and raw_tool_name in self.ordered_tools


def _valid_exact_name(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and len(value) <= _MAX_NAME_CHARS
        and not any(char in value for char in _GLOB_CHARS)
    )


def load_mcp_client_access_policy(server_name: str) -> MCPClientAccessPolicy:
    """Load one server's exact ordered allowlist from launch-profile config."""
    from hermes_cli.config import load_config

    config = load_config()
    servers = config.get("mcp_servers") if isinstance(config, dict) else None
    server = servers.get(server_name) if isinstance(servers, dict) else None
    block = server.get("client_access") if isinstance(server, dict) else None
    if not isinstance(block, dict) or block.get("enabled") is not True:
        return MCPClientAccessPolicy(enabled=False, ordered_tools=())

    configured_tools = block.get("tools")
    if not isinstance(configured_tools, list):
        return MCPClientAccessPolicy(enabled=True, ordered_tools=())

    ordered: list[str] = []
    seen: set[str] = set()
    for raw_name in configured_tools:
        if _valid_exact_name(raw_name) and raw_name not in seen:
            ordered.append(raw_name)
            seen.add(raw_name)
    return MCPClientAccessPolicy(enabled=True, ordered_tools=tuple(ordered))


def _is_mcp_client_runtime_noninteractive(server_name: str) -> bool:
    """Require explicit false/false config and no stale live handlers."""
    from hermes_cli.config import load_config
    from tools.mcp_tool import get_mcp_client_runtime_state

    config = load_config()
    servers = config.get("mcp_servers") if isinstance(config, dict) else None
    server = servers.get(server_name) if isinstance(servers, dict) else None
    if not isinstance(server, dict):
        return False
    sampling = server.get("sampling")
    elicitation = server.get("elicitation")
    if not isinstance(sampling, dict) or sampling.get("enabled") is not False:
        return False
    if not isinstance(elicitation, dict) or elicitation.get("enabled") is not False:
        return False

    runtime = get_mcp_client_runtime_state(server_name)
    if runtime.get("live") is True:
        return (
            runtime.get("sampling_handler") is False
            and runtime.get("elicitation_handler") is False
        )
    return True


def list_mcp_client_tools(server_name: str) -> list[dict]:
    """Return the policy-ordered, exact-provenance, read-only catalog."""
    from tools.mcp_tool import get_mcp_tool_provenance, is_mcp_tool_read_only
    from tools.registry import registry

    policy = load_mcp_client_access_policy(server_name)
    if not policy.enabled:
        return []

    entries_by_raw_name: dict[str, str] = {}
    for registry_name in registry.get_all_tool_names():
        provenance = get_mcp_tool_provenance(registry_name)
        if provenance is None or provenance.server_name != server_name:
            continue
        if provenance.raw_tool_name not in policy.ordered_tools:
            continue
        entries_by_raw_name[provenance.raw_tool_name] = registry_name

    tools: list[dict] = []
    for raw_name in policy.ordered_tools:
        registry_name = entries_by_raw_name.get(raw_name)
        if registry_name is None or not is_mcp_tool_read_only(server_name, raw_name):
            continue
        schema = registry.get_schema(registry_name)
        if not isinstance(schema, dict):
            continue
        description = schema.get("description")
        parameters = schema.get("parameters")
        input_schema = parameters if isinstance(parameters, dict) else {}
        try:
            serialized_schema = json.dumps(
                input_schema,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError, RecursionError):
            raise MCPClientAccessError(
                "MCP_CLIENT_RESULT_CONTRACT", "MCP input schema is not finite JSON"
            ) from None
        if len(serialized_schema.encode("utf-8")) > _MAX_INPUT_SCHEMA_BYTES:
            raise MCPClientAccessError(
                "MCP_CLIENT_RESULT_CONTRACT", "MCP input schema exceeds 64 KiB"
            )
        tools.append(
            {
                "name": raw_name,
                "description": (
                    description[:_MAX_DESCRIPTION_CHARS]
                    if isinstance(description, str)
                    else ""
                ),
                "input_schema": json.loads(serialized_schema),
            }
        )
    return tools


def get_mcp_client_status(server_name: str) -> dict:
    """Return a credential-free snapshot without connecting the server."""
    from hermes_cli.config import load_config
    from tools.mcp_tool import get_mcp_status

    config = load_config()
    servers = config.get("mcp_servers") if isinstance(config, dict) else None
    configured = isinstance(servers, dict) and isinstance(servers.get(server_name), dict)
    policy = load_mcp_client_access_policy(server_name)
    noninteractive = _is_mcp_client_runtime_noninteractive(server_name) if configured else False
    rows = get_mcp_status() if configured else []
    row = next((item for item in rows if item.get("name") == server_name), None)
    connected = bool(row and row.get("connected") is True)
    if row and row.get("status") in {"connected", "connecting", "failed", "disabled", "configured"}:
        status = row["status"]
    elif configured:
        status = "configured"
    else:
        status = "disabled"
    available = len(list_mcp_client_tools(server_name)) if policy.enabled else 0
    return {
        "server": server_name,
        "configured": configured,
        "client_access_enabled": policy.enabled,
        "noninteractive": noninteractive,
        "status": status,
        "connected": connected,
        "allowed_tool_count": len(policy.ordered_tools),
        "available_tool_count": available,
    }


def _find_registry_tool(server_name: str, raw_tool_name: str) -> str | None:
    from tools.mcp_tool import get_mcp_tool_provenance
    from tools.registry import registry

    for registry_name in registry.get_all_tool_names():
        provenance = get_mcp_tool_provenance(registry_name)
        if (
            provenance is not None
            and provenance.server_name == server_name
            and provenance.raw_tool_name == raw_tool_name
        ):
            return registry_name
    return None


def _bounded_result_text(value: str) -> tuple[str, bool]:
    encoded = value.encode("utf-8")
    if len(encoded) <= _MAX_RESULT_BYTES:
        return value, False
    marker = _RESULT_TRUNCATION_MARKER.encode("utf-8")
    prefix = encoded[: _MAX_RESULT_BYTES - len(marker)]
    while True:
        try:
            return prefix.decode("utf-8") + _RESULT_TRUNCATION_MARKER, True
        except UnicodeDecodeError:
            prefix = prefix[:-1]


def _strip_session_credential_metadata(value: str) -> tuple[str, object]:
    """Remove backend session/OAuth timing hints from a JSON result envelope."""
    try:
        envelope = json.loads(value)
    except (TypeError, ValueError):
        return value, None
    changed = False

    def sanitize(node: object, *, metadata: bool = False) -> object:
        nonlocal changed
        if isinstance(node, dict):
            clean = {}
            for key, child in node.items():
                if metadata and key == "session":
                    changed = True
                    continue
                clean[key] = sanitize(child, metadata=key == "_meta")
            return clean
        if isinstance(node, list):
            return [sanitize(child) for child in node]
        return node

    sanitized = sanitize(envelope)
    if not changed:
        return value, envelope
    return (
        json.dumps(sanitized, ensure_ascii=False, separators=(",", ":"), allow_nan=False),
        sanitized,
    )


def call_mcp_client_tool(
    server_name: str, tool_name: str, arguments: dict
) -> MCPClientCallResult:
    """Dispatch one bounded call through the canonical tool registry."""
    if not _valid_exact_name(server_name) or not _valid_exact_name(tool_name):
        raise MCPClientAccessError("MCP_CLIENT_INVALID_PARAMS", "Invalid MCP server or tool name")
    if not isinstance(arguments, dict):
        raise MCPClientAccessError("MCP_CLIENT_INVALID_PARAMS", "arguments must be a JSON object")
    try:
        serialized_arguments = json.dumps(
            arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
    except (TypeError, ValueError):
        raise MCPClientAccessError(
            "MCP_CLIENT_INVALID_PARAMS", "arguments must contain finite JSON values"
        ) from None
    if len(serialized_arguments.encode("utf-8")) > _MAX_ARGUMENT_BYTES:
        raise MCPClientAccessError("MCP_CLIENT_INVALID_PARAMS", "arguments exceed 64 KiB")

    policy = load_mcp_client_access_policy(server_name)
    if not policy.enabled:
        raise MCPClientAccessError("MCP_CLIENT_DISABLED", "MCP client access is disabled")
    if not policy.allows(tool_name):
        raise MCPClientAccessError("MCP_CLIENT_TOOL_DENIED", "MCP tool is not allowlisted")

    registry_name = _find_registry_tool(server_name, tool_name)
    if registry_name is None:
        raise MCPClientAccessError("MCP_CLIENT_SERVER_UNAVAILABLE", "MCP tool is unavailable")

    from tools.mcp_tool import is_mcp_tool_read_only

    if not is_mcp_tool_read_only(server_name, tool_name):
        raise MCPClientAccessError("MCP_CLIENT_NOT_READ_ONLY", "MCP tool is not marked read-only")
    if not _is_mcp_client_runtime_noninteractive(server_name):
        raise MCPClientAccessError(
            "MCP_CLIENT_INTERACTIVE_RUNTIME", "MCP runtime must be reloaded as non-interactive"
        )

    with _client_locks_guard:
        client_lock = _client_locks.setdefault(server_name, threading.Lock())
    if not client_lock.acquire(blocking=False):
        raise MCPClientAccessError("MCP_CLIENT_BUSY", "Another MCP client call is already running")

    request_id = f"mcpui_{uuid.uuid4().hex}"
    started = time.monotonic()
    truncated = False
    ok = False
    try:
        from tools.registry import registry

        # Re-read revocable policy/config at the final pre-transport boundary.
        # The gateway holds its MCP reload lock across this block, so registry
        # provenance and runtime handlers cannot change between this check and
        # dispatch. A revocation observed here always wins.
        current_policy = load_mcp_client_access_policy(server_name)
        if not current_policy.enabled:
            raise MCPClientAccessError("MCP_CLIENT_DISABLED", "MCP client access is disabled")
        if not current_policy.allows(tool_name):
            raise MCPClientAccessError("MCP_CLIENT_TOOL_DENIED", "MCP tool is not allowlisted")
        if not is_mcp_tool_read_only(server_name, tool_name):
            raise MCPClientAccessError("MCP_CLIENT_NOT_READ_ONLY", "MCP tool is not marked read-only")
        if not _is_mcp_client_runtime_noninteractive(server_name):
            raise MCPClientAccessError(
                "MCP_CLIENT_INTERACTIVE_RUNTIME", "MCP runtime must be reloaded as non-interactive"
            )

        dispatched = registry.dispatch(registry_name, arguments)
        if not isinstance(dispatched, str):
            raise MCPClientAccessError(
                "MCP_CLIENT_RESULT_CONTRACT", "MCP tool returned an unsupported result"
            )
        safe_dispatched, envelope = _strip_session_credential_metadata(dispatched)
        result_text, truncated = _bounded_result_text(safe_dispatched)
        ok = not (
            isinstance(envelope, dict) and isinstance(envelope.get("error"), str)
        )
        duration_ms = max(0, int((time.monotonic() - started) * 1000))
        return MCPClientCallResult(
            request_id=request_id,
            server=server_name,
            tool=tool_name,
            ok=ok,
            duration_ms=duration_ms,
            result_text=result_text,
            truncated=truncated,
        )
    finally:
        duration_ms = max(0, int((time.monotonic() - started) * 1000))
        logger.info(
            "MCP client call request_id=%s server=%s tool=%s status=%s duration_ms=%d truncated=%s",
            request_id, server_name, tool_name, "ok" if ok else "error", duration_ms, truncated,
        )
        client_lock.release()
