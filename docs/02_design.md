# Template Design Specification

## Position in AgentCore Architecture

- **Agent Class**: TaskDecompositionAgent
- **L1 Base**: AgentBaseGraph
- **Category**: Cat 1
- **Generation Mode**: llm
- **Three-Layer Separation**:
  - State: flat TypedDict fields containing only msgpack-safe data.
  - Node: FunctionNode subclasses override execute and optional extra security hooks.
  - Graph: register_nodes composes the fixed framework pipeline.

## Architecture Overview

| Node | Responsibility | Input State | Output State | Inherits |
|------|----------------|-------------|--------------|----------|
| initialize | Framework invocation metadata | framework-managed | framework-managed | InitializeNode |
| pre_process | Sanitize the requirement and enforce decomposition input bounds | user_input or raw_query | normalized_query | FunctionNode |
| main | Decompose via the LLM, then estimate, pack, and validate deterministically | normalized requirement | tasks, sprint_plan, validation fields | FunctionNode |
| post_process | Render the plan and enforce the output gate | computed plan | answer, confidence, formatted_output | FunctionNode |
| finalize | Framework response metadata | framework-managed | framework-managed | FinalizeNode |

### Data Flow

    START → initialize → pre_process → main → post_process → finalize → END
                                      ↘ retry → pre_process

The LLM seam sits in MainNode, not PostProcessNode:

    self._nodes["main"] = MainNode(
        llm_timeout_s=config["timeout_s"],
        llm_max_retry=config["max_retry"],
    )
    self._nodes["post_process"] = PostProcessNode()

Decomposition is the only generated step. Everything after it — sizing, packing, validation,
and rendering — is computed, so PostProcessNode needs no provider access at all.

Production creates the Azure client from invocation-scoped secrets inside MainNode.
Constructor injection is a test seam only; clients are never written to State or checkpoint surfaces.

### Why only decomposition is generated

Splitting a requirement into work items needs language understanding. Sizing, packing, and
validating do not, and asking a model for them costs reproducibility: the same requirement would
yield different point totals on different runs, and a plan whose figures move cannot be tracked
against. Computing them also means the plan survives a provider outage in a useful form — the
decomposition fails visibly, but nothing silently produces different numbers.

### Estimation and packing

Sizing is a table lookup on `task_type` (docs 1, research 2, design 3, implementation and
integration 5), with a size label attached for reporting. An unrecognised type falls back to the
default rather than failing, because a plan with one mis-sized item is more useful than no plan.

Sprint packing is greedy bin-packing against `team_velocity_points`. A task larger than an entire
sprint is given its own sprint rather than being packed: it cannot fit, and burying it inside a
mixed sprint is exactly how an overrun goes unnoticed. Such items are also reported separately
under `oversized_tasks` so they are visible before the sprint starts.

Structural validation checks that every task carries a title, description, acceptance criteria,
and a task type. Findings are reported rather than raised — a partially valid plan is still
useful for review, and the confidence label carries the verdict.

### Response parsing

The decomposition response is parsed as JSON, and if that fails, the first JSON array in the text
is extracted. Models frequently narrate around the array; the array is what matters, and failing
the whole invocation over a preamble would be a needless outage.

## State Definition

| Field group | Types | Purpose |
|-------------|-------|---------|
| raw_query, normalized_query, query_id | str | Safe requirement processing and correlation |
| team_velocity_points, max_task_points, max_tasks | int | Planning parameters |
| tasks, task_count, total_story_points | str, int, int | Estimated work items, JSON-serialized |
| oversized_tasks | str | Titles exceeding the per-item ceiling |
| sprint_plan, sprint_count, overflow_warning | str, int, bool | Packing result |
| validation_passed, validation_issues | bool, str | Structural verdict and findings |
| answer, confidence | str | Rendered plan and its confidence label |
| synthesis_mode | str | Always evidence_only — the report is computed, not generated |
| formatted_output | str | Canonical JSON response exposed by AgentBaseGraph |
| error_message, recoverable_error_code | str or None | Short-circuit and degradation control |
| input_error_message, input_error_guidance | str or None, list[str] | User-facing input guidance |

**State Constraints:**

