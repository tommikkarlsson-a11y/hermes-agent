"""Regression: internal worker volume must not crowd out human history."""

from __future__ import annotations

import tui_gateway.server as srv
from hermes_state import SessionDB


def test_session_list_applies_internal_source_denyset_before_limit(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    db = SessionDB(db_path=home / "state.db")
    try:
        db.create_session("human", source="cli")
        db.append_message("human", "user", "keep me", timestamp=1)
        for index in range(250):
            sid = f"worker-{index:03d}"
            db.create_session(sid, source="kanban")
            db.append_message(sid, "user", "internal", timestamp=1000 + index)
    finally:
        db.close()

    monkeypatch.setattr(
        srv,
        "_get_db",
        lambda: SessionDB(db_path=home / "state.db"),
    )
    envelope = srv._methods["session.list"]("history-volume", {"limit": 1})

    assert envelope["result"]["sessions"][0]["id"] == "human"
