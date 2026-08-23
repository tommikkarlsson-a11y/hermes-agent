"""One-turn fail-closed fence for local Bot Mode advisory messages."""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any


EXPLICIT_ADVISORY_MARKER = "[HERMES_BOT_ADVISORY_V1]"
LEGACY_ADVISORY_PREFIX = "Message from 🤖 "

# Deliberately fixed and built-in only. Do not derive this from toolsets or the
# registry: plugins, MCP servers, and future tools must fail closed until they
# receive an explicit review here.
READ_ONLY_TOOL_ALLOWLIST = frozenset(
    {
        "mem0_search",
        "read_file",
        "search_files",
        "session_search",
        "skill_view",
        "skills_list",
        "web_search",
        "web_extract",
        "vision_analyze",
    }
)

_ADVISORY_TURN_CONTEXT: ContextVar[bool] = ContextVar(
    "hermes_bot_advisory_turn",
    default=False,
)


def is_advisory_message(message: Any) -> bool:
    """Recognize explicit v1 and legacy local Bot Mode attribution markers."""
    text = message if isinstance(message, str) else ""
    return text.startswith(f"{EXPLICIT_ADVISORY_MARKER}\n") or text.startswith(
        LEGACY_ADVISORY_PREFIX
    )


def mark_advisory_turn(agent: Any, message: Any) -> bool:
    """Recompute the advisory flag for this turn; never carry it across turns."""
    active = is_advisory_message(message)
    agent._bot_advisory_turn = active
    _ADVISORY_TURN_CONTEXT.set(active)
    return active


def is_advisory_turn_context() -> bool:
    """Return whether the current execution context is an advisory Bot turn."""
    return bool(_ADVISORY_TURN_CONTEXT.get())


def advisory_tool_block(agent: Any, function_name: str) -> str | None:
    """Return a fail-closed block message for non-allowlisted advisory tools."""
    if not bool(getattr(agent, "_bot_advisory_turn", False)):
        return None
    name = str(function_name or "")
    if name in READ_ONLY_TOOL_ALLOWLIST:
        return None
    return (
        f"Local Bot-to-Bot advisory turns cannot execute tool '{name}'. "
        "Only the fixed built-in read-only tools are allowed; create and run "
        "real work through an admitted Kanban mission."
    )
