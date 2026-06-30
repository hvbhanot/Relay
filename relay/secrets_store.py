from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

_SECRET_KEYS = ("openrouter_api_key", "openai_compat_api_key")
_PLACEHOLDER_API_KEYS = frozenset({"sk-live", "sk-test", "sk-secret"})
_MIN_USABLE_KEY_LEN = 20
_KEYCHAIN_SERVICE = "relay"
_VAULT_DIR = Path.home() / ".relay"
_VAULT_FILE = _VAULT_DIR / "secrets.enc"
_MASTER_KEY_FILE = _VAULT_DIR / "master.key"
_MIGRATED_FROM_KEYCHAIN = _VAULT_DIR / ".migrated_from_keychain"


def _fernet():
    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:  # pragma: no cover - exercised via fallback tests
        raise RuntimeError(
            "Install the cryptography package for encrypted secret storage: pip install cryptography"
        ) from exc
    return Fernet


def _load_master_key() -> bytes:
    if not _MASTER_KEY_FILE.exists():
        key = _fernet().generate_key()
        _VAULT_DIR.mkdir(parents=True, exist_ok=True)
        _MASTER_KEY_FILE.write_bytes(key)
    try:
        os.chmod(_MASTER_KEY_FILE, 0o600)
    except OSError:
        pass
    return _MASTER_KEY_FILE.read_bytes()


def _vault_read() -> dict[str, str]:
    if not _VAULT_FILE.exists():
        return {}
    try:
        token = _VAULT_FILE.read_bytes()
        raw = _fernet()(_load_master_key()).decrypt(token)
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: str(v) for k, v in data.items() if k in _SECRET_KEYS and isinstance(v, str) and v}


def _vault_write(secrets: dict[str, str]) -> None:
    _VAULT_DIR.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(secrets, sort_keys=True).encode("utf-8")
    token = _fernet()(_load_master_key()).encrypt(payload)
    _VAULT_FILE.write_bytes(token)
    try:
        os.chmod(_VAULT_FILE, 0o600)
    except OSError:
        pass


def _keychain_get(account: str) -> str | None:
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", _KEYCHAIN_SERVICE, "-a", account, "-w"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    value = result.stdout.strip()
    return value or None


def _keychain_delete(account: str) -> None:
    subprocess.run(
        ["security", "delete-generic-password", "-s", _KEYCHAIN_SERVICE, "-a", account],
        capture_output=True,
        check=False,
    )


def _migrate_keychain_to_vault_once() -> None:
    """One-time import from legacy macOS Keychain storage into the local vault."""
    if _MIGRATED_FROM_KEYCHAIN.exists():
        return
    secrets = _vault_read()
    changed = False
    for key in _SECRET_KEYS:
        if secrets.get(key):
            continue
        legacy = _keychain_get(key)
        if legacy:
            secrets[key] = legacy
            changed = True
        _keychain_delete(key)
    if changed:
        _vault_write(secrets)
    _VAULT_DIR.mkdir(parents=True, exist_ok=True)
    _MIGRATED_FROM_KEYCHAIN.write_text("ok\n")
    try:
        os.chmod(_MIGRATED_FROM_KEYCHAIN, 0o600)
    except OSError:
        pass


def storage_backend() -> str:
    return "vault"


def vault_path() -> str:
    return str(_VAULT_FILE.resolve())


def get_secret(name: str) -> str | None:
    if name not in _SECRET_KEYS:
        raise ValueError(f"Unknown secret: {name}")
    _migrate_keychain_to_vault_once()
    return _vault_read().get(name)


def set_secret(name: str, value: str) -> None:
    if name not in _SECRET_KEYS:
        raise ValueError(f"Unknown secret: {name}")
    _migrate_keychain_to_vault_once()
    cleaned = value.strip()
    if not cleaned:
        delete_secret(name)
        return
    secrets = _vault_read()
    secrets[name] = cleaned
    _vault_write(secrets)
    _keychain_delete(name)


def delete_secret(name: str) -> None:
    if name not in _SECRET_KEYS:
        raise ValueError(f"Unknown secret: {name}")
    _migrate_keychain_to_vault_once()
    secrets = _vault_read()
    secrets.pop(name, None)
    _vault_write(secrets)
    _keychain_delete(name)


def load_secrets() -> dict[str, str]:
    _migrate_keychain_to_vault_once()
    loaded: dict[str, str] = {}
    for key in _SECRET_KEYS:
        value = _vault_read().get(key)
        if value:
            loaded[key] = value
    return loaded


def migrate_inline_secrets(payload: dict[str, Any]) -> dict[str, Any]:
    """Move plaintext secrets from a UI JSON blob into the secure store."""
    for key in _SECRET_KEYS:
        raw = payload.get(key)
        if isinstance(raw, str) and raw.strip():
            set_secret(key, raw.strip())
    return {key: value for key, value in payload.items() if key not in _SECRET_KEYS}


def api_key_looks_usable(key: str | None) -> bool:
    """Heuristic guard against blank, test-stub, or obviously invalid API keys."""
    if not key or not str(key).strip():
        return False
    cleaned = str(key).strip()
    if cleaned in _PLACEHOLDER_API_KEYS:
        return False
    # Real OpenAI/OpenRouter keys are single-line secrets starting with sk-.
    # Prompts accidentally pasted into the key field (e.g. "make me a website…")
    # pass length checks but OpenRouter treats them as missing auth.
    if not cleaned.startswith("sk-"):
        return False
    if any(ch.isspace() for ch in cleaned):
        return False
    return len(cleaned) >= _MIN_USABLE_KEY_LEN


def secrets_public_metadata() -> dict[str, Any]:
    secrets = load_secrets()
    openrouter_key = secrets.get("openrouter_api_key")
    openai_compat_key = secrets.get("openai_compat_api_key")
    return {
        "secrets_backend": storage_backend(),
        "secrets_path": vault_path(),
        "has_openrouter_api_key": bool(openrouter_key),
        "openrouter_api_key_usable": api_key_looks_usable(openrouter_key),
        "has_openai_compat_api_key": bool(openai_compat_key),
        "openai_compat_api_key_usable": api_key_looks_usable(openai_compat_key),
    }