- Flat TypedDict only; collections contain primitive or JSON-safe values.
- No API keys, bearer tokens, credentials, LLM clients, or Pydantic objects.
- InvocationContext is reconstructed with InvocationContext.from_state(state) inside a node if needed.

## Framework Utilization

- [x] InvocationContext and caller trust at the graph boundary.
- [x] Framework S-1 required_trust_level on every domain node.
- [x] Framework final S-2/S-3 gates remain non-overridden.
- [x] PreProcessNode extends S-2 through _extra_security_gate_input to block prompt injection.
- [x] MainNode extends S-2 through _extra_security_gate_input for requirement PII masking.
- [x] PostProcessNode extends S-3 through _extra_security_gate_output for the rendered plan.
- [x] Every execute method emits a domain trace event on success, failure, or short circuit.
- [x] AgentRegistry and standalone server both load config/config.yaml separately from agent.yaml.
- [x] Marketplace and standalone entrypoints bind secrets per invocation and degrade safely when Azure is unavailable.

## Security and Runtime Decisions

- Minimum caller trust: INTERNAL. The agent plans a team's delivery work; it is not a
  public-facing capability.
- External bearer credentials map only to VERIFIED_EXTERNAL, so an external caller cannot reach
  these nodes at all. A separately configured STG runner token is required for INTERNAL.
- Bearers are compared in constant time and invalid configured credentials return HTTP 401.
- memory_enabled and hitl.enabled are false, so no checkpointer is created.
- A requirement below the minimum length is rejected with guidance: it is a topic, not a
  specification, and decomposing it would invent scope.
- Personal data in the rendered plan is masked rather than raised on. A requirement routinely
  names the people who asked for it, and those names flow into task descriptions; raising would
  turn a recoverable filter decision into a failed invocation. The shared name detector favors
  recall and flags any run of Title Case words, so planning vocabulary such as "Acceptance
  Criteria" matches as readily as a real name — a finding is treated as non-personal only when
  every one of its capitalised tokens is planning vocabulary.
- A credential in the plan blocks the output: unlike a personal name, a credential has no place
  in a delivery plan under any reading.
- Every detection pattern uses bounded quantifiers, so a hostile input cannot force
  quadratic-time backtracking.
- Azure client creation is lazy, so missing provider configuration does not break startup. At
  invocation time, decomposition failure returns `decomposition_unavailable` and PostProcessNode
  renders actionable guidance rather than failing the invocation.

## EU AI Act Art.13 Design-Time Evidence

The proposal declares this planning capability outside Annex III scope.
The README documents intended purpose and limitations, and response metadata exposes task count,
sprint count, oversize flags, validation findings, and confidence for user transparency.

## Import Isolation Confirmation

- [x] Source imports framework and shared layers, never the Level 0 platform SDK.
- [x] Agent-local imports use the src package path.

## Design Decision Record

| Decision | Options | Chosen | Rationale |
|----------|---------|--------|-----------|
| L1 base | AgentBaseGraph / AutonomousBaseGraph | AgentBaseGraph | Fixed decompose, estimate, plan stages need no autonomous loop |
| Composition | Standalone / subgraph / remote | Standalone | One independently deployable technical capability |
| Caller trust | VERIFIED_EXTERNAL / INTERNAL | INTERNAL | Plans a team's delivery work, not a public capability |
| HITL | Enabled / disabled | Disabled | The plan is advisory and commits nothing |
| Memory | Enabled / disabled | Disabled | Each decomposition request is self-contained |
| LLM scope | Decompose only / also estimate and plan | Decompose only | Generated estimates would move between runs and could not be tracked against |
| LLM node placement | MainNode / PostProcessNode | MainNode | Decomposition is the generated step; the report is computed |
| Oversized task handling | Pack anyway / own sprint plus explicit flag | Own sprint and flag | Burying an unpackable item inside a sprint is how overruns go unnoticed |
| Unknown task type | Reject / default size | Default | A plan with one mis-sized item beats no plan |
| Malformed LLM response | Fail / extract the JSON array | Extract | Models narrate around the array; a preamble is not an outage |
| Validation failures | Raise / report with confidence | Report | A partially valid plan is still useful for review |
| Personal data in the plan | Raise / mask | Mask | Requirements name their requesters; masking keeps the plan usable |
