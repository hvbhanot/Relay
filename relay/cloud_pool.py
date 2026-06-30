from __future__ import annotations

from .config import RouterConfig
from .providers import ChatProvider, OpenAICompatibleProvider

# Capability resolution order, strongest/most specialised need first. A subtask may
# declare several capabilities; the first one here that has a mapping wins, so a
# specialist need (e.g. reasoning) beats a generic one (e.g. general) on the same task.
CLOUD_MODEL_PRIORITY: list[str] = [
    "high_stakes",
    "reasoning",
    "math",
    "coding",
    "large_context",
    "sources",
    "current_info",
    "vision",
    "creative",
    "general",
]


def default_cloud_model(config: RouterConfig) -> str:
    if config.cloud_provider == "openai-compatible":
        return config.openai_compat_model
    return config.openrouter_model


def build_cloud_provider_for_model(config: RouterConfig, model: str) -> ChatProvider | None:
    """Build a provider for one specific cloud model, reusing the shared connection.

    For OpenRouter, a single key/base URL reaches many models, so the only thing
    that varies per pool entry is the model slug.
    """
    if config.cloud_provider == "openrouter":
        if not config.openrouter_api_key:
            return None
        headers: dict[str, str] = {"X-Title": config.openrouter_app_name}
        if config.openrouter_site_url:
            headers["HTTP-Referer"] = config.openrouter_site_url
        return OpenAICompatibleProvider(
            base_url=config.openrouter_base_url,
            api_key=config.openrouter_api_key,
            model=model,
            timeout_seconds=config.request_timeout_seconds,
            extra_headers=headers,
            name=f"openrouter:{model}",
        )
    if config.cloud_provider == "openai-compatible":
        if not config.openai_compat_api_key:
            return None
        return OpenAICompatibleProvider(
            base_url=config.openai_compat_base_url,
            api_key=config.openai_compat_api_key,
            model=model,
            timeout_seconds=config.request_timeout_seconds,
            name=f"openai-compatible:{model}",
        )
    raise ValueError(f"Unsupported cloud provider: {config.cloud_provider}")


class CloudPool:
    """A pool of cloud models reachable through one provider connection.

    Capability-based routing: each subtask carries capabilities (reasoning, coding,
    current_info, ...). The pool maps the highest-priority capability that has an
    entry in `cloud_model_map` to a specific model, falling back to the default
    cloud model. Providers are built once up front, so per-subtask resolution is a
    cheap dict lookup that is safe to call concurrently from worker threads.
    """

    def __init__(self, config: RouterConfig) -> None:
        self.default_model = default_cloud_model(config)
        # The capability map only applies to OpenRouter, where one key unlocks many
        # models. A single OpenAI-compatible endpoint keeps just its one model.
        self.model_map: dict[str, str] = (
            dict(config.cloud_model_map) if config.cloud_provider == "openrouter" else {}
        )
        self._providers: dict[str, ChatProvider] = {}
        for model in {self.default_model, *self.model_map.values()}:
            provider = build_cloud_provider_for_model(config, model)
            if provider is not None:
                self._providers[model] = provider
        # If the default model could not be built (no key), the pool is unusable.
        self.default_provider = self._providers.get(self.default_model)

    def is_active(self) -> bool:
        return self.default_provider is not None

    def models(self) -> list[str]:
        return sorted(self._providers)

    def select_model(self, capabilities: list[str]) -> str:
        caps = set(capabilities)
        for capability in CLOUD_MODEL_PRIORITY:
            if capability in caps and capability in self.model_map:
                return self.model_map[capability]
        return self.default_model

    def provider_for(self, capabilities: list[str]) -> tuple[ChatProvider | None, str]:
        model = self.select_model(capabilities)
        provider = self._providers.get(model)
        if provider is None:
            # A mapped model that failed to build: fall back to the default.
            return self.default_provider, self.default_model
        return provider, model
