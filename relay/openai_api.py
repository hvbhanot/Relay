from __future__ import annotations

import json
import time
import uuid
from collections.abc import Iterator
from typing import Any

from .providers import ProviderError

DEFAULT_MODEL = "relay"


def _extract_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(part for part in parts if part)
    return str(content or "")


def messages_to_prompt(messages: list[dict[str, Any]]) -> str:
    if not messages:
        raise ValueError("messages must be a non-empty array")

    normalized: list[tuple[str, str]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role", "user")).strip().lower() or "user"
        content = _extract_content(message.get("content", "")).strip()
        if content:
            normalized.append((role, content))

    if not normalized:
        raise ValueError("messages must include at least one non-empty message")

    user_messages = [content for role, content in normalized if role == "user"]
    if len(user_messages) == 1 and len(normalized) == 1:
        return user_messages[0]

    lines: list[str] = []
    for role, content in normalized:
        label = role.capitalize()
        lines.append(f"{label}: {content}")
    return "\n\n".join(lines)


def parse_chat_completion_request(payload: dict[str, Any]) -> tuple[str, str, bool]:
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ValueError("messages must be a non-empty array")
    prompt = messages_to_prompt(messages)
    model = str(payload.get("model") or DEFAULT_MODEL)
    stream = bool(payload.get("stream"))
    return prompt, model, stream


def completion_dict(*, text: str, model: str) -> dict[str, Any]:
    created = int(time.time())
    completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
    }


def stream_chunks(*, text: str, model: str) -> Iterator[str]:
    created = int(time.time())
    completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    first = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
    }
    yield f"data: {json.dumps(first, ensure_ascii=False)}\n\n"

    step = max(1, len(text) // 24) if text else 1
    for index in range(0, len(text), step):
        piece = text[index : index + step]
        chunk = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}],
        }
        yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

    done = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    yield f"data: {json.dumps(done, ensure_ascii=False)}\n\n"
    yield "data: [DONE]\n\n"


def stream_start(model: str) -> tuple[str, int, str]:
    created = int(time.time())
    completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    first = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
    }
    return completion_id, created, f"data: {json.dumps(first, ensure_ascii=False)}\n\n"


def stream_token(*, completion_id: str, created: int, model: str, text: str) -> str:
    payload = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
    }
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def stream_finish(*, completion_id: str, created: int, model: str) -> Iterator[str]:
    done = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    yield f"data: {json.dumps(done, ensure_ascii=False)}\n\n"
    yield "data: [DONE]\n\n"


def models_dict(*, local_models: list[str] | None = None) -> dict[str, Any]:
    data = [
        {
            "id": DEFAULT_MODEL,
            "object": "model",
            "created": int(time.time()),
            "owned_by": "relay",
        }
    ]
    for name in local_models or []:
        if name == DEFAULT_MODEL:
            continue
        data.append(
            {
                "id": name,
                "object": "model",
                "created": int(time.time()),
                "owned_by": "relay-local",
            }
        )
    return {"object": "list", "data": data}


def error_response(exc: Exception) -> tuple[dict[str, Any], int]:
    if isinstance(exc, ProviderError):
        return {
            "error": {
                "message": str(exc),
                "type": "provider_error",
                "code": "provider_error",
            }
        }, 502
    if isinstance(exc, ValueError):
        return {
            "error": {
                "message": str(exc),
                "type": "invalid_request_error",
                "code": "invalid_request",
            }
        }, 400
    return {
        "error": {
            "message": str(exc),
            "type": "server_error",
            "code": "server_error",
        }
    }, 500