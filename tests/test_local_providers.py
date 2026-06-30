from dataclasses import replace

from relay.config import RouterConfig
from relay.local_providers import build_local_provider
from relay.providers import OllamaProvider, OpenAICompatibleProvider


def test_build_ollama_provider() -> None:
    config = replace(RouterConfig.from_env(load_dotenv=False), local_provider="ollama")
    provider = build_local_provider(config)
    assert isinstance(provider, OllamaProvider)


def test_build_lmstudio_provider() -> None:
    config = replace(
        RouterConfig.from_env(load_dotenv=False),
        local_provider="lmstudio",
        ollama_base_url="http://localhost:1234/v1",
        ollama_model="my-model",
    )
    provider = build_local_provider(config)
    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider.name == "lmstudio"
    assert provider.chat_completions_url() == "http://localhost:1234/v1/chat/completions"