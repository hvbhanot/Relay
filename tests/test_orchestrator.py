import json
import threading
import time
from dataclasses import replace

from relay.config import RouterConfig
from relay.orchestrator import Relay, _expand_monolithic_plan, plan_from_dict, plan_to_dict
from relay.policy import RoutingPolicy
from relay.providers import CompletionResult, OpenAICompatibleProvider, ProviderError
from relay.schema import Message, Plan, Subtask


class FakeProvider:
    def __init__(self, name="fake", answers=None):
        self.name = name
        self.answers = answers or []
        self.calls = []

    def complete(self, messages: list[Message], *, temperature: float = 0.2) -> CompletionResult:
        self.calls.append(messages)
        if self.answers:
            answer = self.answers.pop(0)
            if callable(answer):
                answer = answer(messages)
            return CompletionResult(text=answer)
        text = "\n".join(m.content for m in messages)
        if "Return ONLY a JSON object" in messages[0].content:
            return CompletionResult(text=json.dumps(
                {
                    "summary": "single task",
                    "requires_online": False,
                    "final_response_instructions": "finalize",
                    "subtasks": [
                        {
                            "id": "task_1",
                            "title": "Do it",
                            "prompt": "Do it locally",
                            "preferred_route": "auto",
                            "capabilities": ["general"],
                            "depends_on": [],
                            "sensitivity": "low",
                        }
                    ],
                }
            ))
        if "confidence evaluator" in messages[0].content:
            return CompletionResult(text=json.dumps({"confidence": 0.9, "needs_cloud_retry": False, "reason": "ok"}))
        if "synthesis model" in messages[0].content:
            return CompletionResult(text="final answer")
        return CompletionResult(text="subtask answer")


def test_router_runs_local_first() -> None:
    local = FakeProvider("local")
    cloud = FakeProvider("cloud")
    router = Relay(local, cloud, RoutingPolicy(cloud_enabled=True), max_subtasks=3)
    trace = router.run("hello")
    # A single successful subtask returns its answer directly (no synthesis call).
    assert trace.final_answer == "subtask answer"
    assert trace.results[0].route == "local"
    assert len(cloud.calls) == 0


def test_local_only_skips_evaluator_and_synthesis() -> None:
    # With cloud disabled, a simple single-subtask request must cost exactly two
    # local calls (planner + worker) — no evaluator, no synthesis round-trips.
    local = FakeProvider("local")
    router = Relay(local, None, RoutingPolicy(cloud_enabled=False), max_subtasks=3)
    trace = router.run("hello")
    assert len(local.calls) == 2
    assert trace.results[0].route == "local"
    assert trace.results[0].confidence is None
    assert trace.final_answer == "subtask answer"


def test_router_uses_cloud_for_cloud_capability() -> None:
    local = FakeProvider(
        "local",
        answers=[
            json.dumps(
                {
                    "summary": "cloud task",
                    "subtasks": [
                        {
                            "id": "task_1",
                            "title": "Latest info",
                            "prompt": "Find latest info",
                            "preferred_route": "auto",
                            "capabilities": ["current_info"],
                            "sensitivity": "low",
                        }
                    ],
                }
            ),
            json.dumps({"confidence": 0.9, "needs_cloud_retry": False, "reason": "ok"}),
            "final answer",
        ],
    )
    cloud = FakeProvider("cloud", answers=["cloud result"])
    router = Relay(local, cloud, RoutingPolicy(cloud_enabled=True), max_subtasks=3)
    trace = router.run("latest?")
    assert trace.results[0].route == "cloud"
    assert trace.results[0].content == "cloud result"


def test_router_builds_openai_compatible_provider() -> None:
    config = replace(
        RouterConfig.from_env(load_dotenv=False),
        cloud_enabled=True,
        cloud_provider="openai-compatible",
        openai_compat_api_key="sk-test",
        openai_compat_base_url="https://api.openai.com",
        openai_compat_model="gpt-5.5",
    )
    router = Relay.from_config(config)
    assert router.cloud is not None
    assert router.cloud.name == "openai-compatible:gpt-5.5"
    assert router.cloud.chat_completions_url() == "https://api.openai.com/v1/chat/completions"


