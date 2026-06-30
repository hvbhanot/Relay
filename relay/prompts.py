from __future__ import annotations

from .schema import Message, Subtask, SubtaskResult

PLANNER_SYSTEM = """You are the local planning model in Relay, a local-first model router.
Your job is to decompose the user's request into focused, independent subtasks.

Decomposition rules (important):
- Split aggressively when the request has multiple goals, steps, deliverables, or domains.
- One subtask = one clear outcome (design OR implement OR research OR verify OR summarize).
- If the user lists numbered/bulleted parts, create at least one subtask per part.
- Compound requests (e.g. design + implement + prove + research) should become 3–6 subtasks,
  not one mega-task.
- Prefer independent subtasks that can run in parallel (empty depends_on).
- Only set depends_on when a later step truly needs output from a specific earlier step.
- Only use a single subtask for truly simple, one-shot questions.

Relay chooses local vs cloud and the specific model automatically from each subtask's capabilities.
Difficult work (coding, math, proofs, architecture, complex analysis) NEVER runs locally — tag it correctly.
Always set preferred_route to "auto". Tag capabilities accurately so routing can pick the right model:
- general, creative → only for truly simple one-shot questions
- reasoning, math, coding, high_stakes → any non-trivial or specialist work (routes to cloud)
- current_info, sources, large_context → online or very large context needs

Return ONLY a JSON object with this shape:
{
  "summary": "short plan summary",
  "requires_online": false,
  "final_response_instructions": "how the final answer should be assembled",
  "subtasks": [
    {
      "id": "task_1",
      "title": "short title",
      "prompt": "complete standalone instruction for the worker model",
      "preferred_route": "auto",
      "capabilities": ["general|reasoning|coding|math|current_info|sources|high_stakes|large_context|creative|vision"],
      "depends_on": [],
      "sensitivity": "low|medium|high",
      "rationale": "why this subtask exists and which capabilities drive routing"
    }
  ]
}
Do NOT collapse multi-part work into a single subtask.
"""

EVALUATOR_SYSTEM = """You are a local confidence evaluator.
Judge whether the answer is sufficient for the subtask. Return ONLY JSON:
{"confidence": 0.0, "needs_cloud_retry": false, "reason": "short reason"}
Use needs_cloud_retry=true only if the answer is likely incomplete or stale and online fallback would help.
"""

SYNTHESIS_SYSTEM = """You are the local synthesis model in Relay, a local-first model router.
Assemble the subtask results into one complete, self-contained final answer for the user.

The user only ever sees your answer — they cannot see the individual subtask results.
So you must INLINE everything they need. Never tell the user to "refer to the subtask
results", "see the subtask output", "see above", or "the previous step" — there is nothing
else for them to look at.

For code and other concrete deliverables:
- Reproduce every file and code snippet from the subtask results IN FULL and verbatim.
- Never abbreviate, summarize, elide, or replace code with comments like "// ... rest
  unchanged" or "(full code in subtask results)". Include the entire contents of each file.
- Put each file in its own fenced code block whose info line is the language followed by the
  file path, e.g. ```python app/models.py — so every file is clearly labelled.

Be clear about any cloud-routed work, uncertainty, or missing online access.
Do not invent facts, code, or files that are not present in the subtask results.
"""


def planner_messages(user_prompt: str, max_subtasks: int, *, attachment_hint: str = "") -> list[Message]:
    target = min(max_subtasks, max(3, max_subtasks // 2 + 1))
    extra = ""
    if attachment_hint:
        extra = f"\nAttachment note: {attachment_hint}"
    return [
        Message(
            "system",
            PLANNER_SYSTEM
            + f"\nMaximum subtasks: {max_subtasks}."
            + f" For compound requests, aim for {target}–{max_subtasks} subtasks instead of one."
            + extra,
        ),
        Message("user", user_prompt),
    ]


def subtask_messages(
    original_prompt: str,
    subtask: Subtask,
    prior_results: list[SubtaskResult],
    *,
    images: list[str] | None = None,
    conversation_history: str = "",
) -> list[Message]:
    context = ""
    if conversation_history:
        context += f"\n\n{conversation_history}"
    if prior_results:
        context += "\n\nPrior subtask results:\n" + "\n\n".join(
            f"[{r.subtask.id} via {r.route}] {r.content}" for r in prior_results
        )
    return [
        Message("system", "You are executing one subtask for Relay, a local-first model router. Answer only this subtask."),
        Message(
            "user",
            f"Original user request:\n{original_prompt}\n\nSubtask {subtask.id}: {subtask.title}\n{subtask.prompt}{context}",
            images=list(images or []),
        ),
    ]


def evaluator_messages(subtask: Subtask, answer: str) -> list[Message]:
    return [
        Message("system", EVALUATOR_SYSTEM),
        Message("user", f"Subtask:\n{subtask.prompt}\n\nAnswer:\n{answer}"),
    ]


def synthesis_messages(
    original_prompt: str,
    instructions: str,
    results: list[SubtaskResult],
    *,
    conversation_history: str = "",
) -> list[Message]:
    body = "\n\n".join(
        f"Subtask {r.subtask.id}: {r.subtask.title}\nRoute: {r.route}\nRoute reason: {r.reason}\n"
        f"Confidence: {r.confidence if r.confidence is not None else 'unknown'}\n"
        f"Error: {r.error or 'none'}\nResult:\n{r.content}"
        for r in results
    )
    history_block = f"\n\n{conversation_history}" if conversation_history else ""
    return [
        Message("system", SYNTHESIS_SYSTEM),
        Message(
            "user",
            f"Original user request:\n{original_prompt}{history_block}\n\nFinal response instructions:\n{instructions}\n\nSubtask results:\n{body}",
        ),
    ]
