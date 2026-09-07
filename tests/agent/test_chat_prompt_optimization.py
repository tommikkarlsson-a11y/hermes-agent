"""Prompt-only optimizations must preserve real discovery and task behavior."""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import yaml

from agent import prompt_builder as pb
from agent.conversation_loop import _restore_or_build_system_prompt
from agent.system_prompt import _tool_guidance_block
from tools.skills_tool import skills_list, skill_view


@pytest.mark.parametrize("task", [None, "", "  "])
def test_interactive_kanban_tools_do_not_claim_worker_role(monkeypatch, task):
    if task is None:
        monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    else:
        monkeypatch.setenv("HERMES_KANBAN_TASK", task)
    agent = SimpleNamespace(valid_tool_names={"kanban_show", "kanban_list"})
    assert "Kanban task execution protocol" not in (_tool_guidance_block(agent) or "")
    assert agent.valid_tool_names == {"kanban_show", "kanban_list"}


def test_worker_retains_guidance_and_frozen_value(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "assigned-task")
    agent = SimpleNamespace(valid_tool_names={"kanban_show"})
    worker = _tool_guidance_block(agent)
    assert worker and "Kanban task execution protocol" in worker
    agent._kanban_worker_guidance = worker
    monkeypatch.delenv("HERMES_KANBAN_TASK")
    assert _tool_guidance_block(agent) == worker
    assert not _tool_guidance_block(SimpleNamespace(valid_tool_names=set()))


@pytest.mark.parametrize("kind", ["delegated_child_context", "non_dispatcher_owned_context"])
def test_inherited_task_is_not_worker_ownership(monkeypatch, kind):
    from agent import delegation_context

    monkeypatch.setenv("HERMES_KANBAN_TASK", "parent-task")
    with getattr(delegation_context, kind)():
        assert pb.build_kanban_worker_guidance({"kanban_show"}) == ""
    assert "Kanban task execution protocol" in pb.build_kanban_worker_guidance({"kanban_show"})


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "desktop")
    monkeypatch.delenv("HERMES_PLATFORM", raising=False)
    for name in ("common", "specialist"):
        p = tmp_path / "skills" / "demo" / name / "SKILL.md"
        p.parent.mkdir(parents=True)
        p.write_text(f"---\nname: {name}\ndescription: Use {name} capability.\n---\n\nBody for {name}.\n")
    pb.clear_skills_system_prompt_cache(clear_snapshot=True)
    yield tmp_path
    pb.clear_skills_system_prompt_cache(clear_snapshot=True)


def configure(home, value):
    (home / "config.yaml").write_text(yaml.safe_dump({"skills": {"prompt_index_allowlist": value}}))


def build():
    return pb.build_skills_system_prompt(available_tools={"skills_list", "skill_view"})


def test_short_index_preserves_discovery_and_loading(home):
    full = build()
    configure(home, ["common"])
    short = build()
    assert "- common:" in short
    assert "- specialist:" not in short
    assert "skills_list" in short
    assert len(short) < len(full) + 300
    assert "specialist" in skills_list()
    loaded = json.loads(skill_view("specialist"))
    assert loaded.get("success") is True
    assert "Body for specialist." in loaded["content"]
    configure(home, ["specialist"])
    changed = build()
    assert "- specialist:" in changed and "- common:" not in changed


def test_resumed_session_keeps_persisted_shortlist_bytes(home):
    """A profile change affects new builds, never an existing session prefix."""
    configure(home, ["common"])
    persisted = build()

    configure(home, ["specialist"])
    fresh = build()
    assert "- specialist:" in fresh and "- common:" not in fresh

    db = MagicMock()
    db.get_session.return_value = {"system_prompt": persisted}
    agent = MagicMock()
    agent._cached_system_prompt = None
    agent._session_db = db
    agent.session_id = "resumed-session"
    agent.model = "test-model"
    agent.provider = "openrouter"
    agent.platform = "desktop"
    agent._use_prompt_caching = False
    agent._build_system_prompt.side_effect = AssertionError("resume must not rebuild")

    _restore_or_build_system_prompt(agent, None, [{"role": "user", "content": "continue"}])

    assert agent._cached_system_prompt.encode("utf-8") == persisted.encode("utf-8")
    assert "- common:" in agent._cached_system_prompt
    assert "- specialist:" not in agent._cached_system_prompt
    agent._build_system_prompt.assert_not_called()
    db.update_system_prompt.assert_not_called()


