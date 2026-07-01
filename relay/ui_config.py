from __future__ import annotations

import json
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from .config import LOCAL_PROVIDER_DEFAULTS, RouterConfig
from .schema import Capability
from .secrets_store import delete_secret, load_secrets, migrate_inline_secrets, secrets_public_metadata, set_secret

DEFAULT_UI_CONFIG_PATH = Path("relay.ui.json")
_LEGACY_UI_CONFIG_PATH = Path("modelrouter.ui.json")


def resolve_ui_config_path(path: str | Path | None = None) -> Path:
    if path is not None:
        return Path(path)
    if DEFAULT_UI_CONFIG_PATH.exists():
        return DEFAULT_UI_CONFIG_PATH
    if _LEGACY_UI_CONFIG_PATH.exists():
        return _LEGACY_UI_CONFIG_PATH
    return DEFAULT_UI_CONFIG_PATH

_CAPABILITY_KEYS = {c.value for c in Capability}

_PUBLIC_KEYS = {
    "local_provider",
    "ollama_base_url",
    "ollama_model",
    "cloud_enabled",
    "cloud_provider",
    "privacy_mode",
    "min_local_confidence",
    "max_subtasks",
    "openrouter_base_url",
    "openrouter_model",
    "openrouter_site_url",
    "openrouter_app_name",
    "openai_compat_base_url",
    "openai_compat_model",
    "cloud_model_map",
    "request_timeout_seconds",
    "web_search_enabled",
    "cloud_redaction",
}

_SECRET_KEYS = {"openrouter_api_key", "openai_compat_api_key", "ollama_api_key"}
_UI_ONLY_KEYS = {"planner_preview_enabled"}
_UI_ONLY_DEFAULTS = {"planner_preview_enabled": True}
_PRIVACY_MODES = {"strict", "balanced", "permissive"}
_CLOUD_PROVIDERS = {"openrouter", "openai-compatible"}
_LOCAL_PROVIDERS = set(LOCAL_PROVIDER_DEFAULTS)


