from dataclasses import replace

from relay.cloud_pool import CloudPool
from relay.config import RouterConfig


def _openrouter_config(**overrides) -> RouterConfig:
    base = RouterConfig.from_env(load_dotenv=False)
    values = {
        "cloud_enabled": True,
        "cloud_provider": "openrouter",
        "openrouter_api_key": "sk-test",
        "openrouter_model": "anthropic/claude-sonnet-4.6",
        "cloud_model_map": {
            "reasoning": "anthropic/claude-opus-4.8",
            "coding": "anthropic/claude-opus-4.8",
            "current_info": "openai/gpt-5.5",
        },
    }
    values.update(overrides)
    return replace(base, **values)


def test_pool_is_active_and_lists_distinct_models() -> None:
    pool = CloudPool(_openrouter_config())
    assert pool.is_active()
    assert pool.models() == [
        "anthropic/claude-opus-4.8",
        "anthropic/claude-sonnet-4.6",
        "openai/gpt-5.5",
    ]


def test_capability_selects_mapped_model() -> None:
    pool = CloudPool(_openrouter_config())
    assert pool.select_model(["coding"]) == "anthropic/claude-opus-4.8"
    assert pool.select_model(["current_info"]) == "openai/gpt-5.5"


def test_unmapped_capability_uses_default_model() -> None:
    pool = CloudPool(_openrouter_config())
    assert pool.select_model(["general"]) == "anthropic/claude-sonnet-4.6"
    assert pool.select_model(["creative"]) == "anthropic/claude-sonnet-4.6"
    assert pool.select_model([]) == "anthropic/claude-sonnet-4.6"


def test_priority_breaks_ties_between_capabilities() -> None:
    pool = CloudPool(_openrouter_config())
    # reasoning outranks current_info in CLOUD_MODEL_PRIORITY.
    assert pool.select_model(["current_info", "reasoning"]) == "anthropic/claude-opus-4.8"


def test_provider_for_returns_distinct_providers() -> None:
    pool = CloudPool(_openrouter_config())
    coding_provider, coding_model = pool.provider_for(["coding"])
    general_provider, general_model = pool.provider_for(["general"])
    assert coding_model == "anthropic/claude-opus-4.8"
    assert general_model == "anthropic/claude-sonnet-4.6"
    assert coding_provider is not general_provider
    assert coding_provider.model == "anthropic/claude-opus-4.8"


def test_pool_inactive_without_api_key() -> None:
    pool = CloudPool(_openrouter_config(openrouter_api_key=None))
    assert not pool.is_active()
    assert pool.default_provider is None


def test_openai_compatible_ignores_capability_map() -> None:
    base = RouterConfig.from_env(load_dotenv=False)
    config = replace(
        base,
        cloud_enabled=True,
        cloud_provider="openai-compatible",
        openai_compat_api_key="sk-test",
        openai_compat_model="gpt-5.5",
        cloud_model_map={"coding": "anthropic/claude-opus-4.8"},
    )
    pool = CloudPool(config)
    # Single-endpoint providers always use their one model, map is not applied.
    assert pool.models() == ["gpt-5.5"]
    assert pool.select_model(["coding"]) == "gpt-5.5"
