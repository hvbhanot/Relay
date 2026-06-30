from relay.usage import aggregate_run_usage, parse_openai_usage, parse_ollama_usage


def test_parse_openai_usage_with_cost() -> None:
    usage = parse_openai_usage(
        {
            "usage": {
                "prompt_tokens": 12,
                "completion_tokens": 34,
                "total_tokens": 46,
                "cost": 0.00125,
            }
        }
    )
    assert usage is not None
    assert usage.prompt_tokens == 12
    assert usage.completion_tokens == 34
    assert usage.cost_usd == 0.00125


def test_parse_ollama_usage() -> None:
    usage = parse_ollama_usage({"prompt_eval_count": 10, "eval_count": 20})
    assert usage is not None
    assert usage.prompt_tokens == 10
    assert usage.completion_tokens == 20
    assert usage.total_tokens == 30


def test_aggregate_run_usage() -> None:
    summary = aggregate_run_usage(
        [
            {"label": "subtask", "prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost_usd": 0.01},
            {"label": "synthesis", "prompt_tokens": 20, "completion_tokens": 8, "total_tokens": 28},
        ]
    )
    assert summary["prompt_tokens"] == 30
    assert summary["completion_tokens"] == 13
    assert summary["cost_usd"] == 0.01
    assert len(summary["calls"]) == 2