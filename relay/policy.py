from __future__ import annotations

import re
from dataclasses import dataclass

from .schema import Capability, PrivacyMode, RouteDecision, Subtask

CURRENT_INFO_TERMS = re.compile(
    r"\b(latest|today|current|recent|news|price|pricing|schedule|weather|stock|law|regulation|citation|sources?|browse|web|internet|202[5-9])\b",
    re.IGNORECASE,
)

DIFFICULT_TASK_TERMS = re.compile(
    r"\b("
    r"implement|design|architect|refactor|debug|optimize|prove|analyze|analyse|algorithm|"
    r"complex|equation|theorem|proof|derive|formalize|formalise|verify|engineer|develop|"
    r"build|deploy|database schema|api endpoint|unit test|integration test|performance|"
    r"security audit|cryptograph|machine learning|neural network|solve|calculus|linear algebra|"
    r"dynamic programming|concurrency|distributed|microservice|compiler|parser|"
    r"proof of concept|mathematical|optimization"
    r")\b",
    re.IGNORECASE,
)

SENSITIVE_TERMS = re.compile(
    r"\b(password|secret|api[_ -]?key|token|ssn|social security|private key|seed phrase|medical record|bank|credit card)\b",
    re.IGNORECASE,
)

CLOUD_STRONG_CAPABILITIES = {
    Capability.CURRENT_INFO.value,
    Capability.SOURCES.value,
    Capability.LARGE_CONTEXT.value,
    Capability.VISION.value,
}

# Specialist capabilities Relay may auto-route to cloud when preferred_route is "auto".
CLOUD_AUTO_CAPABILITIES = {
    Capability.REASONING.value,
    Capability.MATH.value,
    Capability.CODING.value,
    Capability.HIGH_STAKES.value,
}


@dataclass(frozen=True, slots=True)
class RoutingPolicy:
    cloud_enabled: bool = False
    privacy_mode: PrivacyMode = "balanced"
    min_local_confidence: float = 0.62

    def decide_for_subtask(
        self,
        subtask: Subtask,
        *,
        plan_requires_online: bool = False,
        local_supports_vision: bool = False,
    ) -> RouteDecision:
        if self._must_keep_local(subtask):
            return RouteDecision("local", "Sensitive subtask kept local by privacy policy.", self.cloud_enabled)

        if subtask.preferred_route == "local":
            return RouteDecision("local", "Planner explicitly preferred local execution.", self.cloud_enabled)

        if (
            local_supports_vision
            and Capability.VISION.value in subtask.capabilities
            and subtask.preferred_route != "cloud"
        ):
            return RouteDecision(
                "local",
                "Vision subtask kept local because the configured local model supports images.",
                self.cloud_enabled,
            )

        cloud_reason = self._cloud_reason(subtask)
        if not cloud_reason and plan_requires_online:
            cloud_reason = "Planner marked the overall request as requiring online access."
        if subtask.preferred_route == "cloud" or cloud_reason:
            if self.cloud_enabled:
                return RouteDecision("cloud", cloud_reason or "Planner explicitly preferred cloud execution.", True)
            return RouteDecision(
                "local",
                (cloud_reason or "Planner requested cloud") + " Cloud is disabled, so running locally.",
                False,
            )

        return RouteDecision("local", "Auto-selected local route.", self.cloud_enabled)

    def decide_after_local_eval(self, subtask: Subtask, confidence: float, needs_cloud_retry: bool) -> RouteDecision | None:
        if not self.cloud_enabled:
            return None
        if self._must_keep_local(subtask):
            return None
        if self.is_difficult(subtask):
            return RouteDecision("cloud", "Difficult tasks are not run on the local model.", True)
        if needs_cloud_retry or confidence < self.min_local_confidence:
            return RouteDecision(
                "cloud",
                f"Local confidence {confidence:.2f} below threshold {self.min_local_confidence:.2f} or evaluator requested retry.",
                True,
            )
        return None

    def is_difficult(self, subtask: Subtask) -> bool:
        if set(subtask.capabilities) & CLOUD_AUTO_CAPABILITIES:
            return True
        combined = f"{subtask.title}\n{subtask.prompt}"
        return bool(DIFFICULT_TASK_TERMS.search(combined))

    def _cloud_reason(self, subtask: Subtask) -> str:
        if subtask.preferred_route == "local":
            return ""

        caps = set(subtask.capabilities)
        cloud_caps = sorted(caps & CLOUD_STRONG_CAPABILITIES)
        if cloud_caps:
            return f"Auto-routed to cloud for capabilities: {', '.join(cloud_caps)}."
        combined = f"{subtask.title}\n{subtask.prompt}"
        if CURRENT_INFO_TERMS.search(combined):
            return "Auto-routed to cloud: task needs current or external information."
        auto_caps = sorted(caps & CLOUD_AUTO_CAPABILITIES)
        if auto_caps:
            return f"Auto-routed to cloud for capabilities: {', '.join(auto_caps)}."
        if DIFFICULT_TASK_TERMS.search(combined):
            return "Auto-routed to cloud: difficult specialist work."
        return ""

    def _must_keep_local(self, subtask: Subtask) -> bool:
        if self.privacy_mode == "permissive":
            return False
        combined = f"{subtask.title}\n{subtask.prompt}"
        if self.privacy_mode == "strict" and subtask.sensitivity in {"medium", "high"}:
            return True
        if subtask.sensitivity == "high":
            return True
        return bool(SENSITIVE_TERMS.search(combined))