def test_router_divides_subtasks_across_cloud_models(monkeypatch) -> None:
    import relay.cloud_pool as cloud_pool

    # One fake provider per model, so we can see which model handled each subtask.
    built: dict[str, FakeProvider] = {}

    def fake_build(config, model):
        provider = FakeProvider(f"openrouter:{model}", answers=[f"answer from {model}"])
        provider.model = model
        built[model] = provider
        return provider

    monkeypatch.setattr(cloud_pool, "build_cloud_provider_for_model", fake_build)

    plan = json.dumps(
        {
            "summary": "multi",
            "subtasks": [
                {"id": "t1", "title": "Code", "prompt": "x", "preferred_route": "cloud", "capabilities": ["coding"], "sensitivity": "low"},
                {"id": "t2", "title": "News", "prompt": "x", "preferred_route": "cloud", "capabilities": ["current_info"], "sensitivity": "low"},
                {"id": "t3", "title": "Chat", "prompt": "x", "preferred_route": "auto", "capabilities": ["general"], "sensitivity": "low"},
            ],
        }
    )
    local = FakeProvider("local")
    # planner -> plan, then evaluator JSON for each of 3 subtasks, then synthesis.
    local.answers = [plan] + [json.dumps({"confidence": 0.9, "needs_cloud_retry": False, "reason": "ok"})] * 3 + ["final"]

    config = replace(
        RouterConfig.from_env(load_dotenv=False),
        cloud_enabled=True,
        cloud_provider="openrouter",
        openrouter_api_key="sk-test",
        openrouter_model="anthropic/claude-sonnet-4.6",
        cloud_model_map={"coding": "anthropic/claude-opus-4.8", "current_info": "openai/gpt-5.5"},
        max_subtasks=6,
    )
    router = Relay.from_config(config)
    router.local = local

    trace = router.run("do several things")
    by_id = {r.subtask.id: r for r in trace.results}
    assert by_id["t1"].route == "cloud" and by_id["t1"].model == "anthropic/claude-opus-4.8"
    assert by_id["t2"].route == "cloud" and by_id["t2"].model == "openai/gpt-5.5"
    assert by_id["t3"].route == "local"
    # Specialist subtasks pick mapped cloud models; general work stays local.
    assert {r.model for r in trace.results if r.route == "cloud"} == {
        "anthropic/claude-opus-4.8",
        "openai/gpt-5.5",
    }


def test_expand_monolithic_plan_splits_compound_prompt() -> None:
    prompt = (
        "Design a REST API for a todo app, implement the core endpoints in Python with FastAPI, "
        "and prove with a small example why PUT is idempotent but POST is not."
    )
    plan = Plan(
        summary="single blob",
        subtasks=[Subtask(id="task_1", title="Do everything", prompt=prompt, capabilities=["general"])],
    )
    expanded = _expand_monolithic_plan(plan, prompt, max_subtasks=6)
    assert len(expanded.subtasks) == 3
    assert "FastAPI" in expanded.subtasks[1].prompt
    assert all(not subtask.depends_on for subtask in expanded.subtasks)


def test_expand_monolithic_plan_keeps_simple_prompt() -> None:
    prompt = "What is a for-loop?"
    plan = Plan(
        summary="simple",
        subtasks=[Subtask(id="task_1", title="Answer", prompt=prompt, capabilities=["general"])],
    )
    expanded = _expand_monolithic_plan(plan, prompt, max_subtasks=6)
    assert len(expanded.subtasks) == 1


def test_execute_independent_subtasks_in_parallel() -> None:
    class TrackingProvider(FakeProvider):
        def __init__(self) -> None:
            super().__init__("local")
            self._lock = threading.Lock()
            self._in_flight = 0
            self.peak_in_flight = 0

        def complete(self, messages: list[Message], *, temperature: float = 0.2) -> CompletionResult:
            with self._lock:
                self._in_flight += 1
                self.peak_in_flight = max(self.peak_in_flight, self._in_flight)
            time.sleep(0.05)
            try:
                return super().complete(messages, temperature=temperature)
            finally:
                with self._lock:
                    self._in_flight -= 1

    local = TrackingProvider()
    router = Relay(local, None, RoutingPolicy(cloud_enabled=False), max_subtasks=6)
    plan = Plan(
        summary="parallel",
        subtasks=[
            Subtask(id="t1", title="A", prompt="say a", capabilities=["general"]),
            Subtask(id="t2", title="B", prompt="say b", capabilities=["general"]),
            Subtask(id="t3", title="C", prompt="say c", capabilities=["general"]),
        ],
    )
    router.execute("ignored", plan)
    assert local.peak_in_flight >= 2