def load_ui_overrides(path: str | Path | None = None) -> dict[str, Any]:
    config_path = resolve_ui_config_path(path)
    if not config_path.exists():
        return {}
    try:
        data = json.loads(config_path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    cleaned = migrate_inline_secrets(data)
    if any(key in data for key in _SECRET_KEYS):
        config_path.write_text(json.dumps(cleaned, indent=2, sort_keys=True) + "\n")
    return cleaned


def config_from_sources(
    *,
    load_dotenv: bool = True,
    path: str | Path | None = None,
) -> RouterConfig:
    config_path = resolve_ui_config_path(path)
    base = RouterConfig.from_env(load_dotenv=load_dotenv)
    overrides = load_ui_overrides(config_path)
    overrides.update(load_secrets())
    return apply_ui_overrides(base, overrides)


def apply_ui_overrides(config: RouterConfig, overrides: dict[str, Any]) -> RouterConfig:
    values: dict[str, Any] = {}

    for key in _PUBLIC_KEYS | _SECRET_KEYS:
        if key in overrides:
            values[key] = overrides[key]

    if "local_provider" in values and values["local_provider"] not in _LOCAL_PROVIDERS:
        values["local_provider"] = config.local_provider
    if "cloud_provider" in values and values["cloud_provider"] not in _CLOUD_PROVIDERS:
        values["cloud_provider"] = config.cloud_provider
    if "privacy_mode" in values and values["privacy_mode"] not in _PRIVACY_MODES:
        values["privacy_mode"] = config.privacy_mode

    for key in ("ollama_base_url", "openrouter_base_url", "openai_compat_base_url"):
        if key in values and isinstance(values[key], str):
            values[key] = values[key].strip().rstrip("/")

    if "cloud_model_map" in values:
        values["cloud_model_map"] = _sanitize_model_map(values["cloud_model_map"], config.cloud_model_map)

    for key in ("cloud_enabled", "web_search_enabled", "cloud_redaction"):
        if key in values:
            values[key] = _coerce_bool(values[key], getattr(config, key))

    int_fields = {"max_subtasks": (1, 24)}
    for key, (low, high) in int_fields.items():
        if key in values:
            values[key] = _coerce_int(values[key], getattr(config, key), low, high)

    float_fields = {
        "min_local_confidence": (0.0, 1.0),
        "request_timeout_seconds": (1.0, 600.0),
    }
    for key, (low, high) in float_fields.items():
        if key in values:
            values[key] = _coerce_float(values[key], getattr(config, key), low, high)

    # Secret handling precedence, highest first:
    #   1. A freshly provided non-blank key wins, even if `<key>_clear` is also set.
    #      (Typing a new key and ticking "clear" means "replace it", not "wipe it".)
    #   2. An explicit `<key>_clear` flag removes the stored secret.
    #   3. A blank input keeps the currently configured key. The browser UI always
    #      blanks password fields after load for safety, so without this rule a plain
    #      Save / Ping cloud would wipe the saved key and the app would wrongly report
    #      "Cloud provider is not configured."
    for key in _SECRET_KEYS:
        clear_key = f"{key}_clear"
        raw = values.get(key)
        cleaned = raw.strip() if isinstance(raw, str) else raw
        if cleaned not in ("", None):
            values[key] = cleaned
        elif _coerce_bool(overrides.get(clear_key), False):
            values[key] = None
        else:
            # Blank input and no clear: leave the existing config value untouched.
            values.pop(key, None)

    return replace(config, **values)


def config_to_public_dict(
    config: RouterConfig,
    *,
    path: str | Path | None = None,
) -> dict[str, Any]:
    config_path = resolve_ui_config_path(path)
    data = asdict(config)
    public = {key: data[key] for key in _PUBLIC_KEYS}
    public.update(secrets_public_metadata())
    public["config_path"] = str(config_path.resolve())
    saved = load_ui_overrides(config_path)
    for key, default in _UI_ONLY_DEFAULTS.items():
        value = saved.get(key, default)
        if key == "planner_preview_enabled":
            public[key] = _coerce_bool(value, default)
        else:
            public[key] = value
    return public


def save_ui_payload(
    payload: dict[str, Any],
    *,
    path: str | Path | None = None,
) -> RouterConfig:
    """Persist UI setup payload and return the resulting effective config.

    Secret fields are intentionally write-only from the browser. If a secret input is
    left blank, the existing saved value is preserved. Set `<secret>_clear=true` to
    remove a saved key.
    """
    config_path = resolve_ui_config_path(path)
    current_saved = load_ui_overrides(config_path)
    base = RouterConfig.from_env(load_dotenv=True)
    current_effective = apply_ui_overrides(base, current_saved)

    to_save: dict[str, Any] = {}
    for key in _PUBLIC_KEYS:
        if key in payload:
            to_save[key] = payload[key]
        elif key in current_saved:
            to_save[key] = current_saved[key]

    for key in _UI_ONLY_KEYS:
        if key in payload:
            to_save[key] = payload[key]
        elif key in current_saved:
            to_save[key] = current_saved[key]

    secret_overrides: dict[str, Any] = {}
    for key in _SECRET_KEYS:
        clear_key = f"{key}_clear"
        incoming = payload.get(key)
        if isinstance(incoming, str) and incoming.strip():
            secret_overrides[key] = incoming.strip()
        elif _coerce_bool(payload.get(clear_key), False):
            secret_overrides[clear_key] = True
        elif key in load_secrets():
            secret_overrides[key] = load_secrets()[key]

    # Normalize through the same sanitizer before writing.
    effective = apply_ui_overrides(current_effective, {**to_save, **secret_overrides})
    normalized = asdict(effective)
    persisted = {key: normalized[key] for key in _PUBLIC_KEYS}

    for key in _SECRET_KEYS:
        clear_key = f"{key}_clear"
        incoming = payload.get(key)
        if isinstance(incoming, str) and incoming.strip():
            set_secret(key, incoming.strip())
        elif _coerce_bool(payload.get(clear_key), False):
            delete_secret(key)
        elif normalized.get(key):
            set_secret(key, str(normalized[key]))
    for key in _UI_ONLY_KEYS:
        if key in to_save:
            if key == "planner_preview_enabled":
                persisted[key] = _coerce_bool(to_save[key], _UI_ONLY_DEFAULTS[key])
            else:
                persisted[key] = to_save[key]

    config_path.write_text(json.dumps(persisted, indent=2, sort_keys=True) + "\n")
    return effective


def _sanitize_model_map(value: Any, default: dict[str, str]) -> dict[str, str]:
    """Validate a capability -> cloud-model map from the UI/JSON.

    Accepts an object or a JSON string. Keeps only known capability keys mapped to
    non-empty model slugs. Invalid JSON or a non-object falls back to the current
    map so a typo never silently empties the cloud pool. A valid-but-empty object
    is honoured (every cloud subtask then uses the default model).
    """
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return dict(default)
    if not isinstance(value, dict):
        return dict(default)
    cleaned: dict[str, str] = {}
    for key, model in value.items():
        if key in _CAPABILITY_KEYS and isinstance(model, str) and model.strip():
            cleaned[key] = model.strip()
    return cleaned


def _coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}
    return default


def _coerce_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(low, min(high, parsed))


def _coerce_float(value: Any, default: float, low: float, high: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(low, min(high, parsed))
