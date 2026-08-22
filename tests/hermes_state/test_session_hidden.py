import pytest

from hermes_state import SessionDB


@pytest.fixture
def db(tmp_path):
    database = SessionDB(tmp_path / "state.db")
    try:
        yield database
    finally:
        database.close()


def test_hidden_excluded_by_default_included_on_request(db):
    db.create_session("visible", source="cli")
    db.create_session("secret", source="cli")
    # Give both a message so the default min_message_count filter keeps them.
    for sid in ("visible", "secret"):
        db._conn.execute(
            "UPDATE sessions SET message_count = 1 WHERE id = ?", (sid,)
        )
    db._conn.commit()

    # Flip the hidden flag on one session.
    assert db.set_session_hidden("secret", True) is True
    assert db.get_session("secret")["hidden"] == 1
    assert db.get_session("visible")["hidden"] == 0

    # Default listing drops the hidden row; include_hidden=True surfaces it.
    default_ids = {s["id"] for s in db.list_sessions_rich(min_message_count=1)}
    assert default_ids == {"visible"}

    all_ids = {
        s["id"]
        for s in db.list_sessions_rich(min_message_count=1, include_hidden=True)
    }
    assert all_ids == {"visible", "secret"}

    # Unhiding brings it back into the default listing.
    assert db.set_session_hidden("secret", False) is True
    assert db.get_session("secret")["hidden"] == 0
    unhidden_ids = {s["id"] for s in db.list_sessions_rich(min_message_count=1)}
    assert unhidden_ids == {"visible", "secret"}


@pytest.mark.parametrize("source", ["subagent", "kanban", "tool", "cron"])
def test_owner_managed_session_is_hidden_in_its_first_row(db, source):
    db.create_session(f"worker-{source}", source=source)

    row = db.get_session(f"worker-{source}")

    assert row["hidden"] == 1


@pytest.mark.parametrize("source", ["cli", "desktop", "telegram"])
def test_user_session_is_visible_in_its_first_row(db, source):
    db.create_session(f"user-{source}", source=source)

    row = db.get_session(f"user-{source}")

    assert row["hidden"] == 0


def test_canonical_bot_chat_stays_visible_for_normal_source(db):
    db.create_session("bot-chat", source="desktop")
    assert db.set_session_title("bot-chat", "Bot Chat") is True

    row = db.get_session("bot-chat")

    assert row["hidden"] == 0


def test_worker_source_wins_over_explicit_visible_request(db):
    db.create_session("worker", source="tool", hidden=False)

    assert db.get_session("worker")["hidden"] == 1


def test_owner_can_fetch_hidden_worker_by_id_and_include_hidden(db):
    db.create_session("worker", source="kanban")

    assert db.get_session("worker")["id"] == "worker"
    assert db.list_sessions_rich() == []
    assert [row["id"] for row in db.list_sessions_rich(include_hidden=True)] == ["worker"]


def test_source_scoped_owner_query_and_count_include_hidden_workers(db):
    db.create_session("cron-run", source="cron")

    assert [row["id"] for row in db.list_sessions_rich(source="cron")] == ["cron-run"]
    assert db.session_count(source="cron") == 1
    assert db.session_count() == 0
