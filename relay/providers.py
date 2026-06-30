from __future__ import annotations

import json
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from .schema import Message, TokenUsage
from .usage import parse_ollama_usage, parse_openai_usage

TokenCallback = Callable[[str], None]


@dataclass(slots=True)
class CompletionResult:
    text: str
    usage: TokenUsage | None = None


class ProviderError(RuntimeError):
    pass


class ChatProvider(ABC):
    name: str

    @abstractmethod
    def complete(self, messages: list[Message], *, temperature: float = 0.2) -> CompletionResult:
        raise NotImplementedError

    def stream(
        self,
        messages: list[Message],
        *,
        temperature: float = 0.2,
        on_token: TokenCallback | None = None,
    ) -> CompletionResult:
        """Stream the reply token-by-token, returning the full text and usage if known."""
        result = self.complete(messages, temperature=temperature)
        if on_token and result.text:
            on_token(result.text)
        return result


@dataclass(slots=True)
class OllamaProvider(ChatProvider):
    base_url: str = "http://localhost:11434"
    model: str = "llama3.1:8b"
    timeout_seconds: float = 120.0
    name: str = "ollama"

    @staticmethod
    def _normalize_image_b64(value: str) -> str:
        if value.startswith("data:") and "," in value:
            return value.split(",", 1)[1]
        return value

    @staticmethod
    def _ollama_messages(messages: list[Message]) -> list[dict[str, Any]]:
        payload_messages: list[dict[str, Any]] = []
        for message in messages:
            item: dict[str, Any] = {"role": message.role, "content": message.content}
            if message.images:
                item["images"] = [OllamaProvider._normalize_image_b64(image) for image in message.images]
            payload_messages.append(item)
        return payload_messages

    def complete(self, messages: list[Message], *, temperature: float = 0.2) -> CompletionResult:
        payload = {
            "model": self.model,
            "messages": self._ollama_messages(messages),
            "stream": False,
            "options": {"temperature": temperature},
        }
        data = _post_json(f"{self.base_url.rstrip('/')}/api/chat", payload, timeout=self.timeout_seconds)
        try:
            text = str(data["message"]["content"])
        except (KeyError, TypeError) as exc:
            raise ProviderError(f"Unexpected Ollama response: {data!r}") from exc
        return CompletionResult(text=text, usage=parse_ollama_usage(data))

    def stream(
        self,
        messages: list[Message],
        *,
        temperature: float = 0.2,
        on_token: TokenCallback | None = None,
    ) -> CompletionResult:
        payload = {
            "model": self.model,
            "messages": self._ollama_messages(messages),
            "stream": True,
            "options": {"temperature": temperature},
        }
        url = f"{self.base_url.rstrip('/')}/api/chat"
        parts: list[str] = []
        usage: TokenUsage | None = None
        for line in _stream_lines(url, payload, timeout=self.timeout_seconds):
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            chunk = str((data.get("message") or {}).get("content") or "")
            if chunk:
                parts.append(chunk)
                if on_token:
                    on_token(chunk)
            if data.get("done"):
                usage = parse_ollama_usage(data)
                break
        return CompletionResult(text="".join(parts), usage=usage)


