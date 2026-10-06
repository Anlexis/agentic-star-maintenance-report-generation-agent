# Test Specification — MFG-C2-014 Maintenance Report Generator

## 1. Test Strategy

- **Agent:** MFG-C2-014 — Maintenance Report Generator (Cat 2, document-generation
  pattern, two-layer nested graph: outer `AgentBaseGraph` backbone + inner
  `DomainWorkflowGraph` `BaseGraph`).
- **Coverage target:** ≥ 90% of `src/nodes/` + `src/graph/` branches.
- **Test types:** Unit (per node + graph wiring + caller contract + output
  boundary + config plumbing) · Proof-of-Boundary (framework
  security/serialization contracts) · End-to-end (`POST /invoke` through the
  real ASGI app, and full `Graph().invoke()`).
- **Framework provisioning:** `framework` (agenticstar-agentcore) is supplied by
  CI. Tests import the REAL modules; there are no stub nodes.
- **Audit events:** `emit_trace_event` is patched at the node module level in unit
  tests to avoid audit-backend calls, never via a `sys.modules` stub (which would
  break the real `shared` package the framework loads at import time).

### Test file map

| File | Scope |
|------|-------|
| `tests/unit/test_nodes.py` | All 5 domain nodes + 2 backbone gate nodes (pre/post) + outer & inner graph wiring + the trust gate |
| `tests/unit/test_caller_contract.py` | The caller-data contract: channel selection, transit integrity, instruction-override screen (both directions), contact-identifier screen, field bounds, no-echo |
| `tests/unit/test_output_gate.py` | The external-output boundary: nested credential scan, DETECTOR PARITY with the framework recognizer, identifier integrity (read from the OUTER-state key), verbatim redaction, size cap, agent-class gate hook, and BLOCK CONTAINMENT (every report-bearing field cleared, truthy replacement, key-set inventory guard) |
| `tests/unit/test_config_plumbing.py` | Declared runtime values arrive (outer + inner graph); the finite/bounded config validator |
| `tests/unit/test_get_output.py` | The outer `get_output()` envelope: post-gate values surfaced on success; on EVERY non-success status `result` is None and the base envelope's `formatted_output or result` fallback is re-resolved, so a falsy gate output cannot degrade into the refused report |
| `tests/unit/test_main_node.py` | Standalone `MainNode` — `execute()` contract |
| `tests/unit/test_service.py` | Service facade self-contained envelope |
| `tests/unit/test_framework_compliance_tc06_tc07.py` | TC-06/TC-07 — framework input/output gates are non-bypassable |
| `tests/proof_of_boundary/test_invoke_e2e.py` | End-to-end business behaviour through the real ASGI `POST /invoke` (Bearer auth), including OUTPUT-GATE CONTAINMENT with a clean-path control |
| `tests/proof_of_boundary/test_pb_invoke_order.py` | PB-6 per-node + backbone invoke order (VERIFIED_EXTERNAL) + trust gate + payload alignment |
| `tests/proof_of_boundary/test_import_isolation.py` | PB-4 platform-SDK import isolation (AST scan) |
| `tests/proof_of_boundary/test_state_safety.py` | PB-2/PB-5 State msgpack/credential safety (AST scan) |
| `tests/proof_of_boundary/test_pb7_hitl_interrupt_propagation.py` | PB-7 HITL interrupt-propagation (skip stub — no cross-boundary HITL) |

### Canonical valid payload (PB-6 `_VALID_PAYLOAD`)

The 労働安全衛生法 Art 45 record-required inspection used by the backbone invoke
test and by `deploy/invoke_payload.json` (the two MUST stay identical — asserted
by `test_invoke_payload_matches_pb6`):

```json
{
  "equipment_id": "MFG-EQ-CRANE-014",
  "equipment_class": "crane",
  "inspection_date": "2026-07-12",
  "inspector": "K. Tanaka",
  "findings": [
    {"component": "hoist wire rope", "observation": "Visible strand fraying near the drum flange ...", "severity": "high"},
    {"component": "upper-travel limit switch", "observation": "Delayed actuation under rated load ...", "severity": "medium"}
  ],
  "fault_codes": ["E-204", "E-118"],
  "corrective_actions": ["Replace the hoist wire rope assembly ...", "Recalibrate the upper-travel limit switch ..."],
  "next_maintenance_date": "2026-10-12"
}
```

Record requirement: equipment is a crane (特定機械等 / specified machinery) **AND**
max finding severity is `high` **AND** 2 fault codes reported ⇒
`safety_record_required = True` under 労働安全衛生法 Article 45.

This payload rides the `user_input` fallback channel and is deliberately
mask-safe (lowercase component labels, no consecutive Title-Case words). The
structured `input_context.inspection` channel — the recommended one — is
exercised end-to-end in `test_invoke_e2e.py`, including the proof that
Title-Case labels survive it intact and are refused (not repaired) on the
fallback channel.

