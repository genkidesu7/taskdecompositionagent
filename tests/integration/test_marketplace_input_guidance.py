# CMN-C1-149 — Integration: Marketplace output shaping and safe degradation

import json

from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel

from src.graph.graph import Graph
from src.nodes.main_node import MainNode

_REQUIREMENT = "Build a customer self-service portal with SSO login and order history"


def _tasks(count=3, task_type="implementation"):
    return [
        {
            "title": f"Capability {index}",
            "description": f"Deliver capability {index}",
            "acceptance_criteria": [f"Capability {index} works"],
            "task_type": task_type,
            "dependencies": [],
        }
        for index in range(count)
    ]


class _PlanLLM:
    def __init__(self, payload=None):
        self._payload = payload if payload is not None else _tasks()

    def generate(self, system_prompt, user_prompt):
        return json.dumps(self._payload)


class _LeakyLLM:
    def generate(self, system_prompt, user_prompt):
        return json.dumps(
            [
                {
                    "title": "Confirm scope",
                    "description": "Email jane.doe@example.com to confirm scope",
                    "acceptance_criteria": ["Scope confirmed"],
                    "task_type": "research",
                    "dependencies": [],
                }
            ]
        )


class _UnavailableMainNode(MainNode):
    def _generate(self, state, system_prompt, user_prompt, max_tokens):
        raise ConnectionError("private backend detail")


class _PlanGraph(Graph):
    def register_nodes(self):
        super().register_nodes()
        self._nodes["main"] = MainNode(llm=_PlanLLM())


class _LeakyGraph(Graph):
    def register_nodes(self):
        super().register_nodes()
        self._nodes["main"] = MainNode(llm=_LeakyLLM())


class _UnavailableGraph(Graph):
    def register_nodes(self):
        super().register_nodes()
        self._nodes["main"] = _UnavailableMainNode()


def _invoke(graph, query=_REQUIREMENT):
    return graph.invoke(
        query,
        ctx=InvocationContext(caller_trust_level=TrustLevel.INTERNAL),
        input_context={"conversation_history": []},
    )


def test_short_marketplace_request_returns_readable_guidance():
    graph = Graph(config={})
    graph.compile()

    result = _invoke(graph, "Hello")

    assert result["status"] == "success"
    assert result["output"].startswith("The decomposition request could not be processed.")
    assert "How to continue:" in result["output"]


def test_success_marketplace_output_is_plan_but_api_stays_json():
    graph = Graph(config={})
    formatted_output = json.dumps({"answer": "## Sprint plan\n\n- Work items: 3", "confidence": "high"})

    api_result = graph.get_output({"formatted_output": formatted_output})
    marketplace_result = graph.get_output(
        {"formatted_output": formatted_output, "input_context": {"conversation_history": []}}
    )

    assert api_result["output"] == formatted_output
    assert marketplace_result["output"] == "## Sprint plan\n\n- Work items: 3"


def test_marketplace_loads_runtime_config_by_default():
    graph = Graph()
    graph.register_nodes()

    assert graph._extra_initial_state() == {
        "team_velocity_points": 20,
        "max_task_points": 8,
        "max_tasks": 50,
        "response_budget": 2500,
        "max_input_length": 2000,
    }
    assert graph._nodes["main"]._llm_timeout_s == 30
    assert graph._nodes["main"]._llm_max_retry == 3


def test_end_to_end_plan_reaches_marketplace():
    graph = _PlanGraph(config={})
    graph.compile()

    result = _invoke(graph)

    assert result["status"] == AgentStatus.SUCCESS
    assert "Sprint plan" in result["output"]
    assert "Capability 0" in result["output"]


def test_personal_data_in_a_task_is_masked_without_failing_invocation():
    graph = _LeakyGraph(config={})
    graph.compile()

    result = _invoke(graph)

    assert result["status"] == AgentStatus.SUCCESS
    assert "jane.doe@example.com" not in result["output"]


def test_decomposition_outage_returns_guidance_without_failing_invocation():
    graph = _UnavailableGraph(config={})
    graph.compile()

    result = _invoke(graph)

    assert result["status"] == AgentStatus.SUCCESS
    assert "temporarily unavailable" in result["output"].lower()
    assert "private backend detail" not in result["output"]
