from relay.policy import RoutingPolicy
from relay.schema import Subtask


def test_default_is_local() -> None:
    policy = RoutingPolicy(cloud_enabled=True)
    decision = policy.decide_for_subtask(Subtask(id="t1", title="General", prompt="Summarize this note"))
    assert decision.route == "local"


def test_current_info_routes_cloud_when_enabled() -> None:
    policy = RoutingPolicy(cloud_enabled=True)
    task = Subtask(id="t1", title="Latest pricing", prompt="Find the latest pricing", sensitivity="low")
    decision = policy.decide_for_subtask(task)
    assert decision.route == "cloud"


def test_current_info_stays_local_when_cloud_disabled() -> None:
    policy = RoutingPolicy(cloud_enabled=False)
    task = Subtask(id="t1", title="Latest pricing", prompt="Find the latest pricing", sensitivity="low")
    decision = policy.decide_for_subtask(task)
    assert decision.route == "local"
    assert decision.cloud_allowed is False


def test_sensitive_stays_local_even_when_cloud_enabled() -> None:
    policy = RoutingPolicy(cloud_enabled=True)
    task = Subtask(id="t1", title="Secret", prompt="Use my API key abc", sensitivity="high")
    decision = policy.decide_for_subtask(task)
    assert decision.route == "local"


def test_plan_requires_online_routes_auto_subtask_to_cloud() -> None:
    policy = RoutingPolicy(cloud_enabled=True)
    task = Subtask(id="t1", title="General", prompt="Summarize this note", sensitivity="low")
    # Without the plan-level flag this is a plain local-first task.
    assert policy.decide_for_subtask(task).route == "local"
    # The planner flagging the whole request as online-needing should escalate it.
    decision = policy.decide_for_subtask(task, plan_requires_online=True)
    assert decision.route == "cloud"


def test_coding_auto_routes_cloud_in_balanced_mode() -> None:
    policy = RoutingPolicy(cloud_enabled=True, privacy_mode="balanced")
    task = Subtask(id="t1", title="Implement", prompt="Write a Python parser", capabilities=["coding"])
    decision = policy.decide_for_subtask(task)
    assert decision.route == "cloud"
    assert "coding" in decision.reason


def test_coding_routes_cloud_even_in_strict_mode() -> None:
    policy = RoutingPolicy(cloud_enabled=True, privacy_mode="strict")
    task = Subtask(id="t1", title="Implement", prompt="Write a Python parser", capabilities=["coding"], sensitivity="low")
    decision = policy.decide_for_subtask(task)
    assert decision.route == "cloud"


def test_difficult_keywords_route_cloud_with_general_capability() -> None:
    policy = RoutingPolicy(cloud_enabled=True)
    task = Subtask(id="t1", title="Task", prompt="Design a distributed cache and prove it is correct", capabilities=["general"])
    decision = policy.decide_for_subtask(task)
    assert decision.route == "cloud"
    assert "difficult" in decision.reason.lower()


def test_difficult_local_answer_always_escalates() -> None:
    policy = RoutingPolicy(cloud_enabled=True)
    task = Subtask(id="t1", title="Implement", prompt="Build a REST API", capabilities=["coding"])
    retry = policy.decide_after_local_eval(task, confidence=0.95, needs_cloud_retry=False)
    assert retry is not None
    assert retry.route == "cloud"


def test_vision_stays_local_when_local_model_supports_images() -> None:
    policy = RoutingPolicy(cloud_enabled=True)
    task = Subtask(
        id="t1",
        title="Review image",
        prompt="Describe the attached image",
        capabilities=["vision"],
        sensitivity="low",
    )
    decision = policy.decide_for_subtask(task, local_supports_vision=True)
    assert decision.route == "local"
    assert "vision" in decision.reason.lower()


def test_vision_routes_cloud_when_local_model_lacks_images() -> None:
    policy = RoutingPolicy(cloud_enabled=True)
    task = Subtask(
        id="t1",
        title="Review image",
        prompt="Describe the attached image",
        capabilities=["vision"],
        sensitivity="low",
    )
    decision = policy.decide_for_subtask(task, local_supports_vision=False)
    assert decision.route == "cloud"
    assert "vision" in decision.reason.lower()


def test_plan_requires_online_respects_privacy_and_cloud_disabled() -> None:
    sensitive = Subtask(id="t1", title="Secret", prompt="Use my API key abc", sensitivity="high")
    assert RoutingPolicy(cloud_enabled=True).decide_for_subtask(
        sensitive, plan_requires_online=True
    ).route == "local"

    task = Subtask(id="t2", title="General", prompt="Summarize this note", sensitivity="low")
    assert RoutingPolicy(cloud_enabled=False).decide_for_subtask(
        task, plan_requires_online=True
    ).route == "local"
