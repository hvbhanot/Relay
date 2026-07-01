"""Tests for route forcing, web search injection, and the feedback loop."""

from pathlib import Path

from relay.chat_history import append_message, create_session, get_session, pop_last_message, rename_session
from relay.config import RouterConfig
from relay.feedback import bias_note, confidence_bias, record_feedback
from relay.openai_api import route_mode_from_model
from relay.orchestrator import Relay, force_plan_route
from relay.policy import RoutingPolicy
from relay.providers import CompletionResult
from relay.redaction import RedactionState, redact_messages, redact_text, restore_text
from relay.schema import Message, Plan, Subtask
from relay.ui_config import apply_ui_overrides
from relay.web_search import _parse_results, search_results_block


class FakeProvider:
    def __init__(self, name: str = "fake", text: str = "answer"):
        self.name = name
        self.text = text
        self.prompts: list[str] = []

    def complete(self, messages: list[Message], *, temperature: float = 0.2) -> CompletionResult:
        self.prompts.append("\n".join(m.content for m in messages))
        return CompletionResult(text=self.text)


def _plan(**kwargs) -> Plan:
    defaults = dict(
        summary="test",
        subtasks=[
            Subtask(id="task_1", title="One", prompt="do one thing", capabilities=["general"]),
            Subtask(id="task_2", title="Two", prompt="do another", capabilities=["coding"]),
        ],
    )
    defaults.update(kwargs)
    return Plan(**defaults)


def test_force_plan_route_pins_every_subtask() -> None:
    plan = _plan()
    forced = force_plan_route(plan, "local")
    assert all(s.preferred_route == "local" for s in forced.subtasks)
    # Original plan untouched; invalid/absent modes are no-ops.
    assert all(s.preferred_route == "auto" for s in plan.subtasks)
    assert force_plan_route(plan, None) is plan
    assert force_plan_route(plan, "bogus") is plan


def test_run_with_local_override_never_uses_cloud() -> None:
    local = FakeProvider("local")
    cloud = FakeProvider("cloud")
    router = Relay(local, cloud, RoutingPolicy(cloud_enabled=True), max_subtasks=3)
    trace = router.run("x", plan=_plan(), route_override="local")
    assert all(result.route == "local" for result in trace.results)
    assert not cloud.prompts


def test_run_with_cloud_override_routes_general_work_to_cloud() -> None:
    local = FakeProvider("local")
    cloud = FakeProvider("cloud")
    router = Relay(local, cloud, RoutingPolicy(cloud_enabled=True), max_subtasks=3)
    plan = Plan(summary="s", subtasks=[Subtask(id="t1", title="t", prompt="simple question, no keywords")])
    trace = router.run("x", plan=plan, route_override="cloud")
    assert trace.results[0].route == "cloud"


def test_web_search_context_injected_and_sources_recorded() -> None:
    local = FakeProvider("local")
    calls: list[str] = []

    def fake_search(query: str) -> list[dict[str, str]]:
        calls.append(query)
        return [{"title": "Result", "url": "https://example.com/a", "content": "fresh facts"}]

    router = Relay(local, None, RoutingPolicy(cloud_enabled=False), max_subtasks=3, search=fake_search)
    plan = Plan(
        summary="s",
        subtasks=[Subtask(id="t1", title="News", prompt="what happened today", capabilities=["current_info"])],
    )
    trace = router.run("what happened today", plan=plan)
    assert calls, "search should run for current_info subtasks"
    assert trace.results[0].sources == [{"title": "Result", "url": "https://example.com/a"}]
    assert any("fresh facts" in prompt for prompt in local.prompts)


def test_web_search_runs_on_cloud_routed_subtasks() -> None:
    local = FakeProvider("local")
    cloud = FakeProvider("cloud")
    calls: list[str] = []

    def fake_search(query: str) -> list[dict[str, str]]:
        calls.append(query)
        return [{"title": "R", "url": "https://n.ws", "content": "FRESHFACT"}]

    router = Relay(local, cloud, RoutingPolicy(cloud_enabled=True), max_subtasks=3, search=fake_search)
    plan = Plan(
        summary="s",
        subtasks=[Subtask(id="t1", title="News", prompt="latest news today", capabilities=["current_info"])],
    )
    for override in (None, "cloud"):
        calls.clear()
        cloud.prompts.clear()
        trace = router.run("q", plan=plan, route_override=override)
        assert trace.results[0].route == "cloud"
        assert calls, f"search should run with override={override}"
        assert trace.results[0].sources
        assert any("FRESHFACT" in prompt for prompt in cloud.prompts)


