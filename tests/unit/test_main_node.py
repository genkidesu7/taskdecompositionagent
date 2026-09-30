# CMN-C1-149 — Unit Tests: Main Node

import json

import pytest

from framework.schemas.agent_status import AgentStatus
from src.nodes.main_node import MainNode


def _task(title, task_type="implementation", **extra):
    task = {
        "title": title,
        "description": f"Deliver {title}",
        "acceptance_criteria": [f"{title} works"],
        "task_type": task_type,
        "dependencies": [],
    }
    task.update(extra)
    return task


class _StubLLM:
    def __init__(self, payload=None, raw=None):
        self.raw = raw if raw is not None else json.dumps(payload or [_task("SSO login")])
        self.calls = []

    def complete(self, messages):
        self.calls.append(messages)
        return {"content": self.raw}


class _FailingLLM:
    def complete(self, messages):
        raise RuntimeError("provider unavailable")


def _state(**overrides):
    state = {
        "query_id": "q-1",
        "normalized_query": "Build a customer portal with SSO login and order history",
        "team_velocity_points": 20,
        "max_task_points": 8,
        "max_tasks": 50,
        "response_budget": 2500,
    }
    state.update(overrides)
    return state


class TestMainNodeDecomposition:
    """BL-02 / BL-03: decomposition and response parsing."""

    def test_tasks_are_decomposed(self):
        node = MainNode(llm=_StubLLM(payload=[_task("SSO login"), _task("Order history")]))
        result = node.execute(_state())

        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["task_count"] == 2

    def test_json_wrapped_in_prose_is_recovered(self):
        """The model often narrates around the array; the array is what matters."""
        raw = f"Here is the plan:\n{json.dumps([_task('SSO login')])}\nLet me know."
        result = MainNode(llm=_StubLLM(raw=raw)).execute(_state())
        assert result["task_count"] == 1

    def test_unparseable_response_degrades_recoverably(self):
        result = MainNode(llm=_StubLLM(raw="I cannot help with that.")).execute(_state())

        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["recoverable_error_code"] == "decomposition_unavailable"
        assert result["task_count"] == 0

    def test_provider_failure_degrades_recoverably(self):
        result = MainNode(llm=_FailingLLM()).execute(_state())

        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["recoverable_error_code"] == "decomposition_unavailable"
        assert "provider unavailable" not in result["error_message"]

    def test_task_cap_is_enforced(self):
        payload = [_task(f"Task {i}") for i in range(80)]
        result = MainNode(llm=_StubLLM(payload=payload)).execute(_state(max_tasks=10))
        assert result["task_count"] == 10

    def test_upstream_input_error_short_circuits(self):
        assert MainNode(llm=_StubLLM()).execute({"input_error_message": "x"})["status"] == AgentStatus.SUCCESS.value

    def test_upstream_error_short_circuits(self):
        assert MainNode(llm=_StubLLM()).execute({"error_message": "boom"})["status"] == AgentStatus.ERROR.value


class TestMainNodeEstimation:
    """BL-04: rule-based sizing is deterministic."""

    @pytest.mark.parametrize(
        "task_type,points,label",
        [("docs", 1, "XS"), ("research", 2, "XS"), ("design", 3, "S"), ("implementation", 5, "M")],
    )
    def test_points_follow_task_type(self, task_type, points, label):
        sized = MainNode._estimate(_task("X", task_type=task_type))
        assert sized["story_points"] == points
        assert sized["size_label"] == label

    def test_unknown_task_type_gets_the_default(self):
        sized = MainNode._estimate(_task("X", task_type="archaeology"))
        assert sized["story_points"] == 3

    def test_estimation_is_reproducible(self):
        """The same requirement must always yield the same plan."""
        payload = [_task("SSO login"), _task("Order history", task_type="design")]
        first = MainNode(llm=_StubLLM(payload=payload)).execute(_state())
        second = MainNode(llm=_StubLLM(payload=payload)).execute(_state())
        assert first["tasks"] == second["tasks"]
        assert first["sprint_plan"] == second["sprint_plan"]


