"""Post-process node for TaskDecompositionAgent.

Renders the sprint plan as a readable report and applies the S-3 output gate.
The report is assembled deterministically from the computed plan, so the figures
it cites always match the state that produced them.
"""

from __future__ import annotations

import json
import re
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from framework.security.pii_masking import mask_pii
from shared.security.credential_detector import detect_credentials
from shared.security.pii_detector import detect_pii
from shared.utils.audit_logger import emit_trace_event

_DECOMPOSITION_UNAVAILABLE_ANSWER = (
    "## Decomposition temporarily unavailable\n\n"
    "I couldn't decompose the requirement into tasks right now, so no sprint plan was "
    "produced. Please try again shortly. If the problem continues, contact support and "
    "include the reference below."
)

_NO_TASKS_ANSWER = (
    "## No work items produced\n\n"
    "The requirement did not yield any distinct work items. State what is being built and "
    "which capabilities it must provide, then try again."
)

# The shared name detector favors recall and flags any run of Title Case words,
# so planning vocabulary matches as readily as a real name. A finding is treated
# as non-personal only when every capitalised token is planning vocabulary.
_DOMAIN_VOCABULARY = frozenset(
    {
        "acceptance",
        "criteria",
        "dependencies",
        "design",
        "docs",
        "implementation",
        "integration",
        "items",
        "plan",
        "points",
        "research",
        "size",
        "sprint",
        "sprints",
        "story",
        "task",
        "tasks",
        "testing",
        "total",
        "work",
        "add",
        "build",
        "create",
        "define",
        "implement",
        "note",
        "review",
        "set",
        "validate",
        "verify",
        "write",
    }
)

_NAME_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z'\-]{0,63}")


def _is_domain_phrase(value: str) -> bool:
    """True when every word in a detected name is planning vocabulary."""
    tokens = _NAME_TOKEN_RE.findall(value)
    if not tokens:
        return False
    return all(token.lower() in _DOMAIN_VOCABULARY for token in tokens)


