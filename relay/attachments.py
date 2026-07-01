from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from typing import Any, Literal

AttachmentKind = Literal["text", "image"]
MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024
MAX_TEXT_CHARS = 24_000

TEXT_MIME_PREFIXES = ("text/", "application/json", "application/xml", "application/javascript")
TEXT_EXTENSIONS = {
    ".txt",
    ".md",
    ".markdown",
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".csv",
    ".html",
    ".css",
    ".sql",
    ".sh",
    ".rs",
    ".go",
    ".java",
    ".c",
    ".cpp",
    ".h",
    ".rb",
    ".php",
    ".swift",
    ".kt",
    ".xml",
    ".log",
}
IMAGE_MIMES = {"image/png", "image/jpeg", "image/jpg", "image/gif", "image/webp"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}

VISION_LOCAL_HINTS = (
    "llava",
    "moondream",
    "bakllava",
    "minicpm-v",
    "gemma3",
    "gemma4",
    "vision",
    "qwen2-vl",
    "qwen2.5-vl",
    "qwen3-vl",
    "llama3.2-vision",
    "llama3.3-vision",
    "pixtral",
    "cogvlm",
)


@dataclass(slots=True)
class Attachment:
    name: str
    mime: str
    kind: AttachmentKind
    data: str
    size_bytes: int
    text_excerpt: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        if self.kind == "image":
            payload.pop("text_excerpt", None)
        return payload


def _ollama_reports_vision(base_url: str, model: str) -> bool | None:
    """Ask Ollama whether a model supports vision. None when the probe cannot run."""
    url = f"{base_url.rstrip('/')}/api/show"
    body = json.dumps({"name": model}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"content-type": "application/json", "accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    capabilities = data.get("capabilities")
    if isinstance(capabilities, list):
        return "vision" in capabilities
    return None


def local_model_supports_vision(model: str, *, base_url: str | None = None) -> bool:
    if base_url:
        reported = _ollama_reports_vision(base_url, model)
        if reported is not None:
            return reported
    lower = model.lower()
    return any(hint in lower for hint in VISION_LOCAL_HINTS)


def _extension(name: str) -> str:
    dot = name.rfind(".")
    return name[dot:].lower() if dot >= 0 else ""


def _decode_payload(data: str) -> bytes:
    cleaned = data.strip()
    if cleaned.startswith("data:") and "," in cleaned:
        cleaned = cleaned.split(",", 1)[1]
    try:
        return base64.b64decode(cleaned, validate=True)
    except Exception as exc:
        raise ValueError("attachment data must be valid base64") from exc


def _classify(name: str, mime: str) -> AttachmentKind:
    mime = (mime or "").split(";", 1)[0].strip().lower()
    ext = _extension(name)
    if mime in IMAGE_MIMES or ext in IMAGE_EXTENSIONS:
        return "image"
    if mime.startswith(TEXT_MIME_PREFIXES) or ext in TEXT_EXTENSIONS:
        return "text"
    if mime == "application/octet-stream" and ext in TEXT_EXTENSIONS:
        return "text"
    raise ValueError(f"Unsupported file type for {name or 'attachment'} ({mime or 'unknown'})")


def parse_attachment(*, name: str, mime: str, data: str) -> Attachment:
    raw_name = (name or "attachment").strip() or "attachment"
    raw = _decode_payload(data)
    if len(raw) > MAX_ATTACHMENT_BYTES:
        raise ValueError(f"{raw_name} exceeds the {MAX_ATTACHMENT_BYTES // (1024 * 1024)}MB limit")
    kind = _classify(raw_name, mime)
    encoded = base64.b64encode(raw).decode("ascii")
    text_excerpt: str | None = None
    if kind == "text":
        text = raw.decode("utf-8", errors="replace")
        text_excerpt = text[:MAX_TEXT_CHARS]
        if len(text) > MAX_TEXT_CHARS:
            text_excerpt += "\n\n[Truncated for routing; full file was attached.]"
    return Attachment(
        name=raw_name,
        mime=(mime or "application/octet-stream").split(";", 1)[0].strip().lower(),
        kind=kind,
        data=encoded,
        size_bytes=len(raw),
        text_excerpt=text_excerpt,
    )


def parse_attachments(raw_items: Any) -> list[Attachment]:
    if not raw_items:
        return []
    if not isinstance(raw_items, list):
        raise ValueError("attachments must be an array")
    parsed: list[Attachment] = []
    for item in raw_items[:8]:
        if not isinstance(item, dict):
            raise ValueError("each attachment must be an object")
        parsed.append(
            parse_attachment(
                name=str(item.get("name", "attachment")),
                mime=str(item.get("mime", "")),
                data=str(item.get("data", "")),
            )
        )
    return parsed


def has_images(attachments: list[Attachment]) -> bool:
    return any(item.kind == "image" for item in attachments)


def image_payloads(attachments: list[Attachment]) -> list[str]:
    return [f"data:{item.mime};base64,{item.data}" for item in attachments if item.kind == "image"]


def attachments_summary(attachments: list[Attachment]) -> str:
    if not attachments:
        return ""
    lines = ["Attached files:"]
    for item in attachments:
        if item.kind == "image":
            lines.append(f"- {item.name} (image, {item.size_bytes} bytes)")
        else:
            lines.append(f"- {item.name} (text, {item.size_bytes} bytes)")
    return "\n".join(lines)


def attachments_prompt_context(attachments: list[Attachment]) -> str:
    if not attachments:
        return ""
    chunks = [attachments_summary(attachments), ""]
    for item in attachments:
        if item.kind == "text" and item.text_excerpt:
            chunks.append(f"### File: {item.name}\n{item.text_excerpt}")
    return "\n\n".join(chunk for chunk in chunks if chunk)


def planning_prompt(user_prompt: str, attachments: list[Attachment]) -> str:
    context = attachments_prompt_context(attachments)
    if not context:
        return user_prompt
    return f"{user_prompt.strip()}\n\n{context}".strip()


def attachment_plan_hint(attachments: list[Attachment]) -> str:
    if not attachments:
        return ""
    image_count = sum(1 for item in attachments if item.kind == "image")
    text_count = sum(1 for item in attachments if item.kind == "text")
    hints = []
    if image_count:
        hints.append(f"{image_count} image(s) require a vision-capable model")
    if text_count:
        hints.append(f"{text_count} text file(s) were inlined for planning")
    hints.append("split file review and the user's actual question into separate subtasks when both are present")
    return "; ".join(hints)


def vision_required(attachments: list[Attachment]) -> bool:
    return has_images(attachments)