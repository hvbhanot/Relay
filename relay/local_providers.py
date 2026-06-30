from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from .config import LOCAL_OPENAI_PROVIDERS, LOCAL_PROVIDER_DEFAULTS, RouterConfig
from .providers import ChatProvider, OllamaProvider, OpenAICompatibleProvider, ProviderError

_LOCAL_API_KEY = "local"


def normalize_local_provider(value: str | None) -> str:
    provider = (value or "ollama").strip().lower()
    if provider not in LOCAL_PROVIDER_DEFAULTS:
        return "ollama"
    return provider


def default_local_base_url(provider: str) -> str:
    return LOCAL_PROVIDER_DEFAULTS[normalize_local_provider(provider)][0]


def default_local_model(provider: str) -> str:
    return LOCAL_PROVIDER_DEFAULTS[normalize_local_provider(provider)][1]


def build_local_provider(config: RouterConfig) -> ChatProvider:
    provider = normalize_local_provider(config.local_provider)
    if provider == "ollama":
        return OllamaProvider(
            base_url=config.ollama_base_url,
            model=config.ollama_model,
            timeout_seconds=config.request_timeout_seconds,
        )
    return OpenAICompatibleProvider(
        base_url=config.ollama_base_url,
        api_key=_LOCAL_API_KEY,
        model=config.ollama_model,
        timeout_seconds=config.request_timeout_seconds,
        name=provider,
    )


def list_local_models(config: RouterConfig) -> dict[str, Any]:
    provider = normalize_local_provider(config.local_provider)
    base_url = config.ollama_base_url.rstrip("/")
    if provider == "ollama":
        url = f"{base_url}/api/tags"
        req = urllib.request.Request(url, method="GET", headers={"accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Could not reach Ollama at {base_url}: {exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Ollama at {base_url} returned invalid JSON") from exc
        models = data.get("models", []) if isinstance(data, dict) else []
        names = [item.get("name") for item in models if isinstance(item, dict) and item.get("name")]
        return {"models": names, "base_url": base_url, "provider": provider}

    if provider not in LOCAL_OPENAI_PROVIDERS:
        raise RuntimeError(f"Unsupported local provider: {provider}")

    provider_client = OpenAICompatibleProvider(
        base_url=base_url,
        api_key=_LOCAL_API_KEY,
        model=config.ollama_model,
        timeout_seconds=min(10.0, config.request_timeout_seconds),
        name=provider,
    )
    models_url = provider_client.base_url.rstrip("/")
    parsed_path = models_url.split("://", 1)[-1]
    if "/v1" in parsed_path:
        models_url = f"{models_url.rsplit('/v1', 1)[0]}/v1/models"
    else:
        models_url = f"{models_url}/v1/models"

    req = urllib.request.Request(models_url, method="GET", headers={"accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not reach {provider} at {base_url}: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{provider} at {base_url} returned invalid JSON") from exc

    raw_models = data.get("data", []) if isinstance(data, dict) else []
    names = [item.get("id") for item in raw_models if isinstance(item, dict) and item.get("id")]
    if not names and config.ollama_model:
        names = [config.ollama_model]
    return {"models": names, "base_url": base_url, "provider": provider}