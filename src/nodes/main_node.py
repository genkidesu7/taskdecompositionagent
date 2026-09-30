"""Main node for TaskDecompositionAgent.

Decomposes the requirement into atomic tasks via the LLM, then applies
deterministic estimation, sprint bin-packing, and structural validation.
"""

from __future__ import annotations

import json
import re
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.security.pii_detector import detect_pii
from shared.utils.audit_logger import emit_trace_event
from src.services.llm_runtime import complete_text


def _emit_progress(message: str, metadata: dict[str, Any]) -> None:
    """Emit Marketplace progress when the platform event transport is available."""
    try:
        from shared.services.events import emitter
        from shared.services.events.types import EventType
    except (ImportError, ModuleNotFoundError):
        # Lightweight local AgentCore installations may omit Marketplace events.
        return
    emitter().emit_event(event_type=EventType.PROGRESS_UPDATE, message=message, metadata=metadata)


_DECOMPOSE_PROMPT = (
    "You are a delivery lead decomposing a requirement into atomic, "
    "agent-executable work items.\n\n"
    "Return ONLY a JSON array. Each element MUST have exactly these keys:\n"
    '  "title"               short imperative name\n'
    '  "description"         what the item delivers\n'
    '  "acceptance_criteria" a list of verifiable statements\n'
    '  "task_type"           one of: research, design, implementation, integration, testing, docs\n'
    '  "dependencies"        a list of titles this item depends on (may be empty)\n\n'
    "Rules:\n"
    "1. Each item must be completable in roughly half a day by one person\n"
    "2. Split anything larger rather than describing it as one item\n"
    "3. Use only what the requirement states -- never invent scope\n"
    "4. Output the JSON array and nothing else"
)

# Rule-based sizing. Estimation is deterministic rather than asked of the model,
# so the same requirement always yields the same plan and the numbers survive a
# provider outage.
_TASK_TYPE_POINTS: dict[str, int] = {
    "research": 2,
    "design": 3,
    "implementation": 5,
    "integration": 5,
    "testing": 3,
    "docs": 1,
}
_DEFAULT_POINTS = 3

_SIZE_LABELS: list[tuple[int, str]] = [(2, "XS"), (3, "S"), (5, "M"), (8, "L")]
_OVERSIZED_LABEL = "XL"

_REQUIRED_TASK_FIELDS = ("title", "description", "acceptance_criteria", "task_type")

_JSON_ARRAY_RE = re.compile(r"\[[\s\S]{2,200000}\]")