## 2. Framework Compliance Tests (Mandatory)

| TC-ID | Test | Expected Result | Where |
|-------|------|----------------|-------|
| TC-01 | State contract: flat `TypedDict`, domain fields `NotRequired`, no Pydantic/dataclass | AST scan: 0 violations | `test_state_safety.py` |
| TC-02 | Invalid/empty/non-JSON input rejected at PreProcessNode | `status=error`, error_log populated | `TestPreProcessNode`, `TestFieldBounds` |
| TC-03 | No JWT/credential in State | credential-name scan: 0 violations | `test_state_safety.py` |
| TC-04 | `execute(self, state)` contract | Signature `(self, state)` on every node | `test_execute_signature_is_state_first`, `test_main_node.py` |
| TC-05 | Audit event emitted inside each node `execute()` | ≥1 domain event per node | asserted implicitly by the module-level patches; events named in docs/02 |
| TC-06 | Framework input gate cannot be overridden | subclass override raises TypeError | `test_framework_compliance_tc06_tc07.py` |
| TC-07 | Framework output gate cannot be overridden | subclass override raises TypeError | `test_framework_compliance_tc06_tc07.py` |
| TC-08 | `required_trust_level` enforced in `__call__` before `execute()` | ANONYMOUS caller → refused; VERIFIED_EXTERNAL → admitted | `TestTrustGate` |
| TC-08a | Outer `PreProcessNode` = VERIFIED_EXTERNAL; inner nodes + post_process = ANONYMOUS | trust levels asserted per node | `test_trust_level_*` |
| TC-11 | Output gate on post_process | credential pattern → withheld + `status=error`; clean → pass | `TestPostProcessNode`, `test_output_gate.py` |

## 3. Proof-of-Boundary Tests (Mandatory)

| PB-ID | Boundary | Test | Expected Result | Where |
|-------|----------|------|----------------|-------|
| PB-2 | State serialization | AST scan of `src/schemas/state.py` | primitives only; no Pydantic/dataclass | `test_state_safety.py` |
| PB-4 | Import isolation | AST scan of `src/` | 0 platform-internal SDK imports | `test_import_isolation.py` |
| PB-5 | Checkpoint safety | no credential-named fields / prohibited types in State | inspection pass | `test_state_safety.py` |
| PB-6 | Invoke execution order (per node) | `__call__`: node_start → trust gate → input gate → `execute()` → output gate → node_complete | order verified for every `src/nodes/` class | `TestInvokeOrder` |
| PB-6b | Backbone invoke order | full `Graph().invoke(_VALID_PAYLOAD, ctx=VERIFIED_EXTERNAL)` | `status=success`; node_history = `[Initialize, PreProcess, MaintenanceReportGraphNode, PostProcess, Finalize]` | `TestBackboneInvokeOrder` |
| PB-6c | Real external caller | `InvocationContext(caller_trust_level=VERIFIED_EXTERNAL)` — **never** `for_internal()` | inner ANONYMOUS nodes accept the passthrough trust; SUCCESS end-to-end | `TestBackboneInvokeOrder` |
| PB-6d | Payload alignment | `deploy/invoke_payload.json["input"] == _VALID_PAYLOAD` | the deployment evidence invoke exercises the PB-6 payload | `test_invoke_payload_matches_pb6` |
| PB-7 | HITL interrupt propagation | skip stub — `propagate_hitl=False`, no cross-boundary interrupt() checkpoint | skipped with reason (real assertion when HITL wired) | `test_pb7_hitl_interrupt_propagation.py` |
| PB-E2E | The deployed entry point | `POST /invoke` through the real ASGI app, Bearer auth | complete report from a structured record; Title-Case labels intact; fail-closed rejections; 401 without the token; oversized context → 413 | `test_invoke_e2e.py` |

## 4. Business Logic Tests