def test_web_search_triggers_on_current_info_keywords_without_tag() -> None:
    # The planner sometimes routes to cloud via keyword match without tagging
    # current_info; search must fire on the same signal.
    local = FakeProvider("local")
    calls: list[str] = []

    def fake_search(query: str) -> list[dict[str, str]]:
        calls.append(query)
        return [{"title": "R", "url": "https://n.ws", "content": "fresh"}]

    router = Relay(local, None, RoutingPolicy(cloud_enabled=False), max_subtasks=3, search=fake_search)
    plan = Plan(
        summary="s",
        subtasks=[Subtask(id="t1", title="GPU costs", prompt="research the latest GPU pricing", capabilities=["reasoning"])],
    )
    trace = router.run("q", plan=plan)
    assert calls, "keyword match should trigger search even without the capability tag"
    assert trace.results[0].sources


def test_web_search_skipped_without_matching_capability_and_on_failure() -> None:
    local = FakeProvider("local")

    def boom(query: str) -> list[dict[str, str]]:
        raise RuntimeError("no network")

    router = Relay(local, None, RoutingPolicy(cloud_enabled=False), max_subtasks=3, search=boom)
    plan = Plan(summary="s", subtasks=[Subtask(id="t1", title="Plain", prompt="hi", capabilities=["general"])])
    trace = router.run("hi", plan=plan)
    assert trace.results[0].sources is None

    news = Plan(summary="s", subtasks=[Subtask(id="t1", title="News", prompt="today", capabilities=["current_info"])])
    trace = router.run("today", plan=news)  # search raises; run must still succeed
    assert trace.results[0].error is None
    assert trace.results[0].sources is None


def test_search_results_block_and_parse() -> None:
    parsed = _parse_results({"results": [{"title": "T", "url": "https://x.dev", "content": "  a  b  "}, {"bad": 1}]})
    assert parsed == [{"title": "T", "url": "https://x.dev", "content": "a b"}]
    block = search_results_block(parsed)
    assert "https://x.dev" in block and "a b" in block
    assert search_results_block([]) == ""


def test_feedback_bias_moves_threshold_and_is_clamped(tmp_path: Path) -> None:
    path = tmp_path / "fb.json"
    assert confidence_bias(path) == 0.0
    record_feedback(score=-1, all_local=True, path=path)
    record_feedback(score=-1, all_local=True, path=path)
    record_feedback(score=1, all_local=True, path=path)
    # 0.03 + 0.03 - 0.02
    assert abs(confidence_bias(path) - 0.04) < 1e-9
    record_feedback(score=1, all_local=False, path=path)  # cloud votes don't shift bias
    assert abs(confidence_bias(path) - 0.04) < 1e-9
    for _ in range(20):
        record_feedback(score=-1, all_local=True, path=path)
    assert confidence_bias(path) == 0.15
    assert "sooner" in bias_note(0.15)


def test_from_config_applies_confidence_bias(monkeypatch) -> None:
    monkeypatch.delenv("RELAY_MIN_LOCAL_CONFIDENCE", raising=False)
    config = RouterConfig.from_env(load_dotenv=False)
    router = Relay.from_config(config, confidence_bias=0.1)
    assert abs(router.policy.min_local_confidence - (config.min_local_confidence + 0.1)) < 1e-9


def test_redact_and_restore_round_trip() -> None:
    state = RedactionState()
    text = "mail bob@x.com key sk-or-v1-abcdefghijklmnop1234 password = hunter2secret keep os.environ"
    redacted = redact_text(text, state)
    assert "bob@x.com" not in redacted
    assert "sk-or-v1" not in redacted
    assert "hunter2secret" not in redacted
    assert "os.environ" in text  # code-shaped values are left alone
    assert restore_text(redacted, state) == text
    # Same secret twice gets the same placeholder.
    state2 = RedactionState()
    twice = redact_text("a@b.co and again a@b.co", state2)
    assert twice.count("[REDACTED_EMAIL_1]") == 2


