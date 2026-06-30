from pathlib import Path

import pytest

import relay.secrets_store as secrets_store


@pytest.fixture(autouse=True)
def isolated_vault(monkeypatch, tmp_path: Path):
    vault_dir = tmp_path / "relay"
    vault_file = vault_dir / "secrets.enc"
    master_key = vault_dir / "master.key"
    monkeypatch.setattr(secrets_store, "_VAULT_DIR", vault_dir)
    monkeypatch.setattr(secrets_store, "_VAULT_FILE", vault_file)
    monkeypatch.setattr(secrets_store, "_MASTER_KEY_FILE", master_key)
    monkeypatch.setattr(secrets_store, "_MIGRATED_FROM_KEYCHAIN", vault_dir / ".migrated_from_keychain")
    yield


def test_secret_roundtrip() -> None:
    secrets_store.set_secret("openrouter_api_key", "sk-test")
    assert secrets_store.get_secret("openrouter_api_key") == "sk-test"
    secrets_store.delete_secret("openrouter_api_key")
    assert secrets_store.get_secret("openrouter_api_key") is None


def test_api_key_looks_usable_rejects_placeholders() -> None:
    assert secrets_store.api_key_looks_usable("sk-live") is False
    assert secrets_store.api_key_looks_usable("sk-test") is False
    assert secrets_store.api_key_looks_usable("sk-or-v1-" + "a" * 40) is True
    assert secrets_store.api_key_looks_usable("make me a website, thanks") is False
    assert secrets_store.api_key_looks_usable("sk-or-v1-key with spaces") is False


def test_storage_backend_is_local_vault() -> None:
    assert secrets_store.storage_backend() == "vault"


def test_migrate_inline_secrets_strips_plaintext() -> None:
    payload = {"ollama_model": "llama3.1:8b", "openrouter_api_key": "sk-move"}
    cleaned = secrets_store.migrate_inline_secrets(payload)
    assert "openrouter_api_key" not in cleaned
    assert secrets_store.get_secret("openrouter_api_key") == "sk-move"