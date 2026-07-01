from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from .providers import ProviderError

# Ollama's hosted web search API. Needs an API key from an ollama.com account
# (https://ollama.com/settings/keys); the free tier is enough for personal use.
OLLAMA_WEB_SEARCH_URL = "https://ollama.com/api/web_search"
OLLAMA_WEB_FETCH_URL = "https://ollama.com/api/web_fetch"

_MAX_SNIPPET_CHARS = 700
_MAX_PAGE_CHARS = 3000


def ollama_web_search(
    query: str,
    api_key: str | None,
    *,
    max_results: int = 4,
    timeout: float = 15.0,
) -> list[dict[str, str]]:
    """Run one web search and return [{title, url, content}, ...].

    Raises ProviderError on any transport or auth failure so callers can decide
    whether a missing search is fatal (test button) or ignorable (subtask run).
    """
    cleaned = " ".join((query or "").split())
    if not cleaned:
        return []
    if not api_key or not str(api_key).strip():
        raise ProviderError(
            "Web search needs an Ollama API key. Create one at ollama.com/settings/keys "
            "and paste it in Settings → Web search."
        )
    body = json.dumps({"query": cleaned, "max_results": max(1, min(10, max_results))}).encode("utf-8")
    req = urllib.request.Request(
        OLLAMA_WEB_SEARCH_URL,
        data=body,
        headers={
            "content-type": "application/json",
            "accept": "application/json",
            "authorization": f"Bearer {str(api_key).strip()}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise ProviderError(f"Web search failed (HTTP {exc.code}): {detail}") from exc
    except urllib.error.URLError as exc:
        raise ProviderError(f"Could not reach {OLLAMA_WEB_SEARCH_URL}: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise ProviderError("Web search returned invalid JSON") from exc
    return _parse_results(data)


def _parse_results(data: Any) -> list[dict[str, str]]:
    if not isinstance(data, dict):
        return []
    raw = data.get("results")
    if not isinstance(raw, list):
        return []
    results: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        content = " ".join(str(item.get("content") or "").split())
        results.append(
            {
                "title": str(item.get("title") or url).strip(),
                "url": url,
                "content": content[:_MAX_SNIPPET_CHARS],
            }
        )
    return results


def ollama_web_fetch(url: str, api_key: str | None, *, timeout: float = 15.0) -> str:
    """Fetch one page's readable content via Ollama's web_fetch API (truncated)."""
    if not url or not api_key or not str(api_key).strip():
        return ""
    body = json.dumps({"url": url}).encode("utf-8")
    req = urllib.request.Request(
        OLLAMA_WEB_FETCH_URL,
        data=body,
        headers={
            "content-type": "application/json",
            "accept": "application/json",
            "authorization": f"Bearer {str(api_key).strip()}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError, TimeoutError, OSError) as exc:
        raise ProviderError(f"Web fetch failed for {url}: {exc}") from exc
    if not isinstance(data, dict):
        return ""
    content = " ".join(str(data.get("content") or "").split())
    return content[:_MAX_PAGE_CHARS]


def search_results_block(results: list[dict[str, str]]) -> str:
    """Format search results as a prompt block for a worker model."""
    if not results:
        return ""
    lines = [
        "Web search results (fetched just now; cite the URL when you use one):",
    ]
    for index, result in enumerate(results, start=1):
        lines.append(f"{index}. {result['title']} — {result['url']}")
        if result.get("content"):
            lines.append(f"   {result['content']}")
    return "\n".join(lines)
