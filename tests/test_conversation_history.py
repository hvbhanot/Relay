import pytest

from relay.conversation_history import (
    conversation_prompt_block,
    parse_conversation_history,
    prompt_with_conversation,
)
from relay.schema import Message


def test_parse_conversation_history_filters_roles() -> None:
    parsed = parse_conversation_history(
        [
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello"},
            {"role": "system", "content": "ignored"},
            {"role": "user", "content": ""},
        ]
    )
    assert len(parsed) == 2
    assert parsed[0].role == "user"
    assert parsed[1].role == "assistant"


def test_prompt_with_conversation_includes_prior_turns() -> None:
    history = [Message("user", "What is 2+2?"), Message("assistant", "4")]
    prompt = prompt_with_conversation("Now times 3?", history)
    assert "Conversation so far:" in prompt
    assert "What is 2+2?" in prompt
    assert "Latest user message:" in prompt
    assert "Now times 3?" in prompt


def test_conversation_prompt_block_empty() -> None:
    assert conversation_prompt_block([]) == ""


def test_parse_conversation_history_requires_array() -> None:
    with pytest.raises(ValueError, match="array"):
        parse_conversation_history("bad")