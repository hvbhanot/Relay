from relay.openai_api import completion_dict, messages_to_prompt, parse_chat_completion_request


def test_messages_to_prompt_uses_last_single_user_message() -> None:
    prompt = messages_to_prompt([{"role": "user", "content": "hello"}])
    assert prompt == "hello"


def test_messages_to_prompt_joins_conversation() -> None:
    prompt = messages_to_prompt(
        [
            {"role": "system", "content": "Be concise"},
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello"},
            {"role": "user", "content": "Summarize Relay"},
        ]
    )
    assert "System: Be concise" in prompt
    assert "User: Summarize Relay" in prompt


def test_parse_chat_completion_request() -> None:
    prompt, model, stream = parse_chat_completion_request(
        {"model": "relay", "stream": True, "messages": [{"role": "user", "content": "ping"}]}
    )
    assert prompt == "ping"
    assert model == "relay"
    assert stream is True


def test_completion_dict_shape() -> None:
    payload = completion_dict(text="ok", model="relay")
    assert payload["object"] == "chat.completion"
    assert payload["choices"][0]["message"]["content"] == "ok"