class PostProcessNode(FunctionNode):
    """Render the sprint plan and enforce the output gate."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.INTERNAL

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        if state.get("input_error_message"):
            message = str(state["input_error_message"])
            guidance = state.get("input_error_guidance", [])
            return {
                "answer": message,
                "confidence": "low",
                "formatted_output": json.dumps(
                    {"status": "input_error", "message": message, "guidance": guidance},
                    ensure_ascii=False,
                ),
                "status": AgentStatus.SUCCESS.value,
            }

        if state.get("recoverable_error_code") == "decomposition_unavailable":
            answer = _DECOMPOSITION_UNAVAILABLE_ANSWER
            trace_id = str(state.get("trace_id") or "").strip()
            if trace_id:
                answer = f"{answer}\n\nReference: `{trace_id}`"
            emit_trace_event(
                "post_process_decomposition_unavailable",
                {"query_id": state.get("query_id"), "trace_id": trace_id},
                state,
            )
            return {
                "answer": answer,
                "confidence": "low",
                "synthesis_mode": "evidence_only",
                "formatted_output": json.dumps(
                    {
                        "status": "temporarily_unavailable",
                        "answer": answer,
                        "task_count": 0,
                        "sprint_count": 0,
                        "confidence": "low",
                    },
                    ensure_ascii=False,
                ),
                "error_message": None,
                "recoverable_error_code": None,
                "status": AgentStatus.SUCCESS.value,
            }

        if state.get("error_message"):
            result = {"status": AgentStatus.ERROR.value}
            emit_trace_event("post_process_skipped", {"reason": "upstream_error"}, result)
            return result

        tasks = self._load_json_list(state.get("tasks"))
        sprints = self._load_json_list(state.get("sprint_plan"))
        oversized = self._load_json_strings(state.get("oversized_tasks"))
        issues = self._load_json_strings(state.get("validation_issues"))

        if not tasks:
            emit_trace_event("post_process_no_tasks", {"query_id": state.get("query_id")}, state)
            return {
                "answer": _NO_TASKS_ANSWER,
                "confidence": "low",
                "synthesis_mode": "evidence_only",
                "formatted_output": json.dumps(
                    {"answer": _NO_TASKS_ANSWER, "task_count": 0, "sprint_count": 0, "confidence": "low"},
                    ensure_ascii=False,
                ),
                "error_message": None,
                "status": AgentStatus.SUCCESS.value,
            }

        answer = self._render(state, tasks, sprints, oversized, issues)
        confidence = self._compute_confidence(tasks, oversized, bool(state.get("validation_passed")))

        emit_trace_event(
            "post_process_completed",
            {
                "query_id": state.get("query_id"),
                "task_count": len(tasks),
                "sprint_count": len(sprints),
                "confidence": confidence,
            },
            state,
        )

        return {
            "answer": answer,
            "confidence": confidence,
            # The report is assembled from the computed plan, not generated, so
            # its figures cannot drift from the state that produced them.
            "synthesis_mode": "evidence_only",
            "formatted_output": json.dumps(
                {
                    "answer": answer,
                    "task_count": len(tasks),
                    "total_story_points": state.get("total_story_points", 0),
                    "sprint_count": len(sprints),
                    "overflow_warning": bool(state.get("overflow_warning")),
                    "oversized_tasks": oversized,
                    "validation_passed": bool(state.get("validation_passed")),
                    "validation_issues": issues,
                    "confidence": confidence,
                },
                ensure_ascii=False,
            ),
            "error_message": None,
            "status": AgentStatus.SUCCESS.value,
        }

    @staticmethod
    def _render(
        state: dict[str, Any],
        tasks: list[dict[str, Any]],
        sprints: list[dict[str, Any]],
        oversized: list[str],
        issues: list[str],
    ) -> str:
        lines = [
            "## Sprint plan",
            "",
            f"- Work items: {len(tasks)}",
            f"- Total story points: {state.get('total_story_points', 0)}",
            f"- Sprints required: {len(sprints)}",
        ]
        if state.get("overflow_warning"):
            lines.append("- The work does not fit a single sprint at the configured velocity.")

        if oversized:
            lines.extend(
                [
                    "",
                    "### Items to split before planning",
                    "These exceed the maximum size for a single item:",
                ]
            )
            lines.extend(f"- {title}" for title in oversized)

        lines.extend(["", "### Work items"])
        for task in tasks:
            criteria = task.get("acceptance_criteria") or []
            criteria_text = "; ".join(str(c) for c in criteria) if isinstance(criteria, list) else str(criteria)
            lines.append(
                f"- **{task.get('title', 'untitled')}** "
                f"({task.get('task_type', 'implementation')}, {task.get('story_points', 0)} pts / "
                f"{task.get('size_label', '?')}) — {task.get('description', '')}"
            )
            if criteria_text:
                lines.append(f"  - Acceptance: {criteria_text}")

        if sprints:
            lines.extend(["", "### Sprint allocation"])
            for sprint in sprints:
                lines.append(
                    f"- Sprint {sprint.get('sprint_id', '?')} "
                    f"({sprint.get('points_total', 0)} pts): {', '.join(sprint.get('tasks', []))}"
                )

        if issues:
            lines.extend(["", "### Validation issues"])
            lines.extend(f"- {issue}" for issue in issues)

        return "\n".join(lines)

    @staticmethod
    def _compute_confidence(tasks: list[dict[str, Any]], oversized: list[str], validation_passed: bool) -> str:
        """Confidence reflects how plannable the decomposition actually is."""
        if not validation_passed or not tasks:
            return "low"
        if oversized:
            return "medium"
        return "high" if len(tasks) >= 3 else "medium"

    @staticmethod
    def _load_json_list(value: Any) -> list[dict[str, Any]]:
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if not isinstance(value, str):
            return []
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return []
        return [item for item in parsed if isinstance(item, dict)] if isinstance(parsed, list) else []

    @staticmethod
    def _load_json_strings(value: Any) -> list[str]:
        if isinstance(value, list):
            return [str(item) for item in value]
        if not isinstance(value, str):
            return []
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return []
        return [str(item) for item in parsed] if isinstance(parsed, list) else []

    def _extra_security_gate_output(self, result: dict[str, Any]) -> dict[str, Any]:
        """S-3: block credentials, mask residual personal data.

        A requirement often names the people who asked for it, and those names
        flow into task descriptions. Masking keeps the plan usable; raising would
        turn a recoverable filter decision into a failed invocation.
        """
        answer = result.get("answer", "")
        if not answer:
            return result

        if detect_credentials(answer):
            emit_trace_event("post_process_s3_credential_blocked", {}, result)
            raise RuntimeError("S-3 blocked: credential detected in output")

        findings = [
            finding
            for finding in detect_pii(answer)
            if finding.get("type") != "name" or not _is_domain_phrase(str(finding.get("value", "")))
        ]
        if findings:
            answer = mask_pii(answer, findings)
            answer = f"{answer}\n\n_Some personal information was redacted from this plan._"
            result["answer"] = answer
            self._update_formatted_output(result, answer)
            emit_trace_event(
                "post_process_s3_pii_redacted",
                {
                    "finding_count": len(findings),
                    "finding_types": sorted({str(item["type"]) for item in findings}),
                },
                result,
            )
        return result

    @staticmethod
    def _update_formatted_output(result: dict[str, Any], answer: str) -> None:
        raw = result.get("formatted_output")
        if not isinstance(raw, str):
            return
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return
        if not isinstance(payload, dict):
            return
        payload["answer"] = answer
        result["formatted_output"] = json.dumps(payload, ensure_ascii=False)
