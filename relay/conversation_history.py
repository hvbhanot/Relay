from __future__ import annotations

from typing import Any

from .schema import Message

MAX_HISTORY_MESSAGES = 40
MAX_HISTORY_CHARS = 16_000


def parse_conversation_history(raw: Any) -> list[Message]:
    if not raw:
        return []
    if not isinstance(raw, list):
        raise ValueError("history must be an array")
    parsed: list[Message] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role", "")).strip().lower()
        content = str(item.get("content", "")).strip()
        if role not in {"user", "assistant"} or not content:
            continue
        parsed.append(Message(role=role, content=content))
    return trim_conversation_history(parsed)


def trim_conversation_history(messages: list[Message]) -> list[Message]:
    if not messages:
        return []
    trimmed = messages[-MAX_HISTORY_MESSAGES:]
    total = 0
    kept: list[Message] = []
    for message in reversed(trimmed):
        total += len(message.content)
        if total > MAX_HISTORY_CHARS and kept:
            break
        kept.append(message)
    kept.reverse()
    return kept


def conversation_prompt_block(history: list[Message]) -> str:
    if not history:
        return ""
    lines = ["Conversation so far:"]
    for message in history:
        label = "User" if message.role == "user" else "Assistant"
        lines.append(f"{label}: {message.content}")
    return "\n".join(lines)


def prompt_with_conversation(user_prompt: str, history: list[Message]) -> str:
    block = conversation_prompt_block(history)
    if not block:
        return user_prompt.strip()
    latest = user_prompt.strip()
    return f"{block}\n\nLatest user message:\n{latest}".strip()