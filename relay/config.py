from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .schema import PrivacyMode

LOCAL_PROVIDER_DEFAULTS: dict[str, tuple[str, str]] = {
    "ollama": ("http://localhost:11434", "llama3.1:8b"),
    "lmstudio": ("http://localhost:1234/v1", "local-model"),
    "llamacpp": ("http://localhost:8080/v1", "local-model"),
    "vllm": ("http://localhost:8000/v1", "local-model"),
}
LOCAL_OPENAI_PROVIDERS = frozenset({"lmstudio", "llamacpp", "vllm"})

# Default capability -> cloud model map. Used only for the OpenRouter provider,
# where a single key unlocks many models. A subtask is routed to the model mapped
# to its highest-priority capability; anything unmapped uses the default cloud
# model (`openrouter_model`). All slugs are editable in Setup or via env.
DEFAULT_CLOUD_MODEL_MAP: dict[str, str] = {
    "high_stakes": "anthropic/claude-opus-4.8",
    "reasoning": "anthropic/claude-opus-4.8",
    "math": "anthropic/claude-opus-4.8",
    "coding": "anthropic/claude-opus-4.8",
    "large_context": "openai/gpt-5.5",
    "sources": "openai/gpt-5.5",
    "current_info": "openai/gpt-5.5",
    "vision": "openai/gpt-5.5",
}


def _load_dotenv(path: str | Path = ".env") -> None:
    dotenv = Path(path)
    if not dotenv.exists():
        return
    for raw_line in dotenv.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _getenv(name: str, *, legacy: str | None = None) -> str | None:
    value = os.getenv(name)
    if value is None and legacy:
        value = os.getenv(legacy)
    return value


def _bool(name: str, default: bool = False, *, legacy: str | None = None) -> bool:
    value = _getenv(name, legacy=legacy)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _float(name: str, default: float, *, legacy: str | None = None) -> float:
    value = _getenv(name, legacy=legacy)
    try:
        return float(value if value is not None else default)
    except ValueError:
        return default


def _int(name: str, default: int, *, legacy: str | None = None) -> int:
    value = _getenv(name, legacy=legacy)
    try:
        return int(value if value is not None else default)
    except ValueError:
        return default


def _str_map(name: str, default: dict[str, str], *, legacy: str | None = None) -> dict[str, str]:
    """Parse a JSON object env var into a {capability: model} map, merged over default.

    Invalid JSON or non-string entries are ignored so a typo never wipes the pool.
    """
    merged = dict(default)
    raw = _getenv(name, legacy=legacy)
    if not raw:
        return merged
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return merged
    if not isinstance(data, dict):
        return merged
    for key, value in data.items():
        if isinstance(key, str) and isinstance(value, str) and value.strip():
            merged[key.strip()] = value.strip()
    return merged


@dataclass(frozen=True, slots=True)
class RouterConfig:
    local_provider: str
    ollama_base_url: str
    ollama_model: str

    cloud_enabled: bool
    cloud_provider: str
    privacy_mode: PrivacyMode
    min_local_confidence: float
    max_subtasks: int

    openrouter_api_key: str | None
    openrouter_base_url: str
    openrouter_model: str
    openrouter_site_url: str | None
    openrouter_app_name: str

    openai_compat_base_url: str
    openai_compat_api_key: str | None
    openai_compat_model: str

    cloud_model_map: dict[str, str] = field(default_factory=dict)

    request_timeout_seconds: float = 120.0

    # Ollama-hosted web search for current_info/sources subtasks (ollama.com key).
    web_search_enabled: bool = False
    ollama_api_key: str | None = None

    # Mask secrets/PII in prompts before they are sent to cloud models.
    cloud_redaction: bool = True

    @classmethod
    def from_env(cls, *, load_dotenv: bool = True) -> "RouterConfig":
        if load_dotenv:
            _load_dotenv()
        privacy = (_getenv("RELAY_PRIVACY_MODE", legacy="MODELROUTER_PRIVACY_MODE") or "balanced").strip().lower()
        if privacy not in {"strict", "balanced", "permissive"}:
            privacy = "balanced"
        local_provider = (_getenv("RELAY_LOCAL_PROVIDER", legacy="MODELROUTER_LOCAL_PROVIDER") or "ollama").strip().lower()
        if local_provider not in LOCAL_PROVIDER_DEFAULTS:
            local_provider = "ollama"
        default_base, default_model = LOCAL_PROVIDER_DEFAULTS[local_provider]
        local_base = (
            os.getenv("LOCAL_BASE_URL")
            or os.getenv("OLLAMA_BASE_URL")
            or default_base
        ).rstrip("/")
        local_model = os.getenv("LOCAL_MODEL") or os.getenv("OLLAMA_MODEL") or default_model
        return cls(
            local_provider=local_provider,
            ollama_base_url=local_base,
            ollama_model=local_model,
            cloud_enabled=_bool("RELAY_ENABLE_CLOUD", False, legacy="MODELROUTER_ENABLE_CLOUD"),
            cloud_provider=_getenv("RELAY_CLOUD_PROVIDER", legacy="MODELROUTER_CLOUD_PROVIDER") or "openrouter",
            privacy_mode=privacy,  # type: ignore[arg-type]
            min_local_confidence=_float("RELAY_MIN_LOCAL_CONFIDENCE", 0.62, legacy="MODELROUTER_MIN_LOCAL_CONFIDENCE"),
            max_subtasks=max(1, _int("RELAY_MAX_SUBTASKS", 6, legacy="MODELROUTER_MAX_SUBTASKS")),
            openrouter_api_key=os.getenv("OPENROUTER_API_KEY") or None,
            openrouter_base_url=os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/"),
            openrouter_model=os.getenv("OPENROUTER_MODEL", "anthropic/claude-sonnet-4.6"),
            openrouter_site_url=os.getenv("OPENROUTER_SITE_URL") or None,
            openrouter_app_name=os.getenv("OPENROUTER_APP_NAME", "Relay"),
            openai_compat_base_url=os.getenv("OPENAI_COMPAT_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
            openai_compat_api_key=os.getenv("OPENAI_COMPAT_API_KEY") or None,
            openai_compat_model=os.getenv("OPENAI_COMPAT_MODEL", "gpt-5.5"),
            cloud_model_map=_str_map("RELAY_CLOUD_MODEL_MAP", DEFAULT_CLOUD_MODEL_MAP, legacy="MODELROUTER_CLOUD_MODEL_MAP"),
            request_timeout_seconds=_float("RELAY_TIMEOUT", 120.0, legacy="MODELROUTER_TIMEOUT"),
            web_search_enabled=_bool("RELAY_ENABLE_WEB_SEARCH", False),
            ollama_api_key=os.getenv("OLLAMA_API_KEY") or None,
            cloud_redaction=_bool("RELAY_REDACT_CLOUD", True),
        )
