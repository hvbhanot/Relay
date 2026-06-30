from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal

Role = Literal["system", "user", "assistant", "tool"]
RouteName = Literal["local", "cloud"]
PrivacyMode = Literal["strict", "balanced", "permissive"]


@dataclass(slots=True)
class Message:
    role: Role
    content: str
    images: list[str] = field(default_factory=list)


class Capability(str, Enum):
    GENERAL = "general"
    REASONING = "reasoning"
    CODING = "coding"
    MATH = "math"
    CURRENT_INFO = "current_info"
    SOURCES = "sources"
    HIGH_STAKES = "high_stakes"
    LARGE_CONTEXT = "large_context"
    CREATIVE = "creative"
    VISION = "vision"


@dataclass(slots=True)
class Subtask:
    id: str
    title: str
    prompt: str
    preferred_route: Literal["auto", "local", "cloud"] = "auto"
    capabilities: list[str] = field(default_factory=lambda: [Capability.GENERAL.value])
    depends_on: list[str] = field(default_factory=list)
    sensitivity: Literal["low", "medium", "high"] = "medium"
    rationale: str = ""
    model_override: str | None = None


@dataclass(slots=True)
class Plan:
    summary: str
    subtasks: list[Subtask]
    final_response_instructions: str = "Synthesize the subtask results into a clear final response."
    requires_online: bool = False


@dataclass(slots=True)
class RouteDecision:
    route: RouteName
    reason: str
    cloud_allowed: bool


@dataclass(slots=True)
class TokenUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float | None = None


@dataclass(slots=True)
class SubtaskResult:
    subtask: Subtask
    route: RouteName
    content: str
    confidence: float | None = None
    reason: str = ""
    error: str | None = None
    model: str | None = None
    usage: TokenUsage | None = None
    duration_seconds: float | None = None


@dataclass(slots=True)
class RouterTrace:
    user_prompt: str
    plan: Plan
    route_decisions: dict[str, RouteDecision]
    results: list[SubtaskResult]
    final_answer: str
    metadata: dict[str, Any] = field(default_factory=dict)