def test_cloud_calls_are_redacted_and_answers_restored() -> None:
    local = FakeProvider("local")

    class EchoCloud(FakeProvider):
        def complete(self, messages, *, temperature: float = 0.2) -> CompletionResult:
            self.prompts.append("\n".join(m.content for m in messages))
            # Echo the placeholder back, as a cloud model would.
            import re
            found = re.search(r"\[REDACTED_\w+_\d+\]", self.prompts[-1])
            return CompletionResult(text=f"use {found.group(0) if found else 'nothing'}")

    cloud = EchoCloud("cloud")
    router = Relay(local, cloud, RoutingPolicy(cloud_enabled=True), max_subtasks=3, redact_cloud=True)
    plan = Plan(
        summary="s",
        subtasks=[Subtask(id="t1", title="Code", prompt="refactor this, key is sk-or-v1-abcdefghijklmnop1234", capabilities=["coding"])],
    )
    trace = router.run("q", plan=plan)
    assert trace.results[0].route == "cloud"
    assert all("sk-or-v1" not in prompt for prompt in cloud.prompts), "secret must not reach cloud"
    assert "sk-or-v1-abcdefghijklmnop1234" in trace.results[0].content, "answer must be restored"
    assert "Redacted 1 secret" in trace.results[0].reason


def test_no_redaction_for_local_and_message_passthrough() -> None:
    messages = [Message("user", "nothing secret here")]
    same, state = redact_messages(messages)
    assert state is None and same is messages


def test_web_fetch_deepens_top_result() -> None:
    local = FakeProvider("local")

    def fake_search(query):
        return [{"title": "T", "url": "https://x.dev/a", "content": "snippet"}]

    def fake_fetch(url):
        assert url == "https://x.dev/a"
        return "FULL PAGE CONTENT"

    router = Relay(local, None, RoutingPolicy(cloud_enabled=False), max_subtasks=3, search=fake_search, fetch_page=fake_fetch)
    plan = Plan(summary="s", subtasks=[Subtask(id="t1", title="News", prompt="latest news", capabilities=["current_info"])])
    router.run("q", plan=plan)
    assert any("FULL PAGE CONTENT" in prompt for prompt in local.prompts)


def test_route_mode_from_model() -> None:
    assert route_mode_from_model("relay:local") == "local"
    assert route_mode_from_model("Relay:Cloud") == "cloud"
    assert route_mode_from_model("relay") is None
    assert route_mode_from_model("gpt-4:local") is None


def test_history_rename_pop_and_attachments(tmp_path: Path) -> None:
    path = tmp_path / "hist.json"
    session = create_session(title="Old name", path=path)
    sid = session["id"]
    renamed = rename_session(sid, "New name", path=path)
    assert renamed["title"] == "New name"

    message = append_message(
        sid,
        role="user",
        content="hi",
        attachments=[{"name": "pic.png", "mime": "image/png", "kind": "image", "size_bytes": 10, "data": "aGk="}],
        path=path,
    )
    assert message["attachments"][0]["name"] == "pic.png"
    assert message["attachments"][0]["data"] == "aGk="
    append_message(sid, role="assistant", content="hello", path=path)

    # Pop only removes when the role matches.
    assert pop_last_message(sid, role="user", path=path) is None
    removed = pop_last_message(sid, role="assistant", path=path)
    assert removed and removed["content"] == "hello"
    stored = get_session(sid, path)
    assert len(stored["messages"]) == 1


def test_docs_page_asset_exists() -> None:
    from relay.webui import ASSET_DIR

    assert (ASSET_DIR / "docs.html").exists()


def test_web_search_config_round_trip() -> None:
    base = RouterConfig.from_env(load_dotenv=False)
    config = apply_ui_overrides(base, {"web_search_enabled": True, "ollama_api_key": "olm-key-123"})
    assert config.web_search_enabled is True
    assert config.ollama_api_key == "olm-key-123"
    # Blank secret keeps the saved key; explicit clear removes it.
    kept = apply_ui_overrides(config, {"ollama_api_key": ""})
    assert kept.ollama_api_key == "olm-key-123"
    cleared = apply_ui_overrides(config, {"ollama_api_key": "", "ollama_api_key_clear": True})
    assert cleared.ollama_api_key is None
