from pathlib import Path

import relay.secrets_store as secrets_store
from relay.providers import CompletionResult
from relay.webui import ASSET_DIR, WebUIApp, WebUIHandler


def _patch_secrets_store(monkeypatch, tmp_path: Path) -> None:
    vault_dir = tmp_path / "relay"
    monkeypatch.setattr(secrets_store, "_VAULT_DIR", vault_dir)
    monkeypatch.setattr(secrets_store, "_VAULT_FILE", vault_dir / "secrets.enc")
    monkeypatch.setattr(secrets_store, "_MASTER_KEY_FILE", vault_dir / "master.key")
    monkeypatch.setattr(secrets_store, "_MIGRATED_FROM_KEYCHAIN", vault_dir / ".migrated_from_keychain")


def test_webui_assets_exist() -> None:
    assert (ASSET_DIR / "index.html").exists()
    assert (ASSET_DIR / "style.css").exists()
    assert (ASSET_DIR / "app.js").exists()
    assert (ASSET_DIR / "logo.png").exists()
    assert (ASSET_DIR / "logo-icon.png").exists()


def test_history_api_routes(tmp_path: Path) -> None:
    from http.server import ThreadingHTTPServer
    from threading import Thread

    history_path = tmp_path / "history.json"
    app = WebUIApp(config_path=tmp_path / "ui.json", history_path=history_path, load_dotenv=False)
    WebUIHandler.app = app
    server = ThreadingHTTPServer(("127.0.0.1", 0), WebUIHandler)
    port = server.server_address[1]
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{port}"

        def request(method: str, path: str, body: dict | None = None) -> dict:
            import json
            import urllib.request

            data = None if body is None else json.dumps(body).encode("utf-8")
            req = urllib.request.Request(
                f"{base}{path}",
                data=data,
                method=method,
                headers={"content-type": "application/json"} if data is not None else {},
            )
            with urllib.request.urlopen(req) as res:
                return json.loads(res.read().decode("utf-8"))

        listed = request("GET", "/api/history")
        assert listed["ok"] is True
        assert listed["sessions"] == []

        created = request("POST", "/api/history", {"title": "Test chat"})
        session_id = created["session"]["id"]
        assert created["ok"] is True

        fetched = request("GET", f"/api/history/{session_id}")
        assert fetched["session"]["title"] == "Test chat"

        appended = request(
            "POST",
            f"/api/history/{session_id}/messages",
            {"role": "user", "content": "hello"},
        )
        assert appended["message"]["content"] == "hello"

        request("POST", "/api/history/deactivate", {})
        listed = request("GET", "/api/history")
        assert listed["active_session_id"] is None
    finally:
        server.shutdown()
        server.server_close()


def test_history_deactivate_clears_active_session(tmp_path: Path) -> None:
    from relay.chat_history import create_session, list_sessions

    history_path = tmp_path / "history.json"
    app = WebUIApp(config_path=tmp_path / "ui.json", history_path=history_path, load_dotenv=False)
    session = create_session(path=history_path, title="Old chat")
    assert list_sessions(history_path)["active_session_id"] == session["id"]
    app.history_set_active(None)
    assert list_sessions(history_path)["active_session_id"] is None


def test_webui_app_public_config(tmp_path: Path) -> None:
    app = WebUIApp(config_path=tmp_path / "ui.json", load_dotenv=False)
    public = app.public_config()
    assert public["local_provider"] == "ollama"
    assert "openrouter_api_key" not in public


def test_test_cloud_uses_saved_key_when_field_blank(tmp_path: Path, monkeypatch) -> None:
    # End-to-end guard for the "Cloud provider is not configured" bug behind the
    # Ping cloud button. The browser blanks the password field after save, so the
    # ping payload carries an empty secret; it must still resolve to the saved key.
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    _patch_secrets_store(monkeypatch, tmp_path)
    app = WebUIApp(config_path=tmp_path / "ui.json", load_dotenv=False)
    app.save_config({"cloud_enabled": True, "cloud_provider": "openrouter", "openrouter_api_key": "sk-live"})

    calls: list[str] = []

    def fake_complete(self, messages, *, temperature: float = 0.2) -> CompletionResult:
        calls.append("called")
        return CompletionResult(text="ok")

    monkeypatch.setattr(
        "relay.providers.OpenAICompatibleProvider.complete",
        fake_complete,
    )

    # Simulate the exact payload the UI sends when the password box is empty.
    result = app.test_cloud(
        {"cloud_enabled": True, "cloud_provider": "openrouter", "openrouter_api_key": "", "openrouter_api_key_clear": False}
    )
    assert result["ok"] is True
    assert result["provider"].startswith("openrouter:")
    assert calls == ["called"]
