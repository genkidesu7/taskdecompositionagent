"""Pre-process node for TaskDecompositionAgent.

Sanitizes the requirement text and enforces the input bounds that make a
decomposition meaningful.
"""

from __future__ import annotations

import re
import uuid
from typing import Any, ClassVar

from framework.errors import SecurityViolationError
from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

_INJECTION_PATTERNS = [
    re.compile(r"<script[^>]*>.*?</script>", re.IGNORECASE | re.DOTALL),
    re.compile(r"<[^>]+>"),
    re.compile(r"javascript:", re.IGNORECASE),
    re.compile(r"on\w{1,20}\s{0,4}=", re.IGNORECASE),
]

# S-2: attempts to steer the planning LLM away from the stated requirement.
# Every quantifier is bounded so a hostile input cannot force quadratic-time
# backtracking.
_PROMPT_INJECTION_PATTERNS = [
    re.compile(r"ignore\s{1,4}(?:all\s{1,4}){0,1}previous\s{1,4}instructions", re.IGNORECASE),
    re.compile(r"disregard\s{1,4}(?:all\s{1,4}){0,1}(?:the\s{1,4}){0,1}(?:above|prior)", re.IGNORECASE),
    re.compile(r"system\s{0,4}prompt", re.IGNORECASE),
    re.compile(r"you\s{1,4}are\s{1,4}now", re.IGNORECASE),
    re.compile(r"forget\s{1,4}(?:everything|all)\s{1,4}(?:above|before)", re.IGNORECASE),
]

# A requirement shorter than this cannot be decomposed into distinct work items;
# it is a topic, not a specification.
_MIN_REQUIREMENT_CHARS = 30


class PreProcessNode(FunctionNode):
    """Sanitize the requirement and enforce decomposition input bounds."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.INTERNAL

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        if state.get("error_message"):
            result = {"status": AgentStatus.ERROR.value}
            emit_trace_event("pre_process_skipped", {"reason": "upstream_error"}, result)
            return result

        raw_query = str(state.get("raw_query") or state.get("user_input") or "")
        cfg = (state.get("config") or {}).get("configurable", {})
        agent_config = cfg.get("agent_config", cfg)
        max_input_length = int(state.get("max_input_length") or agent_config.get("max_input_length", 2000))

        if len(raw_query) > max_input_length:
            result = self._input_error(
                raw_query,
                f"The requirement is too long ({len(raw_query)} characters). "
                f"The maximum is {max_input_length} characters.",
            )
            emit_trace_event("pre_process_rejected", {"reason": "input_too_long"}, result)
            return result

        normalized_query = self._sanitize(raw_query)
        if not normalized_query.strip():
            if raw_query.strip():
                result = {
                    "raw_query": raw_query,
                    "error_message": "S-1 violation: requirement is empty after sanitization",
                    "status": AgentStatus.ERROR.value,
                }
            else:
                result = self._input_error(raw_query, "No usable requirement was provided.")
            emit_trace_event("pre_process_rejected", {"reason": "empty_after_sanitization"}, result)
            return result

        if len(normalized_query) < _MIN_REQUIREMENT_CHARS:
            result = self._input_error(
                normalized_query,
                "The requirement is too short to decompose into distinct work items.",
            )
            emit_trace_event("pre_process_rejected", {"reason": "input_too_short"}, result)
            return result

        query_id = str(uuid.uuid4())
        emit_trace_event(
            "pre_process_completed",
            {"query_id": query_id, "requirement_length": len(normalized_query)},
            state,
        )

        return {
            "raw_query": raw_query,
            "query_id": query_id,
            "normalized_query": normalized_query,
            "error_message": None,
            "status": AgentStatus.SUCCESS.value,
        }

    def _extra_security_gate_input(self, state: dict[str, Any]) -> dict[str, Any]:
        """S-2: block attempts to steer the planner away from the requirement."""
        query = str(state.get("raw_query") or state.get("user_input") or "")
        if any(pattern.search(query) for pattern in _PROMPT_INJECTION_PATTERNS):
            raise SecurityViolationError(
                "PreProcessNode: blocked prompt-injection pattern in the decomposition request"
            )
        return state

    @staticmethod
    def _input_error(raw_query: str, message: str) -> dict[str, Any]:
        return {
            "raw_query": raw_query,
            "error_message": message,
            "input_error_message": message,
            "input_error_guidance": [
                "State what is being built and what it must do, for example: "
                "build a customer portal with SSO login and order history.",
                "Include the distinct capabilities you expect — each becomes a work item.",
            ],
            "status": AgentStatus.SUCCESS.value,
        }

    @staticmethod
    def _sanitize(text: str) -> str:
        result = text
        for pattern in _INJECTION_PATTERNS:
            result = pattern.sub("", result)
        return " ".join(result.split())
