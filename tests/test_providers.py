from relay.providers import OpenAICompatibleProvider


def test_openai_provider_accepts_v1_base_url() -> None:
    provider = OpenAICompatibleProvider("https://api.example.com/v1", "key", "model")
    assert provider.chat_completions_url() == "https://api.example.com/v1/chat/completions"


def test_openai_provider_adds_v1_for_root_url() -> None:
    provider = OpenAICompatibleProvider("https://api.example.com", "key", "model")
    assert provider.chat_completions_url() == "https://api.example.com/v1/chat/completions"


def test_openai_provider_accepts_full_endpoint_url() -> None:
    provider = OpenAICompatibleProvider("https://api.example.com/v1/chat/completions", "key", "model")
    assert provider.chat_completions_url() == "https://api.example.com/v1/chat/completions"


def test_openai_provider_repairs_root_chat_completions_url() -> None:
    provider = OpenAICompatibleProvider("https://api.example.com/chat/completions", "key", "model")
    assert provider.chat_completions_url() == "https://api.example.com/v1/chat/completions"