@dataclass(slots=True)
class OpenAICompatibleProvider(ChatProvider):
    base_url: str
    api_key: str
    model: str
    timeout_seconds: float = 120.0
    extra_headers: dict[str, str] | None = None
    name: str = "openai-compatible"

    def chat_completions_url(self) -> str:
        """Return the final chat-completions endpoint.

        UI users commonly paste one of three shapes:
        - an OpenAI-compatible base URL: https://api.example.com/v1
        - a provider root URL: https://api.example.com
        - the full endpoint: https://api.example.com/v1/chat/completions

        Accept all three. For root URLs, prefer the standard OpenAI-compatible
        `/v1/chat/completions` path; this fixes providers where `/chat/completions`
        at the root returns 404.
        """
        base = self.base_url.rstrip("/")
        parsed = urlparse(base)
        path = parsed.path.rstrip("/")
        if path == "/chat/completions":
            root = base[: -len("/chat/completions")]
            return f"{root}/v1/chat/completions"
        if base.endswith("/chat/completions"):
            return base

        if not path:
            return f"{base}/v1/chat/completions"
        return f"{base}/chat/completions"

    @staticmethod
    def _openai_content(message: Message) -> str | list[dict[str, Any]]:
        if not message.images:
            return message.content
        parts: list[dict[str, Any]] = [{"type": "text", "text": message.content}]
        for image in message.images:
            url = image if image.startswith("data:") else f"data:image/jpeg;base64,{image}"
            parts.append({"type": "image_url", "image_url": {"url": url}})
        return parts

    def complete(self, messages: list[Message], *, temperature: float = 0.2) -> CompletionResult:
        payload = {
            "model": self.model,
            "messages": [{"role": m.role, "content": self._openai_content(m)} for m in messages],
            "temperature": temperature,
        }
        headers = {"Authorization": f"Bearer {self.api_key}"}
        if self.extra_headers:
            headers.update(self.extra_headers)
        data = _post_json(
            self.chat_completions_url(),
            payload,
            headers=headers,
            timeout=self.timeout_seconds,
        )
        try:
            text = str(data["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"Unexpected chat completion response: {data!r}") from exc
        return CompletionResult(text=text, usage=parse_openai_usage(data))

    def stream(
        self,
        messages: list[Message],
        *,
        temperature: float = 0.2,
        on_token: TokenCallback | None = None,
    ) -> CompletionResult:
        payload = {
            "model": self.model,
            "messages": [{"role": m.role, "content": self._openai_content(m)} for m in messages],
            "temperature": temperature,
            "stream": True,
        }
        headers = {"Authorization": f"Bearer {self.api_key}"}
        if self.extra_headers:
            headers.update(self.extra_headers)
        parts: list[str] = []
        usage: TokenUsage | None = None
        for line in _stream_lines(self.chat_completions_url(), payload, headers=headers, timeout=self.timeout_seconds):
            if not line.startswith("data:"):
                continue
            data_str = line[len("data:"):].strip()
            if data_str == "[DONE]":
                break
            try:
                data = json.loads(data_str)
            except json.JSONDecodeError:
                continue
            usage = parse_openai_usage(data) or usage
            try:
                delta = str(((data.get("choices") or [{}])[0].get("delta") or {}).get("content") or "")
            except (AttributeError, IndexError, TypeError):
                delta = ""
            if delta:
                parts.append(delta)
                if on_token:
                    on_token(delta)
        return CompletionResult(text="".join(parts), usage=usage)


def _stream_lines(
    url: str,
    payload: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 120.0,
) -> Iterator[str]:
    """POST and yield non-empty response lines as they arrive (NDJSON or SSE)."""
    body = json.dumps(payload).encode("utf-8")
    request_headers = {"content-type": "application/json", "accept": "application/json"}
    if headers:
        request_headers.update(headers)
    req = urllib.request.Request(url, data=body, headers=request_headers, method="POST")
    try:
        response = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise ProviderError(f"HTTP {exc.code} from {url}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise ProviderError(f"Could not reach {url}: {exc.reason}") from exc
    with response:
        for raw in response:
            line = raw.decode("utf-8", errors="replace").strip()
            if line:
                yield line


def _post_json(
    url: str,
    payload: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 120.0,
) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    request_headers = {"content-type": "application/json", "accept": "application/json"}
    if headers:
        request_headers.update(headers)
    req = urllib.request.Request(url, data=body, headers=request_headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            response_body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise ProviderError(f"HTTP {exc.code} from {url}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise ProviderError(f"Could not reach {url}: {exc.reason}") from exc

    try:
        decoded = json.loads(response_body)
    except json.JSONDecodeError as exc:
        raise ProviderError(f"Non-JSON response from {url}: {response_body[:500]}") from exc
    if not isinstance(decoded, dict):
        raise ProviderError(f"Unexpected JSON response from {url}: {decoded!r}")
    return decoded