def test_preview_routes_predicts_local_and_cloud() -> None:
    local = FakeProvider("local")
    cloud = FakeProvider("cloud")
    router = Relay(local, cloud, RoutingPolicy(cloud_enabled=True), max_subtasks=3)
    plan = Plan(
        summary="mixed",
        requires_online=False,
        subtasks=[
            Subtask(id="t1", title="Local task", prompt="summarize", preferred_route="auto", capabilities=["general"]),
            Subtask(id="t2", title="News", prompt="latest pricing", preferred_route="auto", capabilities=["current_info"]),
        ],
    )
    previews = router.preview_routes(plan)
    assert previews[0]["predicted_route"] == "local"
    assert previews[1]["predicted_route"] == "cloud"


def test_model_override_uses_cloud_model() -> None:
    local = FakeProvider("local")
    cloud = FakeProvider("cloud")
    router = Relay(local, cloud, RoutingPolicy(cloud_enabled=True), max_subtasks=3)
    plan = Plan(
        summary="override",
        requires_online=False,
        subtasks=[
            Subtask(
                id="t1",
                title="General",
                prompt="say hi",
                preferred_route="auto",
                capabilities=["general"],
                model_override="anthropic/claude-opus-4.8",
            ),
        ],
    )
    previews = router.preview_routes(plan)
    assert previews[0]["predicted_route"] == "cloud"
    assert previews[0]["predicted_model"] == "anthropic/claude-opus-4.8"


def test_preview_routes_auto_selects_cloud_for_coding() -> None:
    local = FakeProvider("local")
    cloud = FakeProvider("cloud")
    router = Relay(local, cloud, RoutingPolicy(cloud_enabled=True, privacy_mode="balanced"), max_subtasks=3)
    plan = Plan(
        summary="code",
        requires_online=False,
        subtasks=[
            Subtask(id="t1", title="Build API", prompt="implement endpoints", preferred_route="auto", capabilities=["coding"]),
        ],
    )
    previews = router.preview_routes(plan)
    assert previews[0]["predicted_route"] == "cloud"
    assert "coding" in previews[0]["reason"]


def test_execute_with_provided_plan_skips_replanning() -> None:
    local = FakeProvider("local", answers=["provided answer"])
    cloud = FakeProvider("cloud")
    router = Relay(local, cloud, RoutingPolicy(cloud_enabled=False), max_subtasks=3)
    plan = Plan(
        summary="single",
        requires_online=False,
        subtasks=[Subtask(id="task_1", title="Answer", prompt="Say hi", preferred_route="local", capabilities=["general"])],
    )
    events: list[str] = []
    trace = router.run("ignored prompt", on_event=lambda e: events.append(e["type"]), plan=plan)
    assert "planning" not in events
    assert "planned" in events
    assert trace.final_answer == "provided answer"
    roundtrip = plan_from_dict(plan_to_dict(plan), max_subtasks=3, user_prompt="Say hi")
    assert roundtrip.subtasks[0].title == "Answer"


def test_invalid_cloud_api_key_fails_before_planning() -> None:
    local = FakeProvider("local")
    cloud = OpenAICompatibleProvider(
        base_url="https://openrouter.ai/api/v1",
        api_key="make me a website, thanks",
        model="anthropic/claude-sonnet-4.6",
        name="openrouter:test",
    )
    router = Relay(local, cloud, RoutingPolicy(cloud_enabled=True), max_subtasks=3)
    try:
        router.run("hello")
        raise AssertionError("expected ProviderError")
    except ProviderError as exc:
        assert "invalid" in str(exc).lower() or "sk-or-v1" in str(exc)
