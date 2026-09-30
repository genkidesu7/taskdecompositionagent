# Test Specification

## Test Strategy

- Coverage target: 80%
- Test types: unit, framework compliance, proof-of-boundary, integration, and local STG invocation.
- Unit tests may call execute directly for isolated business logic.
- Security-boundary tests call node(state) through FunctionNode.__call__.
- No live Azure OpenAI is required: the provider is exercised through a recording double.

## Framework Compliance Tests

| TC-ID | Test | Expected Result |
|-------|------|-----------------|
| TC-01 | State contract is a flat TypedDict | No Pydantic, dataclass, secret, or runtime client fields |
| TC-02 | Invalid or unsafe input is rejected or sanitized | Safe failure, markup stripped, or blocked injection attempt |
| TC-03 | No credential in State | Credential scan reports zero violations |
| TC-04 | InvocationContext construction inside nodes | Only from_state is allowed; authenticated server adapter is exempt |
| TC-05 | No duplicate lifecycle trace events | execute methods do not emit node_start, node_complete, or node_error |
| TC-06 | FunctionNode input security gate is final | Override attempt raises TypeError |
| TC-07 | FunctionNode output security gate is final | Override attempt raises TypeError |
| TC-08 | required_trust_level is enforced through node(state) | Denial happens before execute; every domain node requires INTERNAL |
| TC-09 | PreProcessNode and MainNode extra S-2 hooks | Prompt injection blocked; PII in the requirement masked before the provider |
| TC-10 | PostProcessNode extra S-3 hook | Credential blocks the output; personal data is masked, not raised |
| TC-11 | Domain tracing | Each execute path emits a domain event |

## Proof-of-Boundary Tests

| PB-ID | Boundary | Expected Result |
|-------|----------|-----------------|
| PB-1 | BaseNode to trace emitter | Domain and lifecycle trace evidence exists |
| PB-2 | State serialization | Only msgpack-safe annotated fields |
| PB-3 | Template to external service | Injected LLM double exercises the provider boundary |
| PB-4 | Import isolation | No Level 0 imports |
| PB-5 | Checkpoint safety, conditional | Auto-waived — checkpointing disabled |
| PB-6 | Invoke order | S-1 → node_start → S-2 → execute → S-3 → node_complete |
| PB-7 | HITL interrupt propagation, conditional | Auto-waived — non-HITL |

PB-1 through PB-4 and PB-6 are mandatory. PB-5 applies only when memory or HITL enables
checkpointing and the framework provides both ingress hooks. PB-7 applies only when
config/config.yaml sets hitl.enabled to true.

## Business Logic Tests

| TC-ID | Test | Input | Expected Result |
|-------|------|-------|-----------------|
| BL-01 | Requirement validation | markup-only, over-length, and two-word requirements | Safe rejection or actionable guidance |
| BL-02 | Decomposition | injected LLM returning a task array | Tasks parsed, counted, and estimated |
| BL-03 | Response parsing | JSON wrapped in prose, and unparseable prose | Array extracted; unparseable degrades recoverably |
| BL-04 | Estimation | each task type, plus an unrecognised type | Correct points and size label; default for unknown |
| BL-05 | Sprint packing | within velocity, overflowing, and a task larger than a sprint | One sprint, several sprints, and an isolated outlier sprint |
| BL-06 | Validation and oversize | task missing fields; task above the per-item ceiling | Issue reported; title listed under oversized_tasks |
| BL-07 | Plan rendering | computed plan | Report figures match state exactly |
| BL-08 | Confidence tiers | valid, oversized, and failing validation | High, medium, and low per policy |
| BL-09 | Output safety | plan containing a credential, and one containing an email | Credential blocks; email masked with formatted_output kept in sync |
| BL-10 | Azure wiring | Azure secrets present | Request-scoped client built from ctx.secrets, no process-global client |
| BL-11 | Missing secrets | no Azure secrets bound | Terminal guidance rather than a failed invocation |
| BL-12 | Reproducibility | same requirement decomposed twice | Identical estimates and identical sprint allocation |
| BL-13 | Bearer mapping | external, runner, wrong, and absent bearer | VERIFIED_EXTERNAL, INTERNAL, HTTP 401, and ANONYMOUS respectively |
| BL-14 | Marketplace shaping | same state with and without conversation_history | Marketplace receives the plan text, the API keeps canonical JSON |

## Test Execution Summary

- Execution date: 2026-09-30
- Framework: agenticstar-agentcore 1.0.3 wheel (not a CI import shim)
- Full suite: 84 passed, 0 failed, 3 skipped
- Local gates: forbidden strings, manifest schema, trust level, audit trace, dependency pinning,
  stub tests, credential scan, OSS license, and Cat consistency all PASS
- Lint and types: ruff format, ruff check, and mypy strict on src/ and cli.py all clean

The three expected skips are PB-5 (checkpointing disabled) and both PB-7 cases (non-HITL).

Two defects were found and fixed during this cycle. The standalone adapter mapped a valid
external bearer to INTERNAL, which would have granted internal privileges to any external caller
holding the public token; BL-13 covers the corrected mapping. The Marketplace entrypoint imported
a graph class name that no longer existed after the port, which mypy caught before deployment.