class TestMainNodeSprintPacking:
    """BL-05: greedy bin-packing against team velocity."""

    def test_tasks_within_velocity_fit_one_sprint(self):
        tasks = [MainNode._estimate(_task(f"T{i}", task_type="docs")) for i in range(5)]
        plan = MainNode._assign_sprints(tasks, velocity=20)
        assert plan["sprint_count"] == 1
        assert plan["overflow_warning"] is False

    def test_overflow_creates_additional_sprints(self):
        tasks = [MainNode._estimate(_task(f"T{i}", task_type="implementation")) for i in range(9)]
        plan = MainNode._assign_sprints(tasks, velocity=20)
        assert plan["sprint_count"] > 1
        assert plan["overflow_warning"] is True

    def test_no_sprint_exceeds_velocity(self):
        tasks = [MainNode._estimate(_task(f"T{i}", task_type="implementation")) for i in range(11)]
        plan = MainNode._assign_sprints(tasks, velocity=20)
        assert all(s["points_total"] <= 20 for s in plan["sprints"])

    def test_task_larger_than_a_sprint_gets_its_own(self):
        """An unpackable outlier must stay visible rather than distort a sprint."""
        oversized = dict(MainNode._estimate(_task("Huge")), story_points=30)
        plan = MainNode._assign_sprints([oversized], velocity=20)
        assert plan["sprint_count"] == 1
        assert plan["sprints"][0]["points_total"] == 30

    def test_sprint_ids_are_sequential(self):
        tasks = [MainNode._estimate(_task(f"T{i}", task_type="implementation")) for i in range(9)]
        plan = MainNode._assign_sprints(tasks, velocity=20)
        assert [s["sprint_id"] for s in plan["sprints"]] == list(range(1, plan["sprint_count"] + 1))

    def test_empty_task_list_yields_no_sprints(self):
        assert MainNode._assign_sprints([], velocity=20)["sprint_count"] == 0


class TestMainNodeValidationAndOversize:
    """BL-06: structural validation and oversize detection."""

    def test_missing_fields_are_reported(self):
        result = MainNode._validate([{"title": "Broken"}])
        assert result["passed"] is False
        assert "Broken" in result["issues"][0]

    def test_complete_tasks_pass(self):
        assert MainNode._validate([_task("Fine")])["passed"] is True

    def test_oversized_tasks_are_flagged(self):
        payload = [_task("Big", task_type="implementation")]
        result = MainNode(llm=_StubLLM(payload=payload)).execute(_state(max_task_points=3))
        assert json.loads(result["oversized_tasks"]) == ["Big"]


class TestMainNodeS2SecurityGate:
    """TC-09: PII in the requirement is masked before the provider sees it."""

    def test_pii_is_masked(self, monkeypatch):
        import src.nodes.main_node as module

        target = "pm@example.com"
        monkeypatch.setattr(
            module,
            "detect_pii",
            lambda text: [{"type": "email", "start": text.index(target), "end": text.index(target) + len(target)}],
        )
        gated = MainNode()._extra_security_gate_input({"normalized_query": f"Ask {target} for scope"})
        assert target not in gated["normalized_query"]


class TestMainNodeAuditLogging:
    """TC-11 / PB-1."""

    def test_success_path_emits_domain_event(self, monkeypatch):
        import src.nodes.main_node as module

        events = []
        monkeypatch.setattr(module, "emit_trace_event", lambda name, payload, state: events.append(name))
        MainNode(llm=_StubLLM()).execute(_state())
        assert "main_completed" in events

    def test_failure_path_emits_domain_event(self, monkeypatch):
        import src.nodes.main_node as module

        events = []
        monkeypatch.setattr(module, "emit_trace_event", lambda name, payload, state: events.append(name))
        MainNode(llm=_FailingLLM()).execute(_state())
        assert "main_decomposition_failed" in events
