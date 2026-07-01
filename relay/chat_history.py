from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_HISTORY_PATH = Path("relay.history.json")
_MAX_SESSIONS = 200
_TITLE_MAX = 80


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _session_title(first_user_message: str) -> str:
    text = " ".join(first_user_message.split())
    if len(text) <= _TITLE_MAX:
        return text or "New chat"
    return text[: _TITLE_MAX - 1].rstrip() + "…"


def load_store(path: str | Path = DEFAULT_HISTORY_PATH) -> dict[str, Any]:
    config_path = Path(path)
    if not config_path.exists():
        return {"active_session_id": None, "sessions": []}
    try:
        data = json.loads(config_path.read_text())
    except (OSError, json.JSONDecodeError):
        return {"active_session_id": None, "sessions": []}
    if not isinstance(data, dict):
        return {"active_session_id": None, "sessions": []}
    sessions = data.get("sessions")
    if not isinstance(sessions, list):
        sessions = []
    active = data.get("active_session_id")
    return {"active_session_id": active if isinstance(active, str) else None, "sessions": sessions}


def save_store(store: dict[str, Any], path: str | Path = DEFAULT_HISTORY_PATH) -> None:
    config_path = Path(path)
    config_path.write_text(json.dumps(store, indent=2, ensure_ascii=False) + "\n")


def list_sessions(path: str | Path = DEFAULT_HISTORY_PATH) -> dict[str, Any]:
    store = load_store(path)
    summaries = []
    for session in store["sessions"]:
        if not isinstance(session, dict):
            continue
        messages = session.get("messages") if isinstance(session.get("messages"), list) else []
        summaries.append(
            {
                "id": session.get("id"),
                "title": session.get("title") or "Untitled chat",
                "updated_at": session.get("updated_at") or session.get("created_at"),
                "message_count": len(messages),
            }
        )
    summaries.sort(key=lambda item: item.get("updated_at") or "", reverse=True)
    return {"active_session_id": store.get("active_session_id"), "sessions": summaries}


def get_session(session_id: str, path: str | Path = DEFAULT_HISTORY_PATH) -> dict[str, Any] | None:
    store = load_store(path)
    for session in store["sessions"]:
        if isinstance(session, dict) and session.get("id") == session_id:
            return session
    return None


def create_session(
    *,
    title: str | None = None,
    path: str | Path = DEFAULT_HISTORY_PATH,
    set_active: bool = True,
) -> dict[str, Any]:
    store = load_store(path)
    now = _now_iso()
    session = {
        "id": uuid.uuid4().hex,
        "title": (title or "New chat").strip() or "New chat",
        "created_at": now,
        "updated_at": now,
        "messages": [],
    }
    store["sessions"].insert(0, session)
    if len(store["sessions"]) > _MAX_SESSIONS:
        store["sessions"] = store["sessions"][:_MAX_SESSIONS]
    if set_active:
        store["active_session_id"] = session["id"]
    save_store(store, path)
    return session


def set_active_session(session_id: str | None, path: str | Path = DEFAULT_HISTORY_PATH) -> None:
    store = load_store(path)
    if session_id is not None and get_session(session_id, path) is None:
        raise ValueError(f"Unknown session: {session_id}")
    store["active_session_id"] = session_id
    save_store(store, path)


def delete_session(session_id: str, path: str | Path = DEFAULT_HISTORY_PATH) -> bool:
    store = load_store(path)
    before = len(store["sessions"])
    store["sessions"] = [s for s in store["sessions"] if not (isinstance(s, dict) and s.get("id") == session_id)]
    if store.get("active_session_id") == session_id:
        store["active_session_id"] = store["sessions"][0]["id"] if store["sessions"] else None
    save_store(store, path)
    return len(store["sessions"]) < before


def rename_session(session_id: str, title: str, path: str | Path = DEFAULT_HISTORY_PATH) -> dict[str, Any]:
    store = load_store(path)
    for session in store["sessions"]:
        if isinstance(session, dict) and session.get("id") == session_id:
            session["title"] = _session_title(title) if title.strip() else "Untitled chat"
            save_store(store, path)
            return session
    raise ValueError(f"Unknown session: {session_id}")


def pop_last_message(
    session_id: str,
    *,
    role: str | None = None,
    path: str | Path = DEFAULT_HISTORY_PATH,
) -> dict[str, Any] | None:
    """Remove and return the session's last message (optionally only if it has `role`)."""
    store = load_store(path)
    for session in store["sessions"]:
        if not (isinstance(session, dict) and session.get("id") == session_id):
            continue
        messages = session.get("messages")
        if not isinstance(messages, list) or not messages:
            return None
        last = messages[-1]
        if role and (not isinstance(last, dict) or last.get("role") != role):
            return None
        messages.pop()
        session["updated_at"] = _now_iso()
        save_store(store, path)
        return last if isinstance(last, dict) else None
    raise ValueError(f"Unknown session: {session_id}")


# Cap stored image payloads (~400KB binary as base64) so a few screenshots don't
# balloon relay.history.json; oversized images degrade to a name-only chip.
_MAX_STORED_IMAGE_B64 = 550_000


def _sanitize_attachments(raw: Any) -> list[dict[str, Any]] | None:
    if not isinstance(raw, list):
        return None
    cleaned: list[dict[str, Any]] = []
    for item in raw[:8]:
        if not isinstance(item, dict) or item.get("kind") not in ("text", "image"):
            continue
        try:
            size = int(item.get("size_bytes") or 0)
        except (TypeError, ValueError):
            size = 0
        entry: dict[str, Any] = {
            "name": str(item.get("name") or "attachment")[:120],
            "mime": str(item.get("mime") or ""),
            "kind": item["kind"],
            "size_bytes": size,
        }
        data = item.get("data")
        if item["kind"] == "image" and isinstance(data, str) and len(data) <= _MAX_STORED_IMAGE_B64:
            entry["data"] = data
        cleaned.append(entry)
    return cleaned or None


def append_message(
    session_id: str,
    *,
    role: str,
    content: str,
    trace: dict[str, Any] | None = None,
    attachments: Any = None,
    path: str | Path = DEFAULT_HISTORY_PATH,
) -> dict[str, Any]:
    if role not in {"user", "assistant"}:
        raise ValueError("role must be user or assistant")
    store = load_store(path)
    session: dict[str, Any] | None = None
    for item in store["sessions"]:
        if isinstance(item, dict) and item.get("id") == session_id:
            session = item
            break
    if session is None:
        raise ValueError(f"Unknown session: {session_id}")

    now = _now_iso()
    message: dict[str, Any] = {"role": role, "content": content, "created_at": now}
    if trace is not None:
        message["trace"] = trace
    stored_attachments = _sanitize_attachments(attachments)
    if stored_attachments:
        message["attachments"] = stored_attachments
    messages = session.setdefault("messages", [])
    if not isinstance(messages, list):
        messages = []
        session["messages"] = messages
    messages.append(message)

    if role == "user" and (session.get("title") in {None, "", "New chat"}):
        session["title"] = _session_title(content)
    session["updated_at"] = now
    store["active_session_id"] = session_id
    save_store(store, path)
    return message


def ensure_active_session(path: str | Path = DEFAULT_HISTORY_PATH) -> dict[str, Any]:
    store = load_store(path)
    active_id = store.get("active_session_id")
    if active_id:
        session = get_session(active_id, path)
        if session is not None:
            return session
    return create_session(path=path, set_active=True)