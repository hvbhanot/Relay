import json
from pathlib import Path

import relay.secrets_store as secrets_store
from relay.config import RouterConfig
from relay.ui_config import (
    _LEGACY_UI_CONFIG_PATH,
    DEFAULT_UI_CONFIG_PATH,
    apply_ui_overrides,
    config_to_public_dict,
    load_ui_overrides,
    resolve_ui_config_path,
    save_ui_payload,
)


def _patch_secrets_store(monkeypatch, tmp_path: Path) -> None:
    vault_dir = tmp_path / "relay"
    monkeypatch.setattr(secrets_store, "_VAULT_DIR", vault_dir)
    monkeypatch.setattr(secrets_store, "_VAULT_FILE", vault_dir / "secrets.enc")
    monkeypatch.setattr(secrets_store, "_MASTER_KEY_FILE", vault_dir / "master.key")
    monkeypatch.setattr(secrets_store, "_MIGRATED_FROM_KEYCHAIN", vault_dir / ".migrated_from_keychain")


def test_ui_config_save_masks_secret_in_public_config(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    _patch_secrets_store(monkeypatch, tmp_path)
    path = tmp_path / "ui.json"
    config = save_ui_payload(
        {
            "ollama_model": "llama3.1:8b",
            "cloud_enabled": True,
            "cloud_provider": "openrouter",
            "openrouter_api_key": "sk-secret",
        },
        path=path,
    )
    assert config.openrouter_api_key == "sk-secret"
    public = config_to_public_dict(config, path=path)
    assert "openrouter_api_key" not in public
    assert public["has_openrouter_api_key"] is True
    saved = json.loads(path.read_text())
    assert "openrouter_api_key" not in saved


def test_ui_config_masks_openai_compat_secret(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_COMPAT_API_KEY", raising=False)
    _patch_secrets_store(monkeypatch, tmp_path)
    path = tmp_path / "ui.json"
    config = save_ui_payload(
        {
            "cloud_provider": "openai-compatible",
            "openai_compat_api_key": "sk-compat-secret",
            "openai_compat_base_url": "https://api.openai.com",
            "openai_compat_model": "gpt-5.5",
        },
        path=path,
    )
    assert config.openai_compat_api_key == "sk-compat-secret"
    public = config_to_public_dict(config, path=path)
    assert "openai_compat_api_key" not in public
    assert public["has_openai_compat_api_key"] is True


def test_blank_secret_preserves_saved_secret_and_clear_removes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    _patch_secrets_store(monkeypatch, tmp_path)
    path = tmp_path / "ui.json"
    save_ui_payload({"openrouter_api_key": "sk-secret"}, path=path)
    preserved = save_ui_payload({"openrouter_api_key": ""}, path=path)
    assert preserved.openrouter_api_key == "sk-secret"

    cleared = save_ui_payload({"openrouter_api_key": "", "openrouter_api_key_clear": True}, path=path)
    assert cleared.openrouter_api_key is None
    assert "openrouter_api_key" not in load_ui_overrides(path)


def test_blank_secret_in_overrides_keeps_existing_key(monkeypatch) -> None:
    # Reproduces the "Cloud provider is not configured" bug: the browser blanks the
    # password field after load, so a Ping cloud / preview sends an empty secret.
    # An empty secret must NOT wipe the already-configured key.
    monkeypatch.delenv("OPENAI_COMPAT_API_KEY", raising=False)
    base = RouterConfig.from_env(load_dotenv=False)
    configured = apply_ui_overrides(base, {"cloud_provider": "openai-compatible", "openai_compat_api_key": "sk-live"})
    assert configured.openai_compat_api_key == "sk-live"

    preview = apply_ui_overrides(
        configured,
        {"cloud_enabled": True, "cloud_provider": "openai-compatible", "openai_compat_api_key": "", "openai_compat_api_key_clear": False},
    )
    assert preview.openai_compat_api_key == "sk-live"
    assert preview.cloud_enabled is True


def test_new_key_wins_over_clear_flag_in_overrides(monkeypatch) -> None:
    # Reproduces the screenshot bug: the user typed a fresh key AND left "Clear
    # saved key" ticked. The new key must win, not be wiped by the clear flag,
    # otherwise Ping cloud reports "Cloud provider is not configured."
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    base = RouterConfig.from_env(load_dotenv=False)
    preview = apply_ui_overrides(
        base,
        {
            "cloud_enabled": True,
            "cloud_provider": "openrouter",
            "openrouter_api_key": "sk-fresh",
            "openrouter_api_key_clear": True,
        },
    )
    assert preview.openrouter_api_key == "sk-fresh"


def test_new_key_wins_over_clear_flag_on_save(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    _patch_secrets_store(monkeypatch, tmp_path)
    path = tmp_path / "ui.json"
    save_ui_payload({"cloud_provider": "openrouter", "openrouter_api_key": "sk-old"}, path=path)
    saved = save_ui_payload(
        {"openrouter_api_key": "sk-fresh", "openrouter_api_key_clear": True}, path=path
    )
    assert saved.openrouter_api_key == "sk-fresh"
    assert "openrouter_api_key" not in load_ui_overrides(path)
    assert secrets_store.get_secret("openrouter_api_key") == "sk-fresh"


def test_explicit_clear_flag_in_overrides_removes_key(monkeypatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    base = RouterConfig.from_env(load_dotenv=False)
    configured = apply_ui_overrides(base, {"openrouter_api_key": "sk-live"})
    cleared = apply_ui_overrides(configured, {"openrouter_api_key": "", "openrouter_api_key_clear": True})
    assert cleared.openrouter_api_key is None


def test_cloud_model_map_accepts_json_string_and_drops_bad_keys() -> None:
    base = RouterConfig.from_env(load_dotenv=False)
    config = apply_ui_overrides(
        base,
        {"cloud_model_map": '{"coding": "anthropic/claude-opus-4.8", "bogus_cap": "x", "math": ""}'},
    )
    # Unknown capability and blank model are dropped; valid entry is kept.
    assert config.cloud_model_map == {"coding": "anthropic/claude-opus-4.8"}


def test_cloud_model_map_invalid_json_keeps_existing() -> None:
    base = RouterConfig.from_env(load_dotenv=False)
    seeded = apply_ui_overrides(base, {"cloud_model_map": {"coding": "anthropic/claude-opus-4.8"}})
    # A typo (invalid JSON) must not silently empty the pool.
    kept = apply_ui_overrides(seeded, {"cloud_model_map": "{not valid json"})
    assert kept.cloud_model_map == {"coding": "anthropic/claude-opus-4.8"}


def test_resolve_ui_config_path_falls_back_to_legacy(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    legacy = tmp_path / _LEGACY_UI_CONFIG_PATH.name
    legacy.write_text('{"ollama_model": "legacy-model"}')
    assert resolve_ui_config_path().resolve() == legacy.resolve()
    assert load_ui_overrides()["ollama_model"] == "legacy-model"


def test_cloud_model_map_empty_object_is_honored() -> None:
    base = RouterConfig.from_env(load_dotenv=False)
    config = apply_ui_overrides(base, {"cloud_model_map": "{}"})
    assert config.cloud_model_map == {}


def test_apply_ui_overrides_sanitizes_numbers_and_provider() -> None:
    base = RouterConfig.from_env(load_dotenv=False)
    config = apply_ui_overrides(
        base,
        {
            "cloud_provider": "bad-provider",
            "privacy_mode": "bad-mode",
            "max_subtasks": 999,
            "min_local_confidence": -1,
        },
    )
    assert config.cloud_provider == base.cloud_provider
    assert config.privacy_mode == base.privacy_mode
    assert config.max_subtasks == 24
    assert config.min_local_confidence == 0.0
