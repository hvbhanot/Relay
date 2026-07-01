from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_FEEDBACK_PATH = Path("relay.feedback.json")

# How much one vote moves the local-confidence threshold. A downvote on an
# answer that ran fully local raises the threshold (escalate to cloud sooner);
# an upvote lowers it (trust local more). Cloud-routed votes are recorded for
# the log but do not shift the threshold — there is nothing local to tune.
_DOWNVOTE_LOCAL_STEP = 0.03
_UPVOTE_LOCAL_STEP = -0.02
_MAX_BIAS = 0.15
_MAX_ENTRIES = 500


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def load_entries(path: str | Path = DEFAULT_FEEDBACK_PATH) -> list[dict[str, Any]]:
    feedback_path = Path(path)
    if not feedback_path.exists():
        return []
    try:
        data = json.loads(feedback_path.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    entries = data.get("entries") if isinstance(data, dict) else None
    return entries if isinstance(entries, list) else []


def record_feedback(
    *,
    score: int,
    all_local: bool | None,
    models: list[str] | None = None,
    path: str | Path = DEFAULT_FEEDBACK_PATH,
) -> dict[str, Any]:
    if score not in (-1, 1):
        raise ValueError("score must be 1 or -1")
    entry: dict[str, Any] = {
        "created_at": _now_iso(),
        "score": score,
        "all_local": all_local,
        "models": [m for m in (models or []) if isinstance(m, str) and m][:8],
    }
    entries = load_entries(path)
    entries.append(entry)
    entries = entries[-_MAX_ENTRIES:]
    Path(path).write_text(json.dumps({"entries": entries}, indent=2, ensure_ascii=False) + "\n")
    return entry


def confidence_bias(path: str | Path = DEFAULT_FEEDBACK_PATH) -> float:
    """Aggregate feedback into a bias added to `min_local_confidence`."""
    bias = 0.0
    for entry in load_entries(path):
        if entry.get("all_local") is not True:
            continue
        score = entry.get("score")
        if score == -1:
            bias += _DOWNVOTE_LOCAL_STEP
        elif score == 1:
            bias += _UPVOTE_LOCAL_STEP
    return max(-_MAX_BIAS, min(_MAX_BIAS, round(bias, 4)))


def bias_note(bias: float) -> str:
    if bias > 0:
        return "Relay now escalates local answers to cloud a little sooner."
    if bias < 0:
        return "Relay now trusts local answers a little more."
    return "Routing threshold unchanged."
