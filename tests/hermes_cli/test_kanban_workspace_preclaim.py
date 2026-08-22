from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    (home / "profiles" / "coder").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_local_workspace_probe_is_bounded_read(monkeypatch, tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "file.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr(kb, "_profile_terminal_config", lambda _a: ("local", {}))
    assert kb._workspace_compatibility_probe("coder", str(workspace))[0] is True
    ok, reason = kb._workspace_compatibility_probe("coder", str(tmp_path / "missing"))
    assert ok is False
    assert "not a directory" in reason


def test_docker_workspace_probe_uses_assignee_mount_and_read_only_container(
    monkeypatch, tmp_path,
):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(kb, "_profile_terminal_config", lambda _a: (
        "docker",
        {
            "docker_volumes": [f"{workspace}:/workspace:rw"],
            "docker_image": "test-image",
        },
    ))
    monkeypatch.setattr(kb.subprocess, "run", fake_run)
    ok, reason = kb._workspace_compatibility_probe("coder", str(workspace))
    assert ok is True
    assert "docker read probe passed" in reason
    assert f"{workspace}:/workspace:ro" in seen["argv"]
    assert seen["kwargs"]["timeout"] == 15
    assert "--network" in seen["argv"] and "none" in seen["argv"]


def test_unreachable_coder_routes_needs_codex_before_claim_or_failure(
    kanban_home, monkeypatch, tmp_path,
):
    workspace = tmp_path / "unmounted"
    workspace.mkdir()
    spawned = []
    monkeypatch.setattr(
        kb, "_workspace_compatibility_probe",
        lambda *_a, **_kw: (False, "no exact Docker mount"),
    )
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="code", assignee="coder", workspace_kind="dir",
            workspace_path=str(workspace),
        )
        result = kb.dispatch_once(
            conn, spawn_fn=lambda *_a, **_kw: spawned.append(True) or 123,
            reconcile_orphans=False,
        )
        task = kb.get_task(conn, tid)
        assert result.needs_codex == [tid]
        assert spawned == []
        assert task.status == "ready"
        assert task.assignee == "needs_codex"
        assert task.current_run_id is None
        assert task.consecutive_failures == 0
        assert kb.list_runs(conn, tid) == []
        event = next(e for e in kb.list_events(conn, tid) if e.kind == "workspace_unreachable")
        assert event.payload["route"] == "NEEDS_CODEX"
        assert event.payload["preclaim"] is True
