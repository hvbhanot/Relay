from __future__ import annotations

import argparse
import json
import mimetypes
import queue
import threading
import time
import webbrowser
from dataclasses import asdict, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .attachments import parse_attachments
from .conversation_history import parse_conversation_history
from .local_providers import list_local_models
from .openai_api import (
    completion_dict,
    error_response,
    models_dict,
    parse_chat_completion_request,
    route_mode_from_model,
    stream_finish,
    stream_start,
    stream_token,
)
from .chat_history import (
    DEFAULT_HISTORY_PATH,
    append_message,
    create_session,
    delete_session,
    ensure_active_session,
    get_session,
    list_sessions,
    pop_last_message,
    rename_session,
    set_active_session,
)
from .config import RouterConfig
from .feedback import DEFAULT_FEEDBACK_PATH, bias_note, confidence_bias, record_feedback
from .orchestrator import Relay, force_plan_route, plan_from_dict, plan_to_dict, trace_to_dict
from .providers import ProviderError
from .web_search import ollama_web_search
from .schema import Message
from .ui_config import (
    DEFAULT_UI_CONFIG_PATH,
    apply_ui_overrides,
    config_from_sources,
    config_to_public_dict,
    save_ui_payload,
)

ASSET_DIR = Path(__file__).with_name("webui_assets")


def _route_mode(payload: dict[str, Any]) -> str | None:
    """Read the optional per-request route override ("local"/"cloud"/None)."""
    mode = str(payload.get("route_mode") or "").strip().lower()
    return mode if mode in {"local", "cloud"} else None


