from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .chat_history import DEFAULT_HISTORY_PATH
from .providers import ProviderError
from .ui_config import DEFAULT_UI_CONFIG_PATH
from .webui import WebUIApp, main as serve_main


def _build_app(args: argparse.Namespace) -> WebUIApp:
    return WebUIApp(
        config_path=args.config,
        history_path=args.history,
        load_dotenv=not args.no_dotenv,
    )


def _print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def cmd_plan(args: argparse.Namespace) -> int:
    args.plan_only = True
    args.stream = False
    return cmd_ask(args)


def cmd_ask(args: argparse.Namespace) -> int:
    message = (args.message or "").strip()
    if not message:
        print("error: message is required", file=sys.stderr)
        return 2

    app = _build_app(args)
    if args.plan_only:
        result = app.plan_chat(message)
        if args.json:
            _print_json(result)
        else:
            plan = result.get("plan") or {}
            print(plan.get("summary") or "Plan ready")
            for subtask in plan.get("subtasks") or []:
                title = subtask.get("title") or subtask.get("id")
                print(f"- {title}")
        return 0

    if args.stream:
        tokens: list[str] = []

        def on_event(event: dict[str, Any]) -> None:
            if event.get("type") == "token":
                chunk = str(event.get("text") or "")
                tokens.append(chunk)
                sys.stdout.write(chunk)
                sys.stdout.flush()

        try:
            result = app.run_chat(message, on_event=on_event)
        except ProviderError as exc:
            print(f"\nerror: {exc}", file=sys.stderr)
            return 1
        if not args.json:
            if tokens:
                sys.stdout.write("\n")
            return 0
        _print_json(result)
        return 0

    try:
        result = app.run_chat(message)
    except ProviderError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        _print_json(result)
        return 0

    print(result.get("answer") or "")
    return 0


def cmd_api(args: argparse.Namespace) -> int:
    from http.server import ThreadingHTTPServer

    from .webui import OpenAIAPIHandler

    app = _build_app(args)
    OpenAIAPIHandler.app = app
    server = ThreadingHTTPServer((args.host, args.port), OpenAIAPIHandler)
    url = f"http://{args.host}:{args.port}"
    print(f"Relay API listening on {url}")
    print("OpenAI-compatible endpoint: POST /v1/chat/completions")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down")
    finally:
        server.server_close()
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    argv = ["--host", args.host, "--port", str(args.port), "--config", str(args.config), "--history", str(args.history)]
    if args.no_dotenv:
        argv.append("--no-dotenv")
    if args.open:
        argv.append("--open")
    return serve_main(argv)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="relay", description="Relay — local-first model router")
    parser.add_argument("--config", default=str(DEFAULT_UI_CONFIG_PATH), help="UI setup JSON path")
    parser.add_argument("--history", default=str(DEFAULT_HISTORY_PATH), help="Chat history JSON path")
    parser.add_argument("--no-dotenv", action="store_true", help="Do not read .env before config")

    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="Run the browser UI and API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8090)
    serve.add_argument("--open", action="store_true", help="Open the UI in your default browser")
    serve.set_defaults(func=cmd_serve)

    ask = sub.add_parser("ask", help="Run a prompt headlessly")
    ask.add_argument("message", nargs="?", help="Prompt text")
    ask.add_argument("-m", "--message-opt", dest="message_flag", help="Prompt text")
    ask.add_argument("--stream", action="store_true", help="Stream tokens to stdout")
    ask.add_argument("--json", action="store_true", help="Print JSON response")
    ask.add_argument("--plan-only", action="store_true", help="Only run the planner")
    ask.set_defaults(func=cmd_ask)

    plan = sub.add_parser("plan", help="Preview the routing plan for a prompt")
    plan.add_argument("message", nargs="?", help="Prompt text")
    plan.add_argument("-m", "--message-opt", dest="message_flag", help="Prompt text")
    plan.add_argument("--json", action="store_true", help="Print JSON response")
    plan.set_defaults(func=cmd_plan)

    api = sub.add_parser("api", help="Run only the OpenAI-compatible API server")
    api.add_argument("--host", default="127.0.0.1")
    api.add_argument("--port", type=int, default=8091)
    api.set_defaults(func=cmd_api)

    args = parser.parse_args(argv)
    if args.command is None:
        return cmd_serve(
            argparse.Namespace(
                command="serve",
                config=args.config,
                history=args.history,
                no_dotenv=args.no_dotenv,
                host="127.0.0.1",
                port=8090,
                open=True,
                func=cmd_serve,
            )
        )

    if args.command in {"ask", "plan"}:
        if not args.message:
            args.message = getattr(args, "message_flag", None)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())