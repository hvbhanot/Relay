from __future__ import annotations

import concurrent.futures
import json
import re
import time
from collections.abc import Callable
from dataclasses import asdict
from typing import Any

from .attachments import (
    Attachment,
    attachment_plan_hint,
    has_images,
    image_payloads,
    local_model_supports_vision,
    planning_prompt,
    vision_required,
)
from .cloud_pool import CloudPool
from .config import RouterConfig
from .conversation_history import conversation_prompt_block, prompt_with_conversation
from .local_providers import build_local_provider
from .policy import RoutingPolicy
from .secrets_store import api_key_looks_usable
from .prompts import evaluator_messages, planner_messages, subtask_messages, synthesis_messages
from .providers import ChatProvider, CompletionResult, OllamaProvider, ProviderError
from .schema import Capability, Message, Plan, RouteDecision, RouteName, RouterTrace, Subtask, SubtaskResult, TokenUsage
from .usage import aggregate_run_usage, merge_usage, usage_to_dict
from .util import clamp_float, extract_json_object


class Relay:
    def __init__(
        self,
        local_provider: ChatProvider,
        cloud_provider: ChatProvider | None,
        policy: RoutingPolicy,
        *,
        max_subtasks: int = 6,
        cloud_pool: CloudPool | None = None,
    ) -> None:
        self.local = local_provider
        self.cloud = cloud_provider
        self.policy = policy
        self.max_subtasks = max_subtasks
        # When present, the pool picks a per-subtask cloud model by capability.
        # When absent, cloud routing falls back to the single `cloud` provider, so
        # directly-constructed routers keep their original single-model behaviour.
        self._cloud_pool = cloud_pool
        self._attachments: list[Attachment] = []
        self._history: list[Message] = []
        self._local_vision_cache: bool | None = None

    @classmethod
    def from_config(cls, config: RouterConfig | None = None) -> "Relay":
        config = config or RouterConfig.from_env()
        local = build_local_provider(config)
        cloud: ChatProvider | None = None
        cloud_pool: CloudPool | None = None
        if config.cloud_enabled:
            pool = CloudPool(config)
            if pool.is_active():
                cloud_pool = pool
                cloud = pool.default_provider
        return cls(
            local,
            cloud,
            RoutingPolicy(
                cloud_enabled=config.cloud_enabled and cloud is not None,
                privacy_mode=config.privacy_mode,
                min_local_confidence=config.min_local_confidence,
            ),
            max_subtasks=config.max_subtasks,
            cloud_pool=cloud_pool,
        )

    def _resolve_cloud(self, subtask: Subtask) -> tuple[ChatProvider | None, str | None]:
        """Pick the cloud provider + model for this subtask's capabilities."""
        if self._cloud_pool is not None:
            return self._cloud_pool.provider_for(subtask.capabilities)
        if self.cloud is not None:
            return self.cloud, getattr(self.cloud, "model", None)
        return None, None

    def available_models(self) -> dict[str, list[str]]:
        local_model = getattr(self.local, "model", None)
        local_models = [local_model] if local_model else []
        cloud_models = self._cloud_pool.models() if self._cloud_pool else []
        if not cloud_models and self.cloud is not None:
            cloud_model = getattr(self.cloud, "model", None)
            if cloud_model:
                cloud_models = [cloud_model]
        return {"local": local_models, "cloud": cloud_models}

    def _local_provider_for(self, model: str) -> ChatProvider:
        if getattr(self.local, "model", None) == model:
            return self.local
        return OllamaProvider(
            base_url=getattr(self.local, "base_url", "http://localhost:11434"),
            model=model,
            timeout_seconds=getattr(self.local, "timeout_seconds", 120.0),
        )

    def _resolve_execution_target(
        self,
        subtask: Subtask,
        decision: RouteDecision,
    ) -> tuple[ChatProvider, str, RouteName]:
        """Resolve provider, model slug, and effective route for one subtask."""
        override = (subtask.model_override or "").strip()
        if override:
            if self._cloud_pool is not None and override in self._cloud_pool._providers:
                return self._cloud_pool._providers[override], override, "cloud"
            if self.cloud is not None and self.policy.cloud_enabled:
                return self.cloud, override, "cloud"
            return self._local_provider_for(override), override, "local"

        cloud_provider, cloud_model = self._resolve_cloud(subtask)
        use_cloud = decision.route == "cloud" and cloud_provider is not None
        if use_cloud:
            effective_model = cloud_model or getattr(cloud_provider, "model", None) or "cloud"
            return cloud_provider, effective_model, "cloud"
        local_model = getattr(self.local, "model", None) or "local"
        return self.local, local_model, "local"

    def run(
        self,
        user_prompt: str,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        *,
        plan: Plan | None = None,
        attachments: list[Attachment] | None = None,
        history: list[Message] | None = None,
    ) -> RouterTrace:
        def emit(event: dict[str, Any]) -> None:
            if on_event is not None:
                on_event(event)

        self._attachments = list(attachments or [])
        self._history = list(history or [])
        try:
            self._ensure_vision_support()
            self._ensure_cloud_auth()
            if plan is None:
                emit({"type": "planning"})
                plan = self.plan(user_prompt, attachments=self._attachments, history=self._history)
            self._emit_planned(plan, emit)
            return self.execute(user_prompt, plan, emit)
        finally:
            self._attachments = []
            self._history = []

    def _cloud_api_key(self) -> str | None:
        if self.cloud is None:
            return None
        key = getattr(self.cloud, "api_key", None)
        return str(key).strip() if isinstance(key, str) and key.strip() else None

    def _ensure_cloud_auth(self) -> None:
        if not self.policy.cloud_enabled or self.cloud is None:
            return
        # Unit-test fakes may omit api_key; only enforce on real cloud providers.
        if not hasattr(self.cloud, "api_key"):
            return
        key = self._cloud_api_key()
        if api_key_looks_usable(key):
            return
        if key and not str(key).strip().startswith("sk-"):
            raise ProviderError(
                "Cloud API key is invalid: it must be an OpenRouter secret starting with sk-or-v1-, "
                "not chat text. Open Settings → Cloud fallback, paste your key from "
                "https://openrouter.ai/keys, save, then click Ping cloud."
            )
        raise ProviderError(
            "Cloud fallback is enabled but no valid API key is saved. "
            "Open Settings → Cloud fallback, paste your OpenRouter key, save, then click Ping cloud."
        )

    def _local_supports_vision(self) -> bool:
        if self._local_vision_cache is not None:
            return self._local_vision_cache
        local_model = getattr(self.local, "model", "") or ""
        base_url = getattr(self.local, "base_url", None)
        self._local_vision_cache = local_model_supports_vision(local_model, base_url=base_url)
        return self._local_vision_cache

    def _cloud_ready_for_vision(self) -> bool:
        return self.policy.cloud_enabled and self.cloud is not None and api_key_looks_usable(self._cloud_api_key())

    def _vision_setup_error(self, *, local_ok: bool) -> str:
        local_model = getattr(self.local, "model", "") or "local model"
        if local_ok:
            return (
                "Attached images require a vision-capable model. "
                "Your local model supports vision; cloud routing was unavailable."
            )
        if self.policy.cloud_enabled and self.cloud is not None and not api_key_looks_usable(self._cloud_api_key()):
            return (
                f"Attached images need a vision model. Your local model ({local_model}) cannot analyze images. "
                "For local vision: run `ollama pull llava`, set Local model to `llava` in Settings, then try again. "
                "For cloud vision: paste a valid OpenRouter API key in Settings and use Test Cloud."
            )
        return (
            f"Attached images need a vision model. Your local model ({local_model}) cannot analyze images. "
            "Run `ollama pull llava`, set Local model to `llava` in Settings, then upload the image again."
        )

    def _ensure_vision_support(self) -> None:
        if not vision_required(self._attachments):
            return
        local_ok = self._local_supports_vision()
        cloud_ok = self._cloud_ready_for_vision()
        if not local_ok and not cloud_ok:
            raise ProviderError(self._vision_setup_error(local_ok=local_ok))

    def _images_for_subtask(self, subtask: Subtask) -> list[str]:
        if not self._attachments or Capability.VISION.value not in subtask.capabilities:
            return []
        return image_payloads(self._attachments)

    def _conversation_block(self) -> str:
        return conversation_prompt_block(self._history)

    def execute(
        self,
        user_prompt: str,
        plan: Plan,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> RouterTrace:
        def emit(event: dict[str, Any]) -> None:
            if on_event is not None:
                on_event(event)

        # Fast path: a single subtask IS the final answer, so stream it straight to
        # the user token-by-token instead of running worker -> evaluator -> synthesis.
        # This is what makes simple requests feel responsive on slow local models.
        if len(plan.subtasks) == 1 and not plan.subtasks[0].depends_on and not self._attachments:
            return self._run_single(user_prompt, plan, emit)

        route_decisions: dict[str, RouteDecision] = {}
        usage_calls: list[dict[str, Any]] = []

        pending = list(plan.subtasks)
        completed: dict[str, SubtaskResult] = {}

        # Execute simple dependency levels. Independent tasks within a level run concurrently.
        while pending:
            ready = [task for task in pending if all(dep in completed for dep in task.depends_on)]
            if not ready:
                # Broken or cyclic dependency from model output: degrade gracefully by unblocking all.
                ready = pending[:]

            for task in ready:
                emit({"type": "subtask_start", "id": task.id, "title": task.title})

            workers = max(1, min(len(ready), self.max_subtasks))
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
                future_map = {
                    executor.submit(
                        self._execute_subtask,
                        user_prompt,
                        task,
                        list(completed.values()),
                        plan.requires_online,
                    ): task
                    for task in ready
                }
                for future in concurrent.futures.as_completed(future_map):
                    result, decision, calls = future.result()
                    completed[result.subtask.id] = result
                    route_decisions[result.subtask.id] = decision
                    usage_calls.extend(calls)
                    self._emit_subtask_done(result, emit)

            pending = [task for task in pending if task.id not in completed]

        results = [completed[s.id] for s in plan.subtasks if s.id in completed]

        auth_failure = _vision_auth_failure(results)
        if auth_failure:
            emit({"type": "answering"})
            emit({"type": "token", "text": auth_failure})
            usage_summary = aggregate_run_usage(usage_calls) if usage_calls else None
            return RouterTrace(
                user_prompt=user_prompt,
                plan=plan,
                route_decisions=route_decisions,
                results=results,
                final_answer=auth_failure,
                metadata=self._metadata(usage_summary=usage_summary),
            )

        # Merge subtask answers into the final response, streaming the synthesis
        # tokens to the user as they are produced.
        emit({"type": "synthesizing"})
        emit({"type": "answering"})
        final_answer, synthesis_usage = self._synthesize_stream(
            user_prompt, plan, results, lambda chunk: emit({"type": "token", "text": chunk})
        )
        # Safety net: a small local synthesis model often abbreviates or drops the code
        # the cloud workers produced ("refer to the subtask results…"). The user never
        # sees those results, so append verbatim any substantial file the synthesis lost.
        # The UI re-renders from the final answer, so recovered files still appear.
        final_answer = _ensure_deliverables_included(final_answer, results)
        if synthesis_usage is not None:
            usage_calls.append(
                self._usage_call(
                    "synthesis",
                    synthesis_usage,
                    route="local",
                    model=getattr(self.local, "model", None),
                )
            )
        usage_summary = aggregate_run_usage(usage_calls) if usage_calls else None
        return RouterTrace(
            user_prompt=user_prompt,
            plan=plan,
            route_decisions=route_decisions,
            results=results,
            final_answer=final_answer,
            metadata=self._metadata(usage_summary=usage_summary),
        )

    def preview_routes(self, plan: Plan) -> list[dict[str, Any]]:
        """Predict local/cloud routing for each planned subtask before execution."""
        previews: list[dict[str, Any]] = []
        for subtask in plan.subtasks:
            decision = self.policy.decide_for_subtask(
                subtask,
                plan_requires_online=plan.requires_online,
                local_supports_vision=self._local_supports_vision(),
            )
            provider, model, route = self._resolve_execution_target(subtask, decision)
            previews.append(
                {
                    "id": subtask.id,
                    "title": subtask.title,
                    "prompt": subtask.prompt,
                    "preferred_route": subtask.preferred_route,
                    "capabilities": list(subtask.capabilities),
                    "sensitivity": subtask.sensitivity,
                    "depends_on": list(subtask.depends_on),
                    "rationale": subtask.rationale,
                    "model_override": subtask.model_override,
                    "predicted_route": route,
                    "predicted_model": model,
                    "reason": decision.reason if not subtask.model_override else f"Model override: {model}.",
                }
            )
        return previews

    @staticmethod
    def _emit_planned(plan: Plan, emit: Callable[[dict[str, Any]], None]) -> None:
        emit(
            {
                "type": "planned",
                "summary": plan.summary,
                "requires_online": plan.requires_online,
                "subtasks": [{"id": s.id, "title": s.title} for s in plan.subtasks],
            }
        )

    def _metadata(self, *, usage_summary: dict[str, Any] | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "local_provider": self.local.name,
            "cloud_provider": self.cloud.name if self.cloud else None,
            "cloud_models": self._cloud_pool.models() if self._cloud_pool else [],
        }
        if usage_summary:
            payload["usage"] = usage_summary
        return payload

    @staticmethod
    def _preview_text(content: str, *, limit: int = 120) -> str:
        text = " ".join(content.split())
        if len(text) <= limit:
            return text
        return text[: limit - 1].rstrip() + "…"

    @staticmethod
    def _subtask_done_event(result: SubtaskResult) -> dict[str, Any]:
        status = "error" if result.error else "done"
        payload: dict[str, Any] = {
            "type": "subtask_done",
            "id": result.subtask.id,
            "title": result.subtask.title,
            "route": result.route,
            "model": result.model,
            "status": status,
            "confidence": result.confidence,
            "error": result.error,
            "duration_seconds": result.duration_seconds,
            "preview": Relay._preview_text(result.content) if result.content and not result.error else "",
            "usage": usage_to_dict(result.usage),
        }
        return payload

    @staticmethod
    def _emit_subtask_done(result: SubtaskResult, emit: Callable[[dict[str, Any]], None]) -> None:
        event = Relay._subtask_done_event(result)
        emit(event)
        emit(
            {
                "type": "routed",
                "id": event["id"],
                "title": event["title"],
                "route": event["route"],
                "model": event["model"],
                "confidence": event["confidence"],
                "error": event["error"],
                "duration_seconds": event["duration_seconds"],
                "preview": event["preview"],
                "usage": event["usage"],
            }
        )

    @staticmethod
    def _usage_call(label: str, result: CompletionResult, *, route: str, model: str | None) -> dict[str, Any]:
        usage = usage_to_dict(result.usage) or {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }
        return {
            "label": label,
            "route": route,
            "model": model,
            **usage,
        }

    @staticmethod
    def _stream_provider(
        provider: ChatProvider,
        messages: list[Message],
        on_token: Callable[[str], None],
        *,
        temperature: float = 0.2,
    ) -> CompletionResult:
        """Stream from a provider, falling back to a blocking call if it can't stream."""
        streamer = getattr(provider, "stream", None)
        if callable(streamer):
            return streamer(messages, temperature=temperature, on_token=on_token)
        result = provider.complete(messages, temperature=temperature)
        if result.text:
            on_token(result.text)
        return result

    def _run_single(
        self,
        user_prompt: str,
        plan: Plan,
        emit: Callable[[dict[str, Any]], None],
    ) -> RouterTrace:
        subtask = plan.subtasks[0]
        decision = self.policy.decide_for_subtask(
            subtask,
            plan_requires_online=plan.requires_online,
            local_supports_vision=self._local_supports_vision(),
        )
        provider, model, route = self._resolve_execution_target(subtask, decision)
        use_cloud = route == "cloud"
        cloud_note = f" Cloud model: {model}." if use_cloud else ""
        if subtask.model_override:
            cloud_note = f" Model override: {model}."
        usage_calls: list[dict[str, Any]] = []
        started = time.monotonic()

        emit({"type": "subtask_start", "id": subtask.id, "title": subtask.title})
        emit({"type": "answering"})

        messages = subtask_messages(
            user_prompt,
            subtask,
            [],
            images=self._images_for_subtask(subtask),
            conversation_history=self._conversation_block(),
        )
        on_token = lambda chunk: emit({"type": "token", "text": chunk})  # noqa: E731
        error: str | None = None
        usage: TokenUsage | None = None
        try:
            stream_result = self._stream_provider(provider, messages, on_token)
            content = stream_result.text
            usage = stream_result.usage
            usage_calls.append(self._usage_call("subtask", stream_result, route=route, model=model))
        except ProviderError as exc:
            if use_cloud and not self.policy.is_difficult(subtask) and not subtask.model_override:
                decision = RouteDecision("local", f"Cloud model {model} failed ({exc}); fell back to local.", decision.cloud_allowed)
                route, model, cloud_note = "local", getattr(self.local, "model", None), ""
                try:
                    stream_result = self._stream_provider(self.local, messages, on_token)
                    content = stream_result.text
                    usage = stream_result.usage
                    usage_calls.append(self._usage_call("subtask", stream_result, route=route, model=model))
                except Exception as local_exc:  # noqa: BLE001
                    content, error = "", str(local_exc)
            else:
                content, error = "", str(exc)

        duration = round(time.monotonic() - started, 3)
        result = SubtaskResult(
            subtask,
            route,
            content,
            None,
            decision.reason + cloud_note,
            error=error,
            model=model,
            usage=usage,
            duration_seconds=duration,
        )
        self._emit_subtask_done(result, emit)
        final_answer = content if not error else f"Subtask failed: {error}"
        usage_summary = aggregate_run_usage(usage_calls) if usage_calls else None
        return RouterTrace(
            user_prompt=user_prompt,
            plan=plan,
            route_decisions={subtask.id: decision},
            results=[result],
            final_answer=final_answer,
            metadata=self._metadata(usage_summary=usage_summary),
        )

    def _synthesize_stream(
        self,
        user_prompt: str,
        plan: Plan,
        results: list[SubtaskResult],
        on_token: Callable[[str], None],
    ) -> tuple[str, CompletionResult | None]:
        messages = synthesis_messages(
            user_prompt,
            plan.final_response_instructions,
            results,
            conversation_history=self._conversation_block(),
        )
        try:
            stream_result = self._stream_provider(self.local, messages, on_token)
            return stream_result.text, stream_result
        except ProviderError as exc:
            parts = [f"Local synthesis failed: {exc}", "", "Subtask results:"]
            for result in results:
                parts.append(f"\n## {result.subtask.title} ({result.route})\n{result.content}")
            text = "\n".join(parts)
            on_token(text)
            return text, None

    def plan(self, user_prompt: str, *, attachments: list[Attachment] | None = None, history: list[Message] | None = None) -> Plan:
        attachments = attachments or []
        history = history or []
        prompt = prompt_with_conversation(planning_prompt(user_prompt, attachments), history)
        hint = attachment_plan_hint(attachments)
        try:
            planner_result = self.local.complete(
                planner_messages(prompt, self.max_subtasks, attachment_hint=hint),
                temperature=0.1,
            )
            data = extract_json_object(planner_result.text)
            plan = _plan_from_dict(data, max_subtasks=self.max_subtasks, fallback_prompt=prompt)
            return _expand_monolithic_plan(plan, prompt, max_subtasks=self.max_subtasks, attachments=attachments)
        except Exception as exc:
            # If planning fails, keep the whole task local instead of losing the request.
            return Plan(
                summary=f"Fallback single-step plan because planning failed: {exc}",
                requires_online=False,
                subtasks=[
                    Subtask(
                        id="task_1",
                        title="Answer the user request",
                        prompt=user_prompt,
                        preferred_route="auto",
                        capabilities=_guess_capabilities(user_prompt),
                        sensitivity="medium",
                        rationale="Fallback plan.",
                    )
                ],
            )

    def _execute_subtask(
        self,
        user_prompt: str,
        subtask: Subtask,
        prior_results: list[SubtaskResult],
        plan_requires_online: bool = False,
    ) -> tuple[SubtaskResult, RouteDecision, list[dict[str, Any]]]:
        started = time.monotonic()
        usage_calls: list[dict[str, Any]] = []
        decision = self.policy.decide_for_subtask(
            subtask,
            plan_requires_online=plan_requires_online,
            local_supports_vision=self._local_supports_vision(),
        )
        provider, model, route = self._resolve_execution_target(subtask, decision)
        local_model = getattr(self.local, "model", None)
        use_cloud = route == "cloud"
        cloud_provider, cloud_model = self._resolve_cloud(subtask)
        cloud_note = f" Cloud model: {model}." if use_cloud else ""
        if subtask.model_override:
            cloud_note = f" Model override: {model}."

        # The evaluator is a *second* local LLM call whose only purpose is deciding
        # whether to escalate a local answer to cloud. When that escalation is
        # impossible (cloud off, or no cloud provider) it is pure latency, so skip
        # it. This is the main reason simple local-only requests felt slow.
        can_escalate = (not use_cloud) and cloud_provider is not None and self.policy.cloud_enabled
        usage: TokenUsage | None = None

        try:
            worker_result = provider.complete(
                subtask_messages(
                    user_prompt,
                    subtask,
                    prior_results,
                    images=self._images_for_subtask(subtask),
                    conversation_history=self._conversation_block(),
                ),
                temperature=0.2,
            )
            content = worker_result.text
            usage = worker_result.usage
            usage_calls.append(self._usage_call("subtask", worker_result, route=route, model=model))
        except ProviderError as exc:
            if use_cloud and not self.policy.is_difficult(subtask) and not subtask.model_override:
                # Cloud failure on simple work can fall back local; difficult tasks stay off local.
                fallback_decision = RouteDecision("local", f"Cloud model {model} failed ({exc}); fell back to local.", decision.cloud_allowed)
                try:
                    worker_result = self.local.complete(
                        subtask_messages(
                            user_prompt,
                            subtask,
                            prior_results,
                            images=self._images_for_subtask(subtask),
                            conversation_history=self._conversation_block(),
                        ),
                        temperature=0.2,
                    )
                    content = worker_result.text
                    usage = worker_result.usage
                    usage_calls.append(self._usage_call("subtask", worker_result, route="local", model=local_model))
                    duration = round(time.monotonic() - started, 3)
                    return (
                        SubtaskResult(
                            subtask,
                            "local",
                            content,
                            None,
                            fallback_decision.reason,
                            model=local_model,
                            usage=usage,
                            duration_seconds=duration,
                        ),
                        fallback_decision,
                        usage_calls,
                    )
                except Exception as local_exc:
                    duration = round(time.monotonic() - started, 3)
                    return (
                        SubtaskResult(
                            subtask,
                            "local",
                            "",
                            None,
                            fallback_decision.reason,
                            error=str(local_exc),
                            model=local_model,
                            duration_seconds=duration,
                        ),
                        fallback_decision,
                        usage_calls,
                    )
            route_name = route if use_cloud else "local"
            model_name = model if use_cloud else local_model
            duration = round(time.monotonic() - started, 3)
            return (
                SubtaskResult(
                    subtask,
                    route_name,
                    "",
                    None,
                    decision.reason,
                    error=str(exc),
                    model=model_name,
                    duration_seconds=duration,
                ),
                decision,
                usage_calls,
            )

        if can_escalate:
            confidence, eval_reason, needs_cloud_retry, eval_usage = self.evaluate(subtask, content)
            if eval_usage is not None:
                usage = merge_usage(usage, eval_usage)
                usage_calls.append(
                    self._usage_call(
                        "evaluator",
                        CompletionResult(text="", usage=eval_usage),
                        route="local",
                        model=local_model,
                    )
                )
            retry_decision = self.policy.decide_after_local_eval(subtask, confidence, needs_cloud_retry)
            if retry_decision and cloud_provider is not None:
                try:
                    cloud_result = cloud_provider.complete(
                        subtask_messages(
                            user_prompt,
                            subtask,
                            prior_results,
                            images=self._images_for_subtask(subtask),
                            conversation_history=self._conversation_block(),
                        ),
                        temperature=0.2,
                    )
                    retry_model = cloud_model or model
                    usage_calls.append(self._usage_call("subtask_retry", cloud_result, route="cloud", model=retry_model))
                    duration = round(time.monotonic() - started, 3)
                    return (
                        SubtaskResult(
                            subtask,
                            "cloud",
                            cloud_result.text,
                            None,
                            retry_decision.reason + f" Cloud model: {retry_model}.",
                            model=retry_model,
                            usage=merge_usage(usage, cloud_result.usage),
                            duration_seconds=duration,
                        ),
                        retry_decision,
                        usage_calls,
                    )
                except ProviderError as exc:
                    content += f"\n\n[Cloud retry failed: {exc}]"
            duration = round(time.monotonic() - started, 3)
            return (
                SubtaskResult(
                    subtask,
                    route,
                    content,
                    confidence,
                    decision.reason + cloud_note + " " + eval_reason,
                    model=model,
                    usage=usage,
                    duration_seconds=duration,
                ),
                decision,
                usage_calls,
            )

        duration = round(time.monotonic() - started, 3)
        return (
            SubtaskResult(subtask, route, content, None, decision.reason + cloud_note, model=model, usage=usage, duration_seconds=duration),
            decision,
            usage_calls,
        )

    def evaluate(self, subtask: Subtask, answer: str) -> tuple[float, str, bool, TokenUsage | None]:
        try:
            eval_result = self.local.complete(evaluator_messages(subtask, answer), temperature=0.0)
            data = extract_json_object(eval_result.text)
            confidence = clamp_float(data.get("confidence"), default=0.7)
            needs_cloud_retry = bool(data.get("needs_cloud_retry", False))
            reason = str(data.get("reason", "Local evaluator completed."))
            return confidence, reason, needs_cloud_retry, eval_result.usage
        except Exception as exc:
            # Avoid overusing cloud just because evaluator JSON failed.
            return 0.7, f"Evaluator fallback: {exc}", False, None

    def synthesize(self, user_prompt: str, plan: Plan, results: list[SubtaskResult]) -> str:
        try:
            return self.local.complete(
                synthesis_messages(
                    user_prompt,
                    plan.final_response_instructions,
                    results,
                    conversation_history=self._conversation_block(),
                ),
                temperature=0.2,
            ).text
        except ProviderError as exc:
            # Plain fallback if the local synthesis model fails after subtask work.
            parts = [f"Local synthesis failed: {exc}", "", "Subtask results:"]
            for result in results:
                parts.append(f"\n## {result.subtask.title} ({result.route})\n{result.content}")
            return "\n".join(parts)


_FENCE_RE = re.compile(r"```([^\n]*)\n(.*?)```", re.DOTALL)


def _iter_code_blocks(text: str):
    for match in _FENCE_RE.finditer(text or ""):
        yield match.group(1).strip(), match.group(2)


def _code_signature(code: str) -> str:
    """Whitespace-insensitive fingerprint so reformatted copies still match."""
    return re.sub(r"\s+", "", code)


def _ensure_deliverables_included(
    final_answer: str,
    results: list[SubtaskResult],
    *,
    min_chars: int = 200,
) -> str:
    """Append any substantial code block from the subtask results that the synthesis
    dropped. The user only sees the final answer, so a lost file is a lost deliverable.
    A block already reproduced (even reformatted) is detected via its signature and skipped.
    """
    answer_sig = _code_signature(final_answer)
    missing: list[tuple[str, str]] = []
    seen: set[str] = set()
    for result in results:
        if result.error or not result.content:
            continue
        for info, code in _iter_code_blocks(result.content):
            if len(code.strip()) < min_chars:
                continue  # skip small inline examples; only guarantee real files
            sig = _code_signature(code)
            if not sig or sig in seen:
                continue
            seen.add(sig)
            probe = sig[:80]
            if probe and probe in answer_sig:
                continue  # already present in the synthesized answer
            missing.append((info or "text", code.rstrip()))
    if not missing:
        return final_answer
    parts = [final_answer.rstrip(), "", "---", "", "## Full files"]
    for info, code in missing:
        parts.extend(["", f"```{info}", code, "```"])
    return "\n".join(parts)


def _vision_auth_failure(results: list[SubtaskResult]) -> str | None:
    for result in results:
        if Capability.VISION.value not in result.subtask.capabilities:
            continue
        error = (result.error or "").lower()
        if "http 401" in error or "authentication" in error or "missing authentication" in error:
            return (
                "Image analysis failed: cloud authentication error (HTTP 401). "
                "For local vision: run `ollama pull llava`, set Local model to `llava` in Settings, and try again. "
                "For cloud vision: paste a valid OpenRouter API key in Settings and use Test Cloud."
            )
    return None


_ACTION_VERBS = (
    "design",
    "implement",
    "prove",
    "write",
    "build",
    "research",
    "create",
    "summarize",
    "compare",
    "analyze",
    "explain",
    "fetch",
    "deploy",
    "test",
    "evaluate",
    "calculate",
    "optimize",
    "draft",
    "review",
)
_NUMBERED_PART = re.compile(
    r"(?:^|\n)\s*(?:\d+[\).\]:]|[-*•])\s+(.+?)(?=(?:\n\s*(?:\d+[\).\]:]|[-*•])\s+)|\Z)",
    re.DOTALL,
)
_CLAUSE_SPLIT = re.compile(
    rf",?\s+(?:and\s+)?(?=({'|'.join(_ACTION_VERBS)})\b)",
    re.IGNORECASE,
)


def _title_from_part(part: str, index: int) -> str:
    text = " ".join(part.split())
    if len(text) <= 72:
        return text
    return text[:69].rstrip() + "…" if index == 1 else f"Part {index}"


def _guess_capabilities(text: str) -> list[str]:
    lower = text.lower()
    if any(w in lower for w in ("image", "screenshot", "photo", "picture", "diagram", "chart", "ocr")):
        return [Capability.VISION.value, Capability.REASONING.value]
    if any(w in lower for w in ("latest", "today", "current", "pricing", "news", "cite", "sources", "this month", "this week")):
        return ["current_info"]
    if any(w in lower for w in ("python", "code", "implement", "fastapi", "api", "function", "script", "endpoint", "refactor", "debug")):
        if any(w in lower for w in ("prove", "math", "optimize", "calculate", "equation", "idempotent")):
            return ["coding", "math"]
        return ["coding"]
    if any(w in lower for w in ("prove", "math", "optimize", "calculate", "equation", "theorem", "calculus", "algebra")):
        return ["math", "reasoning"]
    if any(w in lower for w in ("design", "architecture", "schema", "architect", "analyze", "analyse", "complex", "algorithm")):
        return ["reasoning"]
    if any(w in lower for w in ("build", "deploy", "engineer", "develop", "verify", "formalize", "formalise")):
        return ["reasoning", "coding"]
    return ["general"]


def _extract_list_parts(prompt: str) -> list[str]:
    matches = [m.group(1).strip() for m in _NUMBERED_PART.finditer(prompt) if m.group(1).strip()]
    return [part for part in matches if len(part) > 12]


def _extract_clause_parts(prompt: str) -> list[str]:
    chunks = [chunk.strip(" ,.") for chunk in _CLAUSE_SPLIT.split(prompt) if chunk.strip(" ,.")]
    chunks = [chunk for chunk in chunks if len(chunk) > 12]
    if len(chunks) >= 2:
        return chunks
    semi = [part.strip() for part in prompt.split(";") if part.strip() and len(part.strip()) > 12]
    return semi if len(semi) >= 2 else []


def _extract_sentence_parts(prompt: str) -> list[str]:
    sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", prompt.strip()) if part.strip()]
    if len(sentences) < 2:
        return []
    actionable = [
        sentence
        for sentence in sentences
        if len(sentence) > 20
        and (
            _CLAUSE_SPLIT.search(sentence)
            or any(sentence.lower().startswith(verb) for verb in _ACTION_VERBS)
        )
    ]
    return actionable if len(actionable) >= 2 else []


def _expand_for_attachments(
    plan: Plan,
    user_prompt: str,
    attachments: list[Attachment],
    *,
    max_subtasks: int,
) -> Plan:
    if not attachments:
        return plan

    if has_images(attachments) and len(plan.subtasks) == 1:
        question = user_prompt.strip()
        subtasks = [
            Subtask(
                id="task_1",
                title="Review attached images",
                prompt=(
                    "Inspect the attached image(s) and extract every detail that matters for the user's request. "
                    "Return a concise structured summary of what you see."
                ),
                preferred_route="auto",
                capabilities=[Capability.VISION.value, Capability.LARGE_CONTEXT.value],
                depends_on=[],
                sensitivity="medium",
                rationale="Image review requires a vision-capable model.",
            ),
            Subtask(
                id="task_2",
                title="Answer the user request",
                prompt=question or "Answer the user's request using the image review.",
                preferred_route="auto",
                capabilities=_guess_capabilities(question),
                depends_on=["task_1"],
                sensitivity="medium",
                rationale="Uses the image review to answer the actual question.",
            ),
        ]
        return Plan(
            summary=plan.summary or "Review attachments, then answer the request",
            requires_online=plan.requires_online,
            final_response_instructions=plan.final_response_instructions,
            subtasks=subtasks[:max_subtasks],
        )

    if len(plan.subtasks) == 1:
        text_files = [item.name for item in attachments if item.kind == "text"]
        if text_files:
            subtasks = [
                Subtask(
                    id="task_1",
                    title="Review attached files",
                    prompt=(
                        "Review the attached file contents included in the original request. "
                        f"Files: {', '.join(text_files)}. Summarize the relevant facts."
                    ),
                    preferred_route="auto",
                    capabilities=[Capability.LARGE_CONTEXT.value, Capability.REASONING.value],
                    depends_on=[],
                    sensitivity="medium",
                    rationale="File review is isolated before answering.",
                ),
                Subtask(
                    id="task_2",
                    title="Answer the user request",
                    prompt=user_prompt.strip() or "Answer the user's request using the file review.",
                    preferred_route="auto",
                    capabilities=_guess_capabilities(user_prompt),
                    depends_on=["task_1"],
                    sensitivity="medium",
                    rationale="Answer builds on the file review.",
                ),
            ]
            return Plan(
                summary=plan.summary or "Review files, then answer the request",
                requires_online=plan.requires_online,
                final_response_instructions=plan.final_response_instructions,
                subtasks=subtasks[:max_subtasks],
            )

    return _tag_vision_subtasks(plan, attachments)


def _tag_vision_subtasks(plan: Plan, attachments: list[Attachment]) -> Plan:
    if not has_images(attachments):
        return plan
    updated: list[Subtask] = []
    for subtask in plan.subtasks:
        caps = list(subtask.capabilities)
        if Capability.VISION.value not in caps:
            caps.append(Capability.VISION.value)
        updated.append(
            Subtask(
                id=subtask.id,
                title=subtask.title,
                prompt=subtask.prompt,
                preferred_route=subtask.preferred_route,
                capabilities=caps,
                depends_on=list(subtask.depends_on),
                sensitivity=subtask.sensitivity,
                rationale=subtask.rationale,
                model_override=subtask.model_override,
            )
        )
    return Plan(
        summary=plan.summary,
        requires_online=plan.requires_online,
        final_response_instructions=plan.final_response_instructions,
        subtasks=updated,
    )


def _expand_monolithic_plan(
    plan: Plan,
    user_prompt: str,
    *,
    max_subtasks: int,
    attachments: list[Attachment] | None = None,
) -> Plan:
    """If the planner returned one mega-task, split compound prompts into multiple subtasks."""
    attachments = attachments or []
    if attachments:
        plan = _expand_for_attachments(plan, user_prompt, attachments, max_subtasks=max_subtasks)
        if len(plan.subtasks) > 1:
            return plan

    if len(plan.subtasks) != 1:
        return _tag_vision_subtasks(plan, attachments)

    parts = _extract_list_parts(user_prompt)
    if len(parts) < 2:
        parts = _extract_clause_parts(user_prompt)
    if len(parts) < 2:
        parts = _extract_sentence_parts(user_prompt)
    if len(parts) < 2:
        return _tag_vision_subtasks(plan, attachments)

    subtasks: list[Subtask] = []
    for index, part in enumerate(parts[:max_subtasks], start=1):
        subtasks.append(
            Subtask(
                id=f"task_{index}",
                title=_title_from_part(part, index),
                prompt=part,
                preferred_route="auto",
                capabilities=_guess_capabilities(part),
                depends_on=[],
                sensitivity="medium",
                rationale="Auto-split from a compound user request.",
            )
        )

    return _tag_vision_subtasks(
        Plan(
            summary=plan.summary,
            requires_online=plan.requires_online,
            final_response_instructions=plan.final_response_instructions,
            subtasks=subtasks,
        ),
        attachments,
    )


def _plan_from_dict(data: dict[str, Any], *, max_subtasks: int, fallback_prompt: str) -> Plan:
    raw_subtasks = data.get("subtasks")
    if not isinstance(raw_subtasks, list) or not raw_subtasks:
        raw_subtasks = [{"id": "task_1", "title": "Answer the request", "prompt": fallback_prompt}]

    subtasks: list[Subtask] = []
    for index, raw in enumerate(raw_subtasks[:max_subtasks], start=1):
        if not isinstance(raw, dict):
            continue
        task_id = str(raw.get("id") or f"task_{index}")
        # Relay always auto-selects route and model from capabilities; ignore planner overrides.
        preferred = "auto"
        sensitivity = str(raw.get("sensitivity") or "medium")
        if sensitivity not in {"low", "medium", "high"}:
            sensitivity = "medium"
        capabilities = raw.get("capabilities")
        if not isinstance(capabilities, list) or not all(isinstance(c, str) for c in capabilities):
            capabilities = ["general"]
        title = str(raw.get("title") or f"Task {index}")
        prompt = str(raw.get("prompt") or fallback_prompt)
        if set(capabilities) <= {"general", "creative"}:
            guessed = _guess_capabilities(f"{title}\n{prompt}")
            if set(guessed) - {"general", "creative"}:
                capabilities = guessed
        depends_on = raw.get("depends_on")
        if not isinstance(depends_on, list) or not all(isinstance(d, str) for d in depends_on):
            depends_on = []
        model_override = raw.get("model_override")
        if model_override is not None:
            model_override = str(model_override).strip() or None
        subtasks.append(
            Subtask(
                id=task_id,
                title=title,
                prompt=prompt,
                preferred_route=preferred,  # type: ignore[arg-type]
                capabilities=capabilities,
                depends_on=depends_on,
                sensitivity=sensitivity,  # type: ignore[arg-type]
                rationale=str(raw.get("rationale") or ""),
                model_override=model_override,
            )
        )

    if not subtasks:
        subtasks = [Subtask(id="task_1", title="Answer the request", prompt=fallback_prompt)]

    return Plan(
        summary=str(data.get("summary") or "Local-first plan"),
        requires_online=bool(data.get("requires_online", False)),
        final_response_instructions=str(
            data.get("final_response_instructions")
            or "Assemble the subtask results into one complete, self-contained answer. "
            "Include every file and code snippet in full and verbatim; never refer the user "
            "to the subtask results or abbreviate code."
        ),
        subtasks=subtasks,
    )


def plan_to_dict(plan: Plan) -> dict[str, Any]:
    return asdict(plan)


def plan_from_dict(data: dict[str, Any], *, max_subtasks: int, user_prompt: str) -> Plan:
    return _plan_from_dict(data, max_subtasks=max_subtasks, fallback_prompt=user_prompt)


def trace_to_dict(trace: RouterTrace) -> dict[str, Any]:
    return asdict(trace)


def trace_to_json(trace: RouterTrace) -> str:
    return json.dumps(trace_to_dict(trace), indent=2, ensure_ascii=False)