@pytest.mark.parametrize("value", [None, [], "common", [1], [""], ["nonexistent"]])
def test_invalid_empty_or_unmatched_allowlist_preserves_index(home, value):
    configure(home, value)
    prompt = build()
    assert "- common:" in prompt and "- specialist:" in prompt


def test_no_discovery_tool_preserves_full_index(home):
    configure(home, ["common"])
    prompt = pb.build_skills_system_prompt(available_tools={"skill_view"})
    assert "- specialist:" in prompt


@pytest.mark.parametrize("selection", [["common"], ["nonexistent"]])
def test_shortlist_preserves_hermes_help_entry(home, selection):
    p = home / "skills" / "demo" / "hermes-agent" / "SKILL.md"
    p.parent.mkdir(parents=True)
    p.write_text("---\nname: hermes-agent\ndescription: Hermes help\n---\n")
    configure(home, selection)
    prompt = build()
    assert "- hermes-agent:" in prompt
    assert "- common:" in prompt
    assert ("- specialist:" in prompt) == (selection == ["nonexistent"])


@pytest.mark.parametrize("task", [None, "assigned-task"])
def test_tool_initialization_freezes_assignment_without_removing_tools(monkeypatch, task):
    from agent.agent_init import _load_tools
    import model_tools
    from hermes_cli import plugins

    definitions = [{"function": {"name": "kanban_show"}}, {"function": {"name": "skills_list"}}]
    monkeypatch.setattr(plugins, "discover_plugins", lambda: None)
    monkeypatch.setattr(model_tools, "get_tool_definitions", lambda **kw: definitions)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    if task:
        monkeypatch.setenv("HERMES_KANBAN_TASK", task)
    agent = SimpleNamespace(quiet_mode=True)
    _load_tools(agent, None, None)
    assert agent.tools == definitions
    before = _tool_guidance_block(agent)
    assert bool(before) == bool(task)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "different-later-context")
    assert _tool_guidance_block(agent) == before


def test_profile_override_owns_its_allowlist(home, tmp_path):
    configure(home, ["common"])
    other = tmp_path / "other-profile"
    p = other / "skills" / "demo" / "specialist" / "SKILL.md"
    p.parent.mkdir(parents=True)
    p.write_text("---\nname: specialist\ndescription: Specialist capability\n---\n")
    configure(other, ["specialist"])
    prompt = pb.build_skills_system_prompt(
        available_tools={"skills_list", "skill_view"}, skills_dir_override=other / "skills")
    assert "- specialist:" in prompt
    assert "- common:" not in prompt
    assert "- common:" in build()


def test_external_and_project_offers_are_filtered_after_precedence(home, tmp_path):
    roots = []
    for source in ("external", "project"):
        root = tmp_path / source
        p = root / f"{source}-specialist" / "SKILL.md"
        p.parent.mkdir(parents=True)
        p.write_text(f"---\nname: {source}-specialist\ndescription: {source} body\n---\n")
        roots.append(root)
    configure(home, ["common"])
    prompt = pb._build_skills_system_prompt_inner(
        home / "skills", [roots[0]], {"skills_list", "skill_view"}, None, None, [roots[1]])
    assert "- common:" in prompt
    assert "- external-specialist:" not in prompt
    assert "- project-specialist:" not in prompt
    configure(home, ["external-specialist", "project-specialist"])
    prompt = pb._build_skills_system_prompt_inner(
        home / "skills", [roots[0]], {"skills_list", "skill_view"}, None, None, [roots[1]])
    assert "- external-specialist:" in prompt
    assert "- project-specialist:" in prompt
    assert "- common:" not in prompt
