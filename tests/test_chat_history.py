from pathlib import Path

from relay.chat_history import (
    append_message,
    create_session,
    delete_session,
    ensure_active_session,
    get_session,
    list_sessions,
    set_active_session,
)


def test_chat_history_create_append_and_list(tmp_path: Path) -> None:
    path = tmp_path / "history.json"
    session = create_session(path=path, title="New chat")
    append_message(session["id"], role="user", content="Hello Relay", path=path)
    append_message(session["id"], role="assistant", content="Hi there", path=path, trace={"plan": {"summary": "x"}})

    listed = list_sessions(path)
    assert listed["active_session_id"] == session["id"]
    assert listed["sessions"][0]["title"] == "Hello Relay"
    assert listed["sessions"][0]["message_count"] == 2

    loaded = get_session(session["id"], path)
    assert loaded is not None
    assert loaded["messages"][0]["role"] == "user"
    assert loaded["messages"][1]["trace"]["plan"]["summary"] == "x"


def test_clear_active_session(tmp_path: Path) -> None:
    path = tmp_path / "history.json"
    session = create_session(path=path, title="Active")
    assert list_sessions(path)["active_session_id"] == session["id"]
    set_active_session(None, path)
    assert list_sessions(path)["active_session_id"] is None


def test_chat_history_delete_and_ensure_active(tmp_path: Path) -> None:
    path = tmp_path / "history.json"
    first = create_session(path=path, title="First")
    second = create_session(path=path, title="Second")
    assert delete_session(first["id"], path) is True
    active = ensure_active_session(path)
    assert active["id"] == second["id"]