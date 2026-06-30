import base64

import pytest

from relay.attachments import (
    attachment_plan_hint,
    local_model_supports_vision,
    parse_attachment,
    parse_attachments,
    vision_required,
)


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def test_parse_text_attachment():
    item = parse_attachment(name="notes.md", mime="text/markdown", data=_b64("# Hello"))
    assert item.kind == "text"
    assert item.name == "notes.md"
    assert item.text_excerpt == "# Hello"
    assert item.size_bytes == 7


def test_parse_image_attachment():
    raw = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )
    item = parse_attachment(name="dot.png", mime="image/png", data=base64.b64encode(raw).decode("ascii"))
    assert item.kind == "image"
    assert item.text_excerpt is None
    assert vision_required([item]) is True


def test_parse_attachments_caps_at_eight():
    items = [{"name": f"f{i}.txt", "mime": "text/plain", "data": _b64("x")} for i in range(10)]
    parsed = parse_attachments(items)
    assert len(parsed) == 8


def test_rejects_oversized_attachment():
    big = "a" * (5 * 1024 * 1024 + 1)
    with pytest.raises(ValueError, match="5MB"):
        parse_attachment(name="big.txt", mime="text/plain", data=_b64(big))


def test_gemma4_cloud_variant_counts_as_local_vision() -> None:
    assert local_model_supports_vision("gemma4:31b-cloud") is True


def test_attachment_plan_hint_mentions_vision_and_split():
    text = parse_attachment(name="a.py", mime="text/plain", data=_b64("print(1)"))
    image = parse_attachment(
        name="shot.png",
        mime="image/png",
        data="iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==",
    )
    hint = attachment_plan_hint([text, image])
    assert "vision-capable" in hint
    assert "separate subtasks" in hint