# CMN-C1-149 — Unit Tests: PostProcess Node

import json

import pytest

from framework.schemas.agent_status import AgentStatus
from src.nodes.post_process_node import PostProcessNode


def _task(title, points=5, task_type="implementation", label="M"):
    return {
        "title": title,
        "description": f"Deliver {title}",
        "acceptance_criteria": [f"{title} works"],
        "task_type": task_type,
        "story_points": points,
        "size_label": label,
    }


def _state(**overrides):
    tasks = [_task("SSO login"), _task("Order history"), _task("Returns flow")]
    state = {
        "query_id": "q-1",
        "tasks": json.dumps(tasks),
        "task_count": len(tasks),
        "total_story_points": 15,
        "oversized_tasks": "[]",
        "sprint_plan": json.dumps([{"sprint_id": 1, "tasks": [t["title"] for t in tasks], "points_total": 15}]),
        "sprint_count": 1,
        "overflow_warning": False,
        "validation_passed": True,
        "validation_issues": "[]",
    }
    state.update(overrides)
    return state


class TestPostProcessRendering:
    """BL-07: the report is assembled from the computed plan."""

    def setup_method(self):
        self.node = PostProcessNode()

    def test_plan_is_rendered(self):
        result = self.node.execute(_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "Sprint plan" in result["answer"]
        assert "SSO login" in result["answer"]

    def test_figures_match_state(self):
        """A rendered report cannot drift from the state that produced it."""
        result = self.node.execute(_state())
        assert "Work items: 3" in result["answer"]
        assert "Total story points: 15" in result["answer"]
        assert "Sprints required: 1" in result["answer"]

    def test_synthesis_mode_is_evidence_only(self):
        assert self.node.execute(_state())["synthesis_mode"] == "evidence_only"

    def test_overflow_warning_is_surfaced(self):
        result = self.node.execute(_state(overflow_warning=True, sprint_count=2))
        assert "does not fit a single sprint" in result["answer"]

    def test_oversized_items_are_called_out(self):
        result = self.node.execute(_state(oversized_tasks=json.dumps(["Returns flow"])))
        assert "Items to split before planning" in result["answer"]
        assert "Returns flow" in result["answer"]

    def test_validation_issues_are_listed(self):
        result = self.node.execute(
            _state(validation_passed=False, validation_issues=json.dumps(["Task 'X' is missing: description"]))
        )
        assert "Validation issues" in result["answer"]

    def test_acceptance_criteria_are_rendered(self):
        assert "Acceptance:" in self.node.execute(_state())["answer"]

    def test_no_tasks_reports_no_work_items(self):
        result = self.node.execute(_state(tasks="[]", task_count=0))
        assert "No work items produced" in result["answer"]
        assert result["confidence"] == "low"

    def test_input_error_is_surfaced_as_guidance(self):
        result = self.node.execute({"input_error_message": "too short", "input_error_guidance": ["State scope"]})
        payload = json.loads(result["formatted_output"])
        assert payload["status"] == "input_error"

    def test_decomposition_outage_returns_actionable_answer(self):
        result = self.node.execute({"recoverable_error_code": "decomposition_unavailable", "trace_id": "tr-9"})
        assert "temporarily unavailable" in result["answer"].lower()
        assert "tr-9" in result["answer"]

    def test_upstream_error_short_circuits(self):
        assert self.node.execute({"error_message": "boom"})["status"] == AgentStatus.ERROR.value


class TestPostProcessConfidence:
    """BL-08: confidence reflects how plannable the decomposition is."""

    @pytest.mark.parametrize(
        "tasks,oversized,passed,expected",
        [
            ([_task("a"), _task("b"), _task("c")], [], True, "high"),
            ([_task("a"), _task("b")], [], True, "medium"),
            ([_task("a"), _task("b"), _task("c")], ["a"], True, "medium"),
            ([_task("a")], [], False, "low"),
            ([], [], True, "low"),
        ],
    )
    def test_confidence_tiers(self, tasks, oversized, passed, expected):
        assert PostProcessNode._compute_confidence(tasks, oversized, passed) == expected


class TestPostProcessS3SecurityGate:
    """TC-10 / BL-09."""

    def setup_method(self):
        self.node = PostProcessNode()

    def _gate(self, answer):
        return self.node._extra_security_gate_output(
            {"answer": answer, "formatted_output": json.dumps({"answer": answer})}
        )

    def test_clean_plan_passes_unchanged(self):
        answer = "## Sprint plan\n\n- Work items: 3\n- Total story points: 15"
        assert self._gate(answer)["answer"] == answer

    def test_planning_vocabulary_is_not_masked_as_a_person_name(self):
        answer = "## Sprint plan\n\nAcceptance Criteria and Story Points are recorded per item."
        assert self._gate(answer)["answer"] == answer

    def test_personal_data_is_masked_not_raised(self):
        """A requirement often names its requester; masking keeps the plan usable."""
        gated = self._gate("## Sprint plan\n\nConfirm scope with jane.doe@example.com before sprint 1.")
        assert "jane.doe@example.com" not in gated["answer"]
        assert "redacted" in gated["answer"].lower()

    def test_masked_plan_keeps_formatted_output_in_sync(self):
        gated = self._gate("## Sprint plan\n\nEmail jane.doe@example.com for scope.")
        assert json.loads(gated["formatted_output"])["answer"] == gated["answer"]

    def test_credential_blocks_the_output(self, monkeypatch):
        import src.nodes.post_process_node as module

        monkeypatch.setattr(module, "detect_credentials", lambda text: [{"type": "custom"}])
        with pytest.raises(RuntimeError, match="S-3 blocked"):
            self._gate("## Sprint plan\n\nnothing sensitive")


class TestPostProcessAuditLogging:
    """TC-11 / PB-1."""

    def test_success_path_emits_domain_event(self, monkeypatch):
        import src.nodes.post_process_node as module

        events = []
        monkeypatch.setattr(module, "emit_trace_event", lambda name, payload, state: events.append(name))
        PostProcessNode().execute(_state())
        assert "post_process_completed" in events

    def test_no_tasks_path_emits_domain_event(self, monkeypatch):
        import src.nodes.post_process_node as module

        events = []
        monkeypatch.setattr(module, "emit_trace_event", lambda name, payload, state: events.append(name))
        PostProcessNode().execute(_state(tasks="[]"))
        assert "post_process_no_tasks" in events
