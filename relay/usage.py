from __future__ import annotations

from typing import Any

from .schema import TokenUsage


def parse_openai_usage(data: dict[str, Any]) -> TokenUsage | None:
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return None
    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    total = int(usage.get("total_tokens") or prompt + completion)
    cost_raw = usage.get("cost")
    if cost_raw is None:
        cost_raw = data.get("cost")
    cost = float(cost_raw) if cost_raw is not None else None
    if prompt == 0 and completion == 0 and total == 0 and cost is None:
        return None
    return TokenUsage(prompt_tokens=prompt, completion_tokens=completion, total_tokens=total, cost_usd=cost)


def parse_ollama_usage(data: dict[str, Any]) -> TokenUsage | None:
    prompt = int(data.get("prompt_eval_count") or 0)
    completion = int(data.get("eval_count") or 0)
    if prompt == 0 and completion == 0:
        return None
    return TokenUsage(prompt_tokens=prompt, completion_tokens=completion, total_tokens=prompt + completion)


def merge_usage(*parts: TokenUsage | None) -> TokenUsage | None:
    entries = [part for part in parts if part is not None]
    if not entries:
        return None
    prompt = sum(item.prompt_tokens for item in entries)
    completion = sum(item.completion_tokens for item in entries)
    total = sum(item.total_tokens for item in entries)
    costs = [item.cost_usd for item in entries if item.cost_usd is not None]
    cost = sum(costs) if costs else None
    return TokenUsage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total or prompt + completion,
        cost_usd=cost,
    )


def usage_to_dict(usage: TokenUsage | None) -> dict[str, Any] | None:
    if usage is None:
        return None
    payload: dict[str, Any] = {
        "prompt_tokens": usage.prompt_tokens,
        "completion_tokens": usage.completion_tokens,
        "total_tokens": usage.total_tokens,
    }
    if usage.cost_usd is not None:
        payload["cost_usd"] = round(usage.cost_usd, 6)
    return payload


def aggregate_run_usage(entries: list[dict[str, Any]]) -> dict[str, Any]:
    prompt = sum(int(item.get("prompt_tokens") or 0) for item in entries)
    completion = sum(int(item.get("completion_tokens") or 0) for item in entries)
    total = sum(int(item.get("total_tokens") or 0) for item in entries)
    costs = [float(item["cost_usd"]) for item in entries if item.get("cost_usd") is not None]
    summary: dict[str, Any] = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total or prompt + completion,
        "calls": entries,
    }
    if costs:
        summary["cost_usd"] = round(sum(costs), 6)
    return summary