class WebUIApp:
    def __init__(
        self,
        *,
        config_path: str | Path = DEFAULT_UI_CONFIG_PATH,
        history_path: str | Path = DEFAULT_HISTORY_PATH,
        feedback_path: str | Path = DEFAULT_FEEDBACK_PATH,
        load_dotenv: bool = True,
    ) -> None:
        self.config_path = Path(config_path)
        self.history_path = Path(history_path)
        self.feedback_path = Path(feedback_path)
        self.load_dotenv = load_dotenv
        self.lock = threading.RLock()
        self.config = config_from_sources(load_dotenv=load_dotenv, path=self.config_path)
        self.router = self._build_router()

    def _build_router(self) -> Relay:
        # Answer feedback shifts the local-confidence threshold over time.
        return Relay.from_config(self.config, confidence_bias=confidence_bias(self.feedback_path))

    def public_config(self) -> dict[str, Any]:
        with self.lock:
            return config_to_public_dict(self.config, path=self.config_path)

    def save_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            self.config = save_ui_payload(payload, path=self.config_path)
            self.router = self._build_router()
            return self.public_config()

    def available_models_for_plan(self) -> dict[str, list[str]]:
        with self.lock:
            router = self.router
        models = router.available_models()
        try:
            names = self.list_ollama_models().get("models") or []
            local = list(dict.fromkeys([*names, *models["local"]]))
            models["local"] = [name for name in local if name]
        except Exception:
            pass
        return models

    def plan_chat(
        self,
        message: str,
        *,
        attachments_data: Any = None,
        history_data: Any = None,
        route_mode: str | None = None,
    ) -> dict[str, Any]:
        with self.lock:
            router = self.router
        attachments = parse_attachments(attachments_data)
        history = parse_conversation_history(history_data)
        plan = force_plan_route(router.plan(message, attachments=attachments, history=history), route_mode)
        return {
            "message": message,
            "plan": plan_to_dict(plan),
            "previews": router.preview_routes(plan),
            "models": self.available_models_for_plan(),
            "attachments": [item.to_dict() for item in attachments],
        }

    def preview_plan_routes(
        self,
        plan_data: dict[str, Any],
        *,
        user_prompt: str = "",
        route_mode: str | None = None,
    ) -> dict[str, Any]:
        with self.lock:
            router = self.router
        plan = force_plan_route(
            plan_from_dict(plan_data, max_subtasks=router.max_subtasks, user_prompt=user_prompt),
            route_mode,
        )
        return {
            "plan": plan_to_dict(plan),
            "previews": router.preview_routes(plan),
            "models": self.available_models_for_plan(),
        }

    def run_chat(
        self,
        message: str,
        on_event: Any = None,
        *,
        plan_data: dict[str, Any] | None = None,
        attachments_data: Any = None,
        history_data: Any = None,
        route_mode: str | None = None,
    ) -> dict[str, Any]:
        # Copy the router reference so a setup save does not mutate a request in flight.
        with self.lock:
            router = self.router
        attachments = parse_attachments(attachments_data)
        history = parse_conversation_history(history_data)
        plan = None
        if plan_data is not None:
            plan = plan_from_dict(plan_data, max_subtasks=router.max_subtasks, user_prompt=message)
        trace = router.run(
            message,
            on_event=on_event,
            plan=plan,
            attachments=attachments,
            history=history,
            route_override=route_mode,
        )
        trace_dict = trace_to_dict(trace)
        usage = trace.metadata.get("usage") if isinstance(trace.metadata, dict) else None
        return {
            "answer": trace.final_answer,
            "trace": trace_dict,
            "usage": usage,
            "routes": [
                {
                    "id": result.subtask.id,
                    "title": result.subtask.title,
                    "route": result.route,
                    "model": result.model,
                    "confidence": result.confidence,
                    "reason": result.reason,
                    "error": result.error,
                    "duration_seconds": result.duration_seconds,
                    "usage": asdict(result.usage) if result.usage else None,
                    "sources": result.sources,
                }
                for result in trace.results
            ],
        }

    def preview_config(self, payload: dict[str, Any] | None = None) -> RouterConfig:
        with self.lock:
            config = self.config
        if payload:
            return apply_ui_overrides(config, payload)
        return config

    def list_local_models(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        config = self.preview_config(payload)
        return list_local_models(config)

    def list_ollama_models(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.list_local_models(payload)

    def openai_models(self) -> dict[str, Any]:
        try:
            listed = self.list_local_models()
            return models_dict(local_models=listed.get("models") or [])
        except Exception:
            return models_dict()

    def openai_chat_completions(self, payload: dict[str, Any], *, write: Any = None) -> dict[str, Any] | None:
        prompt, model, stream = parse_chat_completion_request(payload)
        route_mode = route_mode_from_model(model)
        if stream:
            completion_id, created, first = stream_start(model)
            if write is not None:
                write(first.encode("utf-8"))

            def on_event(event: dict[str, Any]) -> None:
                if event.get("type") != "token" or write is None:
                    return
                frame = stream_token(
                    completion_id=completion_id,
                    created=created,
                    model=model,
                    text=str(event.get("text") or ""),
                )
                write(frame.encode("utf-8"))

            result = self.run_chat(prompt, on_event=on_event, route_mode=route_mode)
            if write is not None:
                for frame in stream_finish(completion_id=completion_id, created=created, model=model):
                    write(frame.encode("utf-8"))
            return result

        result = self.run_chat(prompt, route_mode=route_mode)
        return completion_dict(text=str(result.get("answer") or ""), model=model)

    def test_local(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        config = self.preview_config(payload)
        router = Relay.from_config(replace(config, request_timeout_seconds=min(30, config.request_timeout_seconds)))
        started = time.time()
        result = router.local.complete(
            [Message("system", "Reply with exactly: ok"), Message("user", "ping")],
            temperature=0.0,
        )
        return {
            "ok": True,
            "provider": router.local.name,
            "latency_seconds": round(time.time() - started, 3),
            "response": result.text.strip()[:200],
        }

    def test_cloud(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        preview = self.preview_config(payload)
        config = replace(preview, request_timeout_seconds=min(30, preview.request_timeout_seconds))
        if not config.cloud_enabled:
            raise RuntimeError("Cloud fallback is disabled. Enable it in Setup first.")
        router = Relay.from_config(config)
        if router.cloud is None:
            raise RuntimeError("Cloud provider is not configured. Add an API key in Setup.")
        started = time.time()
        result = router.cloud.complete(
            [Message("system", "Reply with exactly: ok"), Message("user", "ping")],
            temperature=0.0,
        )
        return {
            "ok": True,
            "provider": router.cloud.name,
            "latency_seconds": round(time.time() - started, 3),
            "response": result.text.strip()[:200],
        }

    def test_search(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        config = self.preview_config(payload)
        started = time.time()
        results = ollama_web_search("Ollama latest release", config.ollama_api_key, max_results=3)
        return {
            "ok": True,
            "latency_seconds": round(time.time() - started, 3),
            "results": len(results),
            "sample": results[0]["title"] if results else "",
        }

    def record_answer_feedback(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            score = int(payload.get("score", 0))
        except (TypeError, ValueError):
            score = 0
        if score not in (-1, 1):
            raise ValueError("score must be 1 or -1")
        routes = payload.get("routes")
        all_local: bool | None = None
        models: list[str] = []
        if isinstance(routes, list) and routes:
            route_names = [str(r.get("route") or "") for r in routes if isinstance(r, dict)]
            all_local = bool(route_names) and all(name == "local" for name in route_names)
            models = [str(r.get("model")) for r in routes if isinstance(r, dict) and r.get("model")]
        with self.lock:
            record_feedback(score=score, all_local=all_local, models=models, path=self.feedback_path)
            bias = confidence_bias(self.feedback_path)
            self.router = self._build_router()
        return {"bias": bias, "note": bias_note(bias)}

    def history_list(self) -> dict[str, Any]:
        with self.lock:
            data = list_sessions(self.history_path)
            data["history_path"] = str(self.history_path.resolve())
            return data

    def history_get(self, session_id: str) -> dict[str, Any]:
        with self.lock:
            session = get_session(session_id, self.history_path)
        if session is None:
            raise ValueError(f"Unknown session: {session_id}")
        return session

    def history_create(self, *, title: str | None = None) -> dict[str, Any]:
        with self.lock:
            return create_session(title=title, path=self.history_path, set_active=True)

    def history_delete(self, session_id: str) -> None:
        with self.lock:
            if not delete_session(session_id, self.history_path):
                raise ValueError(f"Unknown session: {session_id}")

    def history_set_active(self, session_id: str | None) -> None:
        with self.lock:
            set_active_session(session_id, self.history_path)

    def history_append(
        self,
        session_id: str,
        *,
        role: str,
        content: str,
        trace: dict[str, Any] | None = None,
        attachments: Any = None,
    ) -> dict[str, Any]:
        with self.lock:
            return append_message(
                session_id,
                role=role,
                content=content,
                trace=trace,
                attachments=attachments,
                path=self.history_path,
            )

    def history_rename(self, session_id: str, title: str) -> dict[str, Any]:
        with self.lock:
            return rename_session(session_id, title, path=self.history_path)

    def history_pop(self, session_id: str, *, role: str | None = None) -> dict[str, Any] | None:
        with self.lock:
            return pop_last_message(session_id, role=role, path=self.history_path)

    def history_ensure_active(self) -> dict[str, Any]:
        with self.lock:
            return ensure_active_session(self.history_path)


class _RelayHTTPMixin:
    app: WebUIApp

    def _path(self) -> str:
        return self.path.split("?", 1)[0]

    def _handle_openai_get(self) -> bool:
        if self._path() == "/v1/models":
            self._send_json(self.app.openai_models())
            return True
        return False

    def _handle_openai_post(self, payload: dict[str, Any]) -> bool:
        if self._path() != "/v1/chat/completions":
            return False
        stream = bool(payload.get("stream"))
        if stream:
            # Close the connection at end-of-stream so the client sees EOF instead
            # of hanging on a kept-alive socket after the final chunk.
            self.close_connection = True
            self.send_response(200)
            self.send_header("content-type", "text/event-stream; charset=utf-8")
            self.send_header("cache-control", "no-cache")
            self.send_header("connection", "close")
            self.end_headers()

            def write(chunk: bytes) -> None:
                self.wfile.write(chunk)
                self.wfile.flush()

            try:
                self.app.openai_chat_completions(payload, write=write)
            except Exception as exc:  # noqa: BLE001
                error, status = error_response(exc)
                frame = f"data: {json.dumps(error, ensure_ascii=False)}\n\n"
                write(frame.encode("utf-8"))
            return True

        try:
            response = self.app.openai_chat_completions(payload)
            self._send_json(response)
        except Exception as exc:  # noqa: BLE001
            error, status = error_response(exc)
            self._send_json(error, status=status)
        return True


class WebUIHandler(_RelayHTTPMixin, BaseHTTPRequestHandler):
    app: WebUIApp

    def do_GET(self) -> None:  # noqa: N802
        try:
            if self._handle_openai_get():
                return
            if self.path == "/" or self.path == "/index.html":
                self._send_file(ASSET_DIR / "index.html")
                return
            if self._path() in ("/docs", "/docs/"):
                self._send_file(ASSET_DIR / "docs.html")
                return
            if self.path.startswith("/assets/"):
                name = self.path.removeprefix("/assets/").split("?", 1)[0]
                asset_path = (ASSET_DIR / name).resolve()
                if ASSET_DIR.resolve() not in asset_path.parents:
                    self.send_error(403)
                    return
                self._send_file(asset_path)
                return
            if self.path == "/api/config":
                self._send_json({"ok": True, "config": self.app.public_config()})
                return
            if self.path == "/api/ollama/models":
                self._send_json({"ok": True, **self.app.list_ollama_models()})
                return
            if self.path == "/api/history":
                self._send_json({"ok": True, **self.app.history_list()})
                return
            if self.path.startswith("/api/history/"):
                session_id = self.path.removeprefix("/api/history/").split("?", 1)[0].strip("/")
                if not session_id or "/" in session_id:
                    self.send_error(404, "not found")
                    return
                self._send_json({"ok": True, "session": self.app.history_get(session_id)})
                return
            self.send_error(404, "not found")
        except Exception as exc:  # noqa: BLE001
            self._send_error(exc)

    def do_DELETE(self) -> None:  # noqa: N802
        try:
            if self.path.startswith("/api/history/"):
                session_id = self.path.removeprefix("/api/history/").split("?", 1)[0].strip("/")
                if not session_id or "/" in session_id:
                    self.send_error(404, "not found")
                    return
                self.app.history_delete(session_id)
                self._send_json({"ok": True})
                return
            self.send_error(404, "not found")
        except Exception as exc:  # noqa: BLE001
            self._send_error(exc)

    def do_POST(self) -> None:  # noqa: N802
        try:
            payload = self._read_json()
            if self._handle_openai_post(payload):
                return
            if self.path == "/api/config":
                config = self.app.save_config(payload)
                self._send_json({"ok": True, "config": config})
                return
            if self.path == "/api/chat":
                message = str(payload.get("message", "")).strip()
                if not message:
                    raise ValueError("message is required")
                self._send_json({"ok": True, **self.app.run_chat(message)})
                return
            if self.path == "/api/chat/plan":
                message = str(payload.get("message", "")).strip()
                if not message:
                    raise ValueError("message is required")
                self._send_json({
                    "ok": True,
                    **self.app.plan_chat(
                        message,
                        attachments_data=payload.get("attachments"),
                        history_data=payload.get("history"),
                        route_mode=_route_mode(payload),
                    ),
                })
                return
            if self.path == "/api/chat/preview-routes":
                plan_data = payload.get("plan")
                if not isinstance(plan_data, dict):
                    raise ValueError("plan is required")
                message = str(payload.get("message", "")).strip()
                self._send_json({
                    "ok": True,
                    **self.app.preview_plan_routes(plan_data, user_prompt=message, route_mode=_route_mode(payload)),
                })
                return
            if self.path == "/api/chat/stream":
                message = str(payload.get("message", "")).strip()
                if not message:
                    raise ValueError("message is required")
                plan_data = payload.get("plan")
                attachments_data = payload.get("attachments")
                self._stream_chat(
                    message,
                    plan_data if isinstance(plan_data, dict) else None,
                    attachments_data=attachments_data,
                    history_data=payload.get("history"),
                    route_mode=_route_mode(payload),
                )
                return
            if self.path == "/api/feedback":
                self._send_json({"ok": True, **self.app.record_answer_feedback(payload)})
                return
            if self.path == "/api/test-search":
                self._send_json(self.app.test_search(payload))
                return
            if self.path == "/api/history/deactivate":
                self.app.history_set_active(None)
                self._send_json({"ok": True})
                return
            if self.path == "/api/history":
                title = payload.get("title")
                session = self.app.history_create(title=str(title).strip() if title else None)
                self._send_json({"ok": True, "session": session})
                return
            if self.path.startswith("/api/history/") and self.path.endswith("/activate"):
                session_id = self.path.removeprefix("/api/history/").removesuffix("/activate").strip("/")
                self.app.history_set_active(session_id)
                self._send_json({"ok": True, "session_id": session_id})
                return
            if self.path.startswith("/api/history/") and self.path.endswith("/messages"):
                session_id = self.path.removeprefix("/api/history/").removesuffix("/messages").strip("/")
                role = str(payload.get("role", "")).strip()
                content = str(payload.get("content", "")).strip()
                if not role or not content:
                    raise ValueError("role and content are required")
                trace = payload.get("trace")
                message = self.app.history_append(
                    session_id,
                    role=role,
                    content=content,
                    trace=trace if isinstance(trace, dict) else None,
                    attachments=payload.get("attachments"),
                )
                self._send_json({"ok": True, "message": message})
                return
            if self.path.startswith("/api/history/") and self.path.endswith("/title"):
                session_id = self.path.removeprefix("/api/history/").removesuffix("/title").strip("/")
                title = str(payload.get("title", "")).strip()
                if not title:
                    raise ValueError("title is required")
                session = self.app.history_rename(session_id, title)
                self._send_json({"ok": True, "session": {"id": session.get("id"), "title": session.get("title")}})
                return
            if self.path.startswith("/api/history/") and self.path.endswith("/pop"):
                session_id = self.path.removeprefix("/api/history/").removesuffix("/pop").strip("/")
                role = str(payload.get("role", "")).strip() or None
                removed = self.app.history_pop(session_id, role=role)
                self._send_json({"ok": True, "removed": removed is not None})
                return
            if self.path == "/api/test-local":
                self._send_json(self.app.test_local(payload))
                return
            if self.path == "/api/test-cloud":
                self._send_json(self.app.test_cloud(payload))
                return
            if self.path == "/api/ollama/models":
                self._send_json({"ok": True, **self.app.list_local_models(payload)})
                return
            self.send_error(404, "not found")
        except Exception as exc:  # noqa: BLE001
            self._send_error(exc)

    def _stream_chat(
        self,
        message: str,
        plan_data: dict[str, Any] | None = None,
        *,
        attachments_data: Any = None,
        history_data: Any = None,
        route_mode: str | None = None,
    ) -> None:
        """Run a chat and stream phase events to the browser as Server-Sent Events.

        The orchestration runs on a worker thread and pushes events onto a queue;
        this request thread drains the queue and writes one SSE frame per event,
        so the UI can animate planning -> routing -> synthesizing in real time.
        """
        events: "queue.Queue[dict[str, Any] | None]" = queue.Queue()

        def emit(event: dict[str, Any]) -> None:
            events.put(event)

        def worker() -> None:
            try:
                result = self.app.run_chat(
                    message,
                    on_event=emit,
                    plan_data=plan_data,
                    attachments_data=attachments_data,
                    history_data=history_data,
                    route_mode=route_mode,
                )
                events.put({"type": "done", **result})
            except ProviderError as exc:
                events.put({"type": "error", "error": {"type": type(exc).__name__, "message": str(exc)}})
            except Exception as exc:  # noqa: BLE001
                events.put({"type": "error", "error": {"type": type(exc).__name__, "message": str(exc)}})
            finally:
                events.put(None)  # sentinel: stream complete

        threading.Thread(target=worker, daemon=True).start()

        # Close the connection when the stream ends. Without this the socket stays
        # open (keep-alive) after the final event, so a browser reading the body as
        # a stream never sees EOF — its read loop hangs and any work it does after
        # the stream (e.g. saving the answer to history) never runs.
        self.close_connection = True
        self.send_response(200)
        self.send_header("content-type", "text/event-stream; charset=utf-8")
        self.send_header("cache-control", "no-cache")
        self.send_header("connection", "close")
        self.send_header("x-accel-buffering", "no")
        self.end_headers()

        while True:
            event = events.get()
            if event is None:
                break
            frame = f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode("utf-8")
            try:
                self.wfile.write(frame)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                # Client navigated away; stop streaming. The worker thread will
                # finish its in-flight model calls and exit on its own.
                break

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}")

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("content-length", "0"))
        body = self.rfile.read(length).decode("utf-8") if length else "{}"
        data = json.loads(body)
        if not isinstance(data, dict):
            raise ValueError("JSON body must be an object")
        return data

    def _send_json(self, payload: dict[str, Any], *, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, exc: Exception) -> None:
        status = 502 if isinstance(exc, ProviderError) else 400
        self._send_json(
            {
                "ok": False,
                "error": {
                    "type": type(exc).__name__,
                    "message": str(exc),
                },
            },
            status=status,
        )

    def _send_file(self, path: Path) -> None:
        if not path.exists() or not path.is_file():
            self.send_error(404, "not found")
            return
        body = path.read_bytes()
        mime_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("content-type", mime_type)
        self.send_header("content-length", str(len(body)))
        # Never cache UI assets: this is a local dev tool that updates in place, and
        # stale cached CSS/JS is far more confusing than re-fetching a few KB.
        self.send_header("cache-control", "no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(body)


class OpenAIAPIHandler(_RelayHTTPMixin, BaseHTTPRequestHandler):
    app: WebUIApp

    def do_GET(self) -> None:  # noqa: N802
        try:
            if self._handle_openai_get():
                return
            self.send_error(404, "not found")
        except Exception as exc:  # noqa: BLE001
            error, status = error_response(exc)
            self._send_json(error, status=status)

    def do_POST(self) -> None:  # noqa: N802
        try:
            payload = self._read_json()
            if self._handle_openai_post(payload):
                return
            self.send_error(404, "not found")
        except Exception as exc:  # noqa: BLE001
            error, status = error_response(exc)
            self._send_json(error, status=status)

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}")

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("content-length", "0"))
        body = self.rfile.read(length).decode("utf-8") if length else "{}"
        data = json.loads(body)
        if not isinstance(data, dict):
            raise ValueError("JSON body must be an object")
        return data

    def _send_json(self, payload: dict[str, Any], *, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the browser UI for Relay")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--config", default=str(DEFAULT_UI_CONFIG_PATH), help="UI setup JSON path")
    parser.add_argument("--history", default=str(DEFAULT_HISTORY_PATH), help="Chat history JSON path")
    parser.add_argument("--no-dotenv", action="store_true", help="Do not read .env before UI config")
    parser.add_argument("--open", action="store_true", help="Open the UI in your default browser")
    args = parser.parse_args(argv)

    WebUIHandler.app = WebUIApp(
        config_path=args.config,
        history_path=args.history,
        load_dotenv=not args.no_dotenv,
    )
    server = ThreadingHTTPServer((args.host, args.port), WebUIHandler)
    url = f"http://{args.host}:{args.port}"
    print(f"Relay UI listening on {url}")
    print(f"Setup is saved to {Path(args.config).resolve()}")
    print(f"Chat history is saved to {Path(args.history).resolve()}")
    print("OpenAI-compatible endpoint: POST /v1/chat/completions")
    print("Press Ctrl+C to stop.")
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