class MainNode(FunctionNode):
    """Decompose, estimate, pack into sprints, and validate."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.INTERNAL

    def __init__(
        self,
        llm: Any | None = None,
        *,
        llm_timeout_s: float = 30.0,
        llm_max_retry: int = 3,
    ) -> None:
        super().__init__()
        # Unit-test seam only. Production resolves Azure OpenAI from the
        # invocation-scoped secrets declared by the agent manifest.
        self._llm = llm
        self._llm_timeout_s = llm_timeout_s
        self._llm_max_retry = llm_max_retry

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        if state.get("input_error_message"):
            return {"status": AgentStatus.SUCCESS.value}

        if state.get("error_message"):
            result = {"status": AgentStatus.ERROR.value}
            emit_trace_event("main_skipped", {"reason": "upstream_error"}, result)
            return result

        cfg = (state.get("config") or {}).get("configurable", {})
        agent_config = cfg.get("agent_config", cfg)
        velocity = int(state.get("team_velocity_points") or agent_config.get("team_velocity_points", 20))
        max_task_points = int(state.get("max_task_points") or agent_config.get("max_task_points", 8))
        max_tasks = int(state.get("max_tasks") or agent_config.get("max_tasks", 50))
        budget = int(state.get("response_budget") or agent_config.get("llm_max_tokens", 2500))

        requirement = str(state.get("normalized_query") or state.get("raw_query") or "")

        _emit_progress(
            "Decomposing the requirement with Azure OpenAI...",
            {"stage": "decomposition"},
        )
        try:
            raw = self._generate(state, _DECOMPOSE_PROMPT, f"Requirement:\n{requirement}", budget)
            tasks = self._parse_tasks(raw)
            decomposition_mode = "azure_openai"
        except Exception as exc:
            emit_trace_event(
                "main_decomposition_failed",
                {"query_id": state.get("query_id"), "reason": type(exc).__name__},
                state,
            )
            _emit_progress(
                "Azure OpenAI decomposition is temporarily unavailable.",
                {"stage": "decomposition", "status": "error"},
            )
            return {
                "tasks": "[]",
                "task_count": 0,
                "total_story_points": 0,
                "oversized_tasks": "[]",
                "sprint_plan": "[]",
                "sprint_count": 0,
                "overflow_warning": False,
                "validation_passed": False,
                "validation_issues": json.dumps(["Decomposition was unavailable; no plan was produced."]),
                "error_message": (
                    f"MainNode: requirement decomposition is temporarily unavailable ({type(exc).__name__})"
                ),
                "recoverable_error_code": "decomposition_unavailable",
                # Handled by PostProcessNode so Marketplace receives an actionable
                # response instead of treating the invocation itself as failed.
                "status": AgentStatus.SUCCESS.value,
            }

        tasks = tasks[:max_tasks]
        estimated = [self._estimate(task) for task in tasks]
        oversized = [t["title"] for t in estimated if t["story_points"] > max_task_points]
        plan = self._assign_sprints(estimated, velocity)
        validation = self._validate(estimated)

        emit_trace_event(
            "main_completed",
            {
                "query_id": state.get("query_id"),
                "task_count": len(estimated),
                "sprint_count": plan["sprint_count"],
                "oversized_count": len(oversized),
                "validation_passed": validation["passed"],
                "decomposition_mode": decomposition_mode,
            },
            state,
        )

        return {
            "tasks": json.dumps(estimated),
            "task_count": len(estimated),
            "total_story_points": sum(t["story_points"] for t in estimated),
            "oversized_tasks": json.dumps(oversized),
            "sprint_plan": json.dumps(plan["sprints"]),
            "sprint_count": plan["sprint_count"],
            "overflow_warning": plan["overflow_warning"],
            "validation_passed": validation["passed"],
            "validation_issues": json.dumps(validation["issues"]),
            "error_message": None,
            "recoverable_error_code": None,
            "status": AgentStatus.SUCCESS.value,
        }

    def _generate(self, state: dict[str, Any], system_prompt: str, user_prompt: str, max_tokens: int) -> str:
        config = state.get("config")
        configurable = config.get("configurable", {}) if isinstance(config, dict) else {}
        injected = self._llm or configurable.get("llm")
        return complete_text(
            state,
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            injected,
            max_tokens=max_tokens,
            timeout_s=self._llm_timeout_s,
            max_retry=self._llm_max_retry,
        )

    @staticmethod
    def _parse_tasks(raw: str) -> list[dict[str, Any]]:
        """Extract the task array, tolerating prose or a fence around the JSON."""
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            match = _JSON_ARRAY_RE.search(raw)
            if not match:
                raise ValueError("decomposition response contained no JSON array") from None
            parsed = json.loads(match.group(0))
        if not isinstance(parsed, list):
            raise ValueError("decomposition response was not a JSON array")
        return [item for item in parsed if isinstance(item, dict)]

    @staticmethod
    def _estimate(task: dict[str, Any]) -> dict[str, Any]:
        """Rule-based sizing by task type, with a size label for reporting."""
        sized = dict(task)
        task_type = str(sized.get("task_type", "")).lower()
        points = _TASK_TYPE_POINTS.get(task_type, _DEFAULT_POINTS)
        sized["task_type"] = task_type or "implementation"
        sized["story_points"] = points
        sized["size_label"] = next(
            (label for threshold, label in _SIZE_LABELS if points <= threshold), _OVERSIZED_LABEL
        )
        return sized

    @staticmethod
    def _assign_sprints(tasks: list[dict[str, Any]], velocity: int) -> dict[str, Any]:
        """Greedy bin-packing into sprints of at most `velocity` points."""
        if not tasks:
            return {"sprints": [], "sprint_count": 0, "overflow_warning": False}

        sprints: list[dict[str, Any]] = []
        current: dict[str, Any] = {"tasks": [], "points_total": 0}

        for task in tasks:
            points = int(task.get("story_points", _DEFAULT_POINTS))
            # A single task larger than a whole sprint gets its own sprint; it
            # cannot be packed and should be visible as an outlier.
            if points > velocity:
                if current["tasks"]:
                    sprints.append(current)
                    current = {"tasks": [], "points_total": 0}
                sprints.append({"tasks": [task["title"]], "points_total": points})
                continue
            if current["points_total"] + points > velocity:
                sprints.append(current)
                current = {"tasks": [task["title"]], "points_total": points}
            else:
                current["tasks"].append(task["title"])
                current["points_total"] += points

        if current["tasks"]:
            sprints.append(current)

        for index, sprint in enumerate(sprints, start=1):
            sprint["sprint_id"] = index

        return {
            "sprints": sprints,
            "sprint_count": len(sprints),
            "overflow_warning": len(sprints) > 1,
        }

    @staticmethod
    def _validate(tasks: list[dict[str, Any]]) -> dict[str, Any]:
        """Structural validation — every task must carry the required fields."""
        issues: list[str] = []
        for task in tasks:
            title = str(task.get("title") or "<untitled>")
            missing = [field for field in _REQUIRED_TASK_FIELDS if not task.get(field)]
            if missing:
                issues.append(f"Task '{title}' is missing: {', '.join(sorted(missing))}")
        return {"passed": not issues, "issues": issues}

    def _extra_security_gate_input(self, state: dict[str, Any]) -> dict[str, Any]:
        """S-2: PII masking on the requirement before it reaches the provider."""
        raw = state.get("normalized_query", "")
        if not raw:
            return state
        findings = detect_pii(raw)
        if findings:
            for finding in reversed(findings):
                raw = raw[: finding["start"]] + "[MASKED]" + raw[finding["end"] :]
            state["normalized_query"] = raw
            emit_trace_event("pii_masked_requirement", {}, state)
        return state
