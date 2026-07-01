from __future__ import annotations

import re

from .schema import Message

# Secrets are swapped for plain bracketed placeholders before a prompt leaves for
# a cloud model, and swapped back into the answer locally. Placeholders are
# deliberately boring ASCII so models pass them through untouched.

# `key: value` / `key=value` assignments. Values that are clearly code references
# (calls, env lookups, template vars) are left alone so coding prompts survive.
_ASSIGN_RE = re.compile(
    r"(?i)\b(password|passwd|pwd|api[_-]?key|secret|access[_-]?token|auth[_-]?token|token)\b(\s*[:=]\s*[\"']?)([^\s\"']{6,})"
)
_ASSIGN_VALUE_EXCLUDE = re.compile(r"[(){}<$]|^(?:os\.|process\.|env|ENV|None|null|true|false)")

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("PRIVATE_KEY", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]+?-----END [A-Z ]*PRIVATE KEY-----")),
    ("API_KEY", re.compile(r"\b(?:sk|rk)-[A-Za-z0-9_-]{16,}\b")),
    ("API_KEY", re.compile(r"\b(?:gsk_|ghp_|gho_|github_pat_|xoxb-|xoxp-|glpat-|AKIA)[A-Za-z0-9_-]{8,}\b")),
    ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("CARD", re.compile(r"\b\d{4}[ -]\d{4}[ -]\d{4}[ -]\d{2,4}\b")),
]


class RedactionState:
    """Placeholder -> original secret mapping accumulated across messages."""

    def __init__(self) -> None:
        self.replacements: dict[str, str] = {}
        self._counts: dict[str, int] = {}

    def placeholder_for(self, kind: str, secret: str) -> str:
        for placeholder, original in self.replacements.items():
            if original == secret:
                return placeholder
        count = self._counts.get(kind, 0) + 1
        self._counts[kind] = count
        placeholder = f"[REDACTED_{kind}_{count}]"
        self.replacements[placeholder] = secret
        return placeholder


def redact_text(text: str, state: RedactionState) -> str:
    def assign_repl(match: re.Match[str]) -> str:
        value = match.group(3)
        if _ASSIGN_VALUE_EXCLUDE.search(value):
            return match.group(0)
        return f"{match.group(1)}{match.group(2)}{state.placeholder_for('SECRET', value)}"

    out = _ASSIGN_RE.sub(assign_repl, text or "")
    for kind, pattern in _PATTERNS:
        out = pattern.sub(lambda m, k=kind: state.placeholder_for(k, m.group(0)), out)
    return out


def redact_messages(messages: list[Message]) -> tuple[list[Message], RedactionState | None]:
    """Redact every message's text. Returns the originals when nothing matched."""
    state = RedactionState()
    redacted = [
        Message(message.role, redact_text(message.content, state), images=list(message.images))
        for message in messages
    ]
    if not state.replacements:
        return messages, None
    return redacted, state


def restore_text(text: str, state: RedactionState | None) -> str:
    if state is None:
        return text
    for placeholder, secret in state.replacements.items():
        text = text.replace(placeholder, secret)
    return text