| BL-ID | Test | Input | Expected Result | Where |
|-------|------|-------|----------------|-------|
| BL-01 | Happy-path report generation | `_VALID_PAYLOAD` / structured record | 7-section report; `EQUIPMENT MAINTENANCE REPORT` + equipment_id present | `TestBackboneInvokeOrder`, `TestInvokeEndToEnd` |
| BL-02 | Severity-label normalisation | `["crit","major","moderate","minor"]` | `["critical","high","medium","low"]` (canonicalised); `max_severity=critical` | `test_severity_aliases_are_normalised` |
| BL-03 | Unknown severity at the caller contract | `severity="urgent"` | refused, field named (never downgraded) | `test_unknown_severity_is_refused_not_downgraded` |
| BL-04 | Criticality classification | severity / fault-code matrix | critical / high / medium / low per thresholds | `TestParseInspectionDataNode` |
| BL-05 | Art 45 record required — specified machinery | equipment_class = crane/boiler/特定機械, clean inspection | `safety_record_required=True` (is_specified_machinery) | `test_rule_set_is_preserved_from_the_retired_node` |
| BL-06 | Art 45 record required — high severity | general equipment, max finding severity critical | `safety_record_required=True` (severity_threshold_met) | `test_rule_set_is_preserved_from_the_retired_node` |
| BL-07 | Art 45 record required — fault codes | general equipment, 1 fault code | `safety_record_required=True` (fault_code_threshold_met) | `test_rule_set_is_preserved_from_the_retired_node` |
| BL-08 | Art 45 record NOT required | general equipment, low/medium severity, no fault codes | `safety_record_required=False`; report states `Formal Record Required:    NO` | `test_not_required_for_clean_general_equipment`, `test_general_equipment_clean_findings_not_reportable` |
| BL-09 | Report section assembly | 7 rendered sections | all 7 headers in the body; the body carries **no** determination — `REGULATORY COMPLIANCE NOTE` is appended by the output boundary | `TestOutputFormatNode`, `test_gate_renders_the_note_the_report_body_does_not_carry` |
| BL-10 | Graph key coupling | inner `get_output` ↔ outer `merge_output` | 3 coupled keys mapped; `compliance_flags` / `safety_record_required` deliberately NOT mapped (boundary-owned); `merge_output` returns changed keys only | `TestOuterGraphComposition`, `TestInnerDomainGraph` |
| BL-11 | Identifier fidelity | letter-digit / underscore / pure-numeric codes | byte-identical in the rendered report; altered code ⇒ withheld | `TestIdentifierIntegrity`, `test_precision_identifiers_are_byte_identical` |
| BL-13 | Output-gate containment | a caller `input` equal to their own equipment ID (redaction erases the identifier, integrity refuses) | `status=error` with `PostProcessNode` in `node_history`; `result` / `maintenance_report` / `report_sections` / `compliance_flags` all None; no report body, fault code, inspector name, observation text, traceback or source path anywhere in the envelope; clean-path control on the same record still returns the real report | `TestBlockedOutputIsContained`, `TestOutputGateContainment`, `TestErrorEnvelopeContainment` |
| BL-15 | Art 45 determination is not bypassable | the inner pipeline rewired with any `compliance*` node registration dropped | `status=success` with the correct `YES — retain for 3 years`; never the fabricated `Formal Record Required:    NO` / `Determination Basis:       N/A`. With no readable record the response is withheld instead of defaulting to NO | `TestStatutoryDeterminationSurvivesTopologyDrift`, `test_no_record_withholds_rather_than_defaulting_to_NO`, `TestNoComplianceCheckNodeRemains` |
| BL-14 | Detector parity | every shape `framework.security.detect_credentials` refuses (stripe / openai / JWT / AKIA / bearer / connection string) | the domain gate blocks each one; ordinary maintenance text is not flagged | `TestDetectorParityWithFramework` |
| BL-12 | Structured-channel integrity | Title-Case labels via `input_context` | labels reach the report intact; the same record on the fallback channel is refused as altered in transit | `TestStructuredChannel`, `TestInvokeEndToEnd` |

### Negative / boundary cases

| Case | Node | Expected |
|------|------|----------|
| empty `user_input`, no structured record | PreProcessNode | `status=error`, "empty" |
| invalid JSON on the fallback channel | PreProcessNode | `status=error`, "JSON" |
| record not an object | PreProcessNode | `status=error`, "object" |
| missing required fields | PreProcessNode | `status=error`, fields named |
| identifier / date / severity / cap violations | PreProcessNode | `status=error`, field named, value never echoed |
| instruction-override content (tokens, phrases, keys, escaped) | PreProcessNode | `status=error`, nothing carried forward |
| contact identifier in free text | PreProcessNode | `status=error`, refused not masked |
| mask sentinel on the fallback channel | PreProcessNode | `status=error`, structured channel named |
| missing `inspection_data` | Parse / Generate / Compliance | `status=error` |
| identifier erased by verbatim redaction | PostProcessNode | `status=error`, report withheld and CONTAINED (not shipped with an unidentifiable subject) |
| missing `report_sections` | OutputFormatNode | `status=error` |
| empty `maintenance_report` | PostProcessNode | fallback message, `status=success` |
| credential anywhere in either output representation | PostProcessNode | withheld, `status=error` |
| identifier altered between record and report | PostProcessNode | withheld, `status=error` |
| raw request embedded verbatim in the report | PostProcessNode | `[REDACTED]`, `status=success` |
| output above the size cap | PostProcessNode | truncated with notice |

## 5. Test Execution Summary

- Execution: `pytest tests/` under the real framework wheel (`agenticstar-agentcore` as pinned by central CI).
- Total: 225 tests — **224 passed, 1 skipped** (PB-7 skip stub, by design).
- Repository gate scripts (manifest schema, forbidden strings, credentials,
  dependency pinning, trust level, stub-test check, category consistency) — all
  PASS.
- Coverage: node + graph modules exercised on both success and error paths,
  through direct `execute()` drives, full graph invokes, and the real ASGI
  entry point.
