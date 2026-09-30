"""Flat, msgpack-safe state for CMN-C1-149."""

# ADR-005: State must be a flat TypedDict — never Pydantic BaseModel.
# LangGraph checkpoints use msgpack serialization; Pydantic objects
# cause silent corruption.  Extend AgentState with agent-specific
# fields only.  Do NOT add credentials, secrets, or Pydantic models.

from framework.schemas.agent_state import AgentState


class State(AgentState):
    """Agent state for TaskDecompositionAgent.

    Add only fields specific to this agent below.
    All shared fields (user_input, status, session_id, node_history,
    error_log, hitl_*, etc.) are inherited from AgentState.
    """

    # --- Input ---
    raw_query: str
    team_velocity_points: int
    max_task_points: int
    max_tasks: int
    response_budget: int
    max_input_length: int

    # --- Pre-process outputs ---
    query_id: str
    normalized_query: str

    # --- Main (decomposition) outputs ---
    tasks: str  # JSON string: list of task dicts with story_points
    task_count: int
    total_story_points: int
    oversized_tasks: str  # JSON string: titles exceeding max_task_points
    sprint_plan: str  # JSON string: list of {sprint_id, tasks, points_total}
    sprint_count: int
    overflow_warning: bool
    validation_passed: bool
    validation_issues: str  # JSON string: list of issue descriptions

    # --- Post-process outputs ---
    answer: str
    confidence: str  # "high" | "medium" | "low"
    synthesis_mode: str  # "azure_openai" | "evidence_only"
    formatted_output: str

    # --- Error ---
    error_message: str | None
    recoverable_error_code: str | None
    input_error_message: str | None
    input_error_guidance: list[str]
