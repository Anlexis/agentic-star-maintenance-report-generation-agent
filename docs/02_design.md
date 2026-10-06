# Template Design Specification — MFG-C2-014 Maintenance Report Generator

## Position in AgentCore Architecture

| Role | Value |
|------|-------|
| Agent Class | MaintenanceReportGeneratorAgent |
| L1 Base (framework base class) | AgentBaseGraph — direct framework inheritance |
| Pattern | Cat 2 — document-generation pipeline (two-layer nested workflow) |

Three-layer separation:

- **State**: flat TypedDict composition (no Pydantic — msgpack incompatible); all
  dict/list-valued fields are JSON-serialised strings
- **Node**: framework inheritance (Template Method: `execute(self, state) -> dict`
  override only)
- **Graph**: composition (`register_nodes()` for node substitution; the Cat 2
  nesting via `GraphNode`)

## Domain Context

Equipment maintenance report generator for Japanese manufacturing operators.
Generates structured, audit-ready maintenance reports from equipment inspection
findings submitted by maintenance technicians.

**Regulatory basis**: 労働安全衛生法 (Industrial Safety and Health Act) Article 45 —
operators of 特定機械等 (specified machinery) must perform periodic self-inspection
(定期自主検査) and retain the records for 3 years. Reports also serve as J-SOX
internal-control evidence for maintenance work-order closure.

The pipeline is deterministic: the report is synthesised from the validated
inspection record with fixed templates, so the same submission always yields the
same report. No model is called and no external service is contacted. The `llm`
block in `config/config.yaml` and `prompts/maintenance_report.j2` are reserved for
a live-model build; they are validated and forwarded to the inner graph but not
consumed by the bundled build.

## Architecture Overview

### Backbone (outer AgentBaseGraph — fixed 5-node pipeline)

```
START → initialize → pre_process → main(GraphNode) → post_process → finalize → END
                                         ↓ (retry, max 3)
                                       pre_process
```

### Inner Domain Workflow (DomainWorkflowGraph — linear 4-node pipeline)

```
START → input_validate → parse_inspection_data → generate_report_sections
          → output_format → END
```

The 労働安全衛生法 Art 45 determination is **not** a node here. It is made and
rendered at the output boundary (PostProcessNode layer 0) — see §Output boundary.

### Node Configuration

| Node | Class | File | Trust | Responsibility | Input Keys | Output Keys |
|------|-------|------|-------|---------------|------------|-------------|
| initialize | InitializeNode | framework | — | session init | — | session_id, schema_version |
| pre_process | PreProcessNode | src/nodes/pre_process_node.py | VERIFIED_EXTERNAL | trust gate + the caller-data contract | user_input, input_context | inspection_payload, validated_input, enriched_context |
| main | MaintenanceReportGraphNode | src/graph/graph.py | — | delegates to DomainWorkflowGraph; bridges the record | inspection_payload, validated_input | maintenance_report, report_sections |
| post_process | PostProcessNode | src/nodes/post_process_node.py | ANONYMOUS | external-output boundary (5 layers, below) + the 労働安全衛生法 Art 45 determination | maintenance_report, report_sections, inspection_payload | formatted_output, result, compliance_flags, safety_record_required (+ cleared report fields on a block) |
| finalize | FinalizeNode | framework | — | response metadata | — | response_metadata, total_time_ms |
| input_validate (inner) | InputValidateNode | src/nodes/input_validate_node.py | ANONYMOUS | domain normalisation (severity canonicalisation, derived counts) | input_context (bridged record) | inspection_data |
| parse_inspection_data (inner) | ParseInspectionDataNode | src/nodes/parse_inspection_data_node.py | ANONYMOUS | criticality classification + Art 45 pre-check | inspection_data | inspection_data (enriched) |
| generate_report_sections (inner) | GenerateReportSectionsNode | src/nodes/generate_report_sections_node.py | ANONYMOUS | generate 7 maintenance-report sections (deterministic) | inspection_data | report_sections |
| output_format (inner) | OutputFormatNode | src/nodes/output_format_node.py | ANONYMOUS | assemble the report body (no compliance note) | report_sections | maintenance_report, result |

### The caller-data contract (PreProcessNode)

Caller data arrives on two channels, both validated by the same rules:

- **`input_context` — the structured channel (recommended).** Fields:
  `inspection` (the inspection record, JSON object) and `channel` (inert slug,
  `[a-z0-9_]{1,32}`). The transport adapter caps the serialised context at
  256 KB.
- **`input` — the fallback.** A JSON object with the same inspection-record
  shape, for direct invocation and test harnesses. It is lossy by construction:
  the platform masks two-word Title-Case runs on the string channel before the
  node runs, and a component or inspector label has exactly that shape. A
  fallback record carrying the mask sentinel is **refused, never repaired** — a
  masked label would name the wrong part in the finished report.

Field rules (fail CLOSED; a rejection names the FIELD and never echoes the
value):

| Field | Rule |
|-------|------|
| `equipment_id` | identifier alphabet `[A-Za-z0-9_-]{1,64}` — rendered verbatim, re-checked at the output boundary |
| `equipment_class` | 32 chars max of the plain-text alphabet (ASCII slug or Japanese label such as 特定機械) |
| `inspection_date`, `next_maintenance_date` | ISO-8601 calendar date, shape-checked then parsed |
| `inspector` | plain-text label ≤ 80 chars; contact identifiers refused |
| `findings` | 1–100 records; each `{component ≤ 80, observation ≤ 500, severity}` |
| `findings[].severity` | closed vocabulary (critical/high/medium/low + aliases) — an unknown label is refused, not downgraded, because it drives the compliance determination |
| `fault_codes` | 0–50 entries, each `[A-Za-z0-9_-]{1,32}` — rendered verbatim, re-checked at the boundary |
| `corrective_actions` | 0–50 entries, each plain text ≤ 500 chars |

The plain-text alphabet permits letters, digits, common technical punctuation
and the Japanese script ranges; it excludes every control character (including
newline and tab) and markup characters, so a label or observation can neither
carry markup nor break the one-line-per-entry structure of the report it is
rendered into. Values are checked as supplied, before whitespace normalisation
— repairing first would silently accept caller-controlled line structure.

**Instruction-override screen.** Runs post-parse, depth-first over every string
in the payload — dictionary keys included — and refuses on the first hit. It
catches chat-template control tokens as a class (`<|…|>`, `[INST]`, `<<SYS>>`),
which have no legitimate reading in an inspection record, plus directive
phrases anchored on the whole imperative shape (override verb + prompt noun).
The screen is deliberately narrow: genuine maintenance prose borrows the same
verbs ("Override the safety interlock and re-test", "Ignore the previous
reading when the sensor is faulty"), so verb-only or substring matching would
refuse real inspections. Both directions are pinned in
`tests/unit/test_caller_contract.py`. The platform's input gate also refuses
high-confidence payloads on the string channel, but the template does not
depend on it: the screen lives in `execute()` and is proven by driving
`execute()` directly.

**Contact-identifier screen.** `detect_pii` runs on every free-text field
(observations, corrective actions, inspector), and a finding from the
high-precision classes (email, phone numbers, card/national-identifier shapes)
is refused, never masked. Two detector classes are deliberately excluded, with
the trade-off documented here: the personal-name heuristics match any two
Title-Case words — which is what a component label is ("Hoist Wire Rope") and
what the sign-off block requires of the inspector field — and the 12-digit
national-identifier shape is also the shape of a serial or article number.

**Caller numerics.** The inspection-record schema carries no caller-controlled
numeric field (dates, severities and codes are strings; counts are derived
server-side). String fields are type-locked: a number where a string is
expected is refused, not coerced. Declared configuration numerics go through
`_config_number` (finite + bounded; NaN/Infinity/bools are never forwarded) —
see `tests/unit/test_config_plumbing.py`.

### The outer→inner record hand-off (context bridge)

The framework does not forward the outer `input_context` into a nested graph's
invoke, and the string channel (`user_input` / `validated_input`) is PII-masked
at every node boundary — so the validated record crosses the layer boundary
out-of-band: `MaintenanceReportGraphNode.extract_input()` stashes it in a
ContextVar (`src/graph/context_bridge.py`) and the inner graph's
`_extra_initial_state()` seeds it onto the inner state's `input_context`,
where `InputValidateNode` reads it. Only an inert request summary (identifier
text) travels as the inner graph's `user_input`. The hand-off is proven
end-to-end in `tests/proof_of_boundary/test_invoke_e2e.py`: a two-word
Title-Case component label and the inspector's name reach the rendered report
byte-identical.

### Data Flow

```
input_context.inspection (structured record)   [or user_input JSON fallback]
    │
    ▼ PreProcessNode (VERIFIED_EXTERNAL — trust gate + caller contract)
inspection_payload (validated record, JSON string)
validated_input   (inert request summary)
enriched_context  (channel metadata, JSON string)
    │
    ▼ MaintenanceReportGraphNode → context bridge → DomainWorkflowGraph
    │   InputValidateNode          → inspection_data (JSON string)
    │   ParseInspectionDataNode    → inspection_data (enriched)
    │   GenerateReportSectionsNode → report_sections (JSON string)
    │   OutputFormatNode           → maintenance_report (str), result (str)
    ▼ merge_output
maintenance_report, report_sections → outer state
    │
    ▼ PostProcessNode (ANONYMOUS — external-output boundary)
Art 45 determination → compliance_flags (JSON string), safety_record_required (bool)
formatted_output (gated report + REGULATORY COMPLIANCE NOTE), result
```

### State Definition

| Field | Type | Purpose | Producer |
|-------|------|---------|----------|
| inspection_payload | NotRequired[Optional[str]] | Validated inspection record (JSON string) | PreProcessNode |
| validated_input | NotRequired[Optional[str]] | Inert request summary (identifier text only) | PreProcessNode |
| enriched_context | NotRequired[Optional[str]] | JSON: {source, channel, equipment_id} | PreProcessNode |
| inspection_data | NotRequired[Optional[str]] | JSON: normalised + enriched inspection record | InputValidateNode / ParseInspectionDataNode |
| report_sections | NotRequired[Optional[str]] | JSON: {section_name: text, ...} × 7 sections | GenerateReportSectionsNode |
| compliance_flags | NotRequired[Optional[str]] | JSON: 労働安全衛生法 Art 45 determination | PostProcessNode (output boundary) |
| safety_record_required | NotRequired[Optional[bool]] | True if Art 45 record required | PostProcessNode (output boundary) |
| maintenance_report | NotRequired[Optional[str]] | Maintenance report body (no compliance note) | OutputFormatNode |
| result | NotRequired[Optional[str]] | Same as maintenance_report (backbone convention) | OutputFormatNode / PostProcessNode |

All dict/list-valued fields use JSON-serialised `Optional[str]`; `to_json()` /
`from_json()` helpers are defined in `src/schemas/state.py` and used at every
producer/consumer boundary — one contract end-to-end.

**Prohibited**: re-declaring `formatted_output` (inherited from AgentState),
credentials in State, Pydantic models.

### Input Payload Schema (`input_context.inspection`)

```json
{
  "equipment_id": "PUMP-4021",
  "equipment_class": "specified_machinery",
  "inspection_date": "2026-07-13",
  "findings": [
    {"component": "bearing", "observation": "abnormal vibration at 60Hz", "severity": "high"},
    {"component": "seal", "observation": "minor oil weep", "severity": "low"}
  ],
  "fault_codes": ["E-207", "W-114"],
  "corrective_actions": [
    "Replace bearing assembly",
    "Re-lubricate and re-torque seal housing"
  ],
  "next_maintenance_date": "2026-10-13",
  "inspector": "Tanaka T."
}
```

### Output Report Sections (maintenance-record format)

1. **Equipment Summary** — equipment ID, class, inspection date, criticality
2. **Inspection Findings** — per-finding component, observation, severity
3. **Fault Codes** — reported fault/error codes
4. **Corrective Actions** — steps taken / required to resolve
5. **Next Maintenance Schedule** — next scheduled maintenance date
6. **Compliance Status** — 労働安全衛生法 Art 45 record-required verdict + basis
7. **Sign-off** — inspector / approver sign-off block

### The external-output boundary (PostProcessNode)

The response carries the report in two representations — the rendered text and
the structured section/compliance data — and both cross the boundary, so every
layer below runs over both, to any nesting depth (caller-derived text rides
inside the structured sections, where a top-level-only scan sees nothing).

1. **Credential scan** — API keys, JWTs, Bearer tokens, cloud access-key IDs,
   connection strings and password/secret assignments anywhere in either
   representation withhold the response entirely (sanitised stub,
   `status=error`), with an audit event. Recognition is the **union of the
   framework's own `detect_credentials()`** and a small local set covering what
   the framework does not recognise (`password: …` assignments). The framework
   half is not optional: `FunctionNode` scans every node result with that same
   detector and *raises* on a hit, and a raise discards the node's whole return
   — including the containment in step (5) — so a shape the framework catches
   and this gate missed would be a containment bypass, not merely a narrower
   gate. Unioning them makes the two impossible to drift apart.
2. **Verbatim caller-text redaction** — the report is synthesised from
   validated fields; the raw request text or the full serialised record
   reappearing verbatim is bulk re-emission and is replaced with `[REDACTED]`.
3. **Identifier integrity** — the equipment ID and every fault code in the
   validated record must appear byte-identical in the rendered text; a report
   that fails is withheld. The record is read from **`inspection_payload`**,
   the key `PreProcessNode` writes into the OUTER state. `inspection_data` is
   the INNER graph's key and `MaintenanceReportGraphNode.merge_output()` does
   not map it outward, so reading it alone left this layer comparing against
   `{}` on every real invocation — silently disabled. Because redaction (2)
   runs first, this layer is also what refuses a report whose identifier was
   erased *by* that redaction rather than rendered wrong.
4. **Size cap** — output above 100,000 characters is truncated with an
   explicit notice, so a malformed upstream state cannot become a bulk dump.
5. **Containment on a block** — blocking is not a status flip. The framework
   envelope resolves the caller-facing value as `formatted_output or result`
   **without consulting status** (`AgentBaseGraph.get_output`), so returning
   `status=error` while leaving the output-bearing fields populated still ships
   the refused report inside the error envelope. `_withhold()` therefore
   overwrites `formatted_output`, `result`, `maintenance_report`,
   `report_sections`, `compliance_flags` and `safety_record_required`, and its `formatted_output`
   replacement is deliberately **non-empty** — a falsy stand-in (`""`, `{}`) is
   what re-opens the `or result` fallback rather than closing it. Violation
   messages name field paths and pattern names only: echoing a matched value
   would put the refused string back into this node's own result, where the
   framework scan raises and discards the clearing along with it.

**No monetary precision grid applies to this template.** It renders no
monetary aggregate — the report carries equipment identifiers, fault codes,
dates, severity labels, counts and the statutory retention period; no currency
symbol, amount or financial figure appears anywhere. A rounding grid here
would have nothing to round and would be actively harmful: a digit-grouping
grammar reads letter-digit codes as values to snap, which mangles exactly the
identifiers this domain is built from (`SKF-6205` → `SKF-6,000`; a bare
`48210` has no letters to protect it). The invariant enforced in its place is
the opposite one: **a precision identifier reaches the reader byte-identical**.
That is checked at the boundary rather than assumed, and pinned in tests
across the identifier forms this domain actually renders — letter-digit
equipment codes, underscore SKUs, pure-numeric codes — in
`tests/unit/test_output_gate.py` and end-to-end in
`tests/proof_of_boundary/test_invoke_e2e.py`.

The agent class also carries the canonical `_security_gate_output` hook, which
delegates to the same scanner (one source of truth for the pattern set) and
raises `SecurityViolationError` on a match.

### The invoke() envelope (`MaintenanceReportGeneratorAgent.get_output`)

The envelope is the second half of the output-gate contract, and it extends the
framework base rather than replacing it. `AgentBaseGraph.get_output()` resolves
its `output` key as `formatted_output or result` with no status check, so the
override re-resolves that fallback on every non-success outcome:

- `formatted_output` — what the gated `PostProcessNode` produced, so it is the
  only caller-facing value on either path (on a block, the gate's own
  content-free withholding notice).
- `result` — surfaced **ONLY when `status == SUCCESS`**. On any non-success
  outcome it is `None` and `output` is re-resolved as `formatted_output or
  None`, so an absent gate output stays absent and never degrades into the
  report the gate just refused.
- `maintenance_report`, `report_sections`, `compliance_flags`,
  `safety_record_required` — surfaced **ONLY when `status == SUCCESS`**.

Pinned directly on `get_output()` in `tests/unit/test_get_output.py` (every
non-success status, including a falsy `formatted_output`) and end-to-end on the
real `/invoke` surface in `tests/proof_of_boundary/test_invoke_e2e.py`.

## Security design

### Trust levels

- `PreProcessNode` (pre_process): `VERIFIED_EXTERNAL` — the external-facing gate
- All inner domain nodes: `ANONYMOUS` — the inner graph inherits the outer
  invocation context unchanged, and `INTERNAL` would deny a genuine external
  caller
- `PostProcessNode` (post_process): `ANONYMOUS` — the external gate is on
  pre_process

### Refusal is owned by the template

The instruction-override screen, the contact-identifier screen and every field
bound live in `PreProcessNode.execute()` and are proven by driving `execute()`
directly — the agent behaves the same wherever it runs, whether or not a
platform gate sits in front of it. Assertions are behavioural (error status,
nothing carried forward, nothing echoed), never a gate's wording.

### Audit logging

Every node's `execute()` emits at least one domain-specific
`emit_trace_event(event_name, payload, state)`: validation outcomes (reason
codes only — never the rejected value), classification results, section
generation, the compliance determination, and every output-boundary decision
(block, redaction, truncation) as its own event.

### Credential handling

No credentials in State; the manifest declares no secrets and no extras. The
standalone entry point's `INVOKE_AUTH_TOKEN` is a deployment-level caller
credential handled entirely in the adapter (`src/api/server.py`).

## Runtime configuration

`config/agent.yaml` is the flat registration manifest (identity only — no
runtime parameters). `config/config.yaml` holds every runtime parameter; the
platform registry passes it as `Graph(config=...)` and the standalone server
reads the same file, so both deployments see identical configuration:

| Key | Default | Consumer |
|-----|---------|----------|
| `max_retry` | 3 | The framework's retry routing (outer backbone) |
| `timeout_s` | 30 | Wall-clock budget for one report generation |
| `llm.system_prompt_template` | `prompts/maintenance_report.j2` | Reserved for a live-model build (validated + forwarded to the inner graph; not consumed by the bundled deterministic build) |
| `llm.temperature` | 0.1 | Reserved for a live-model build (as above) |
| `llm.max_tokens` | 3500 | Reserved for a live-model build (as above) |

Every forwarded value is validated (type, finiteness, range) in
`MaintenanceReportGraphNode._parent_config()`; invalid or absent keys are not
forwarded and the pipeline keeps its built-in behaviour. That the declared
values actually arrive — at the outer graph and across the layer boundary at
the inner graph — is pinned in `tests/unit/test_config_plumbing.py`.

## Framework Utilization

- [x] InvocationContext (session_id, trust level) via the framework entry points
- [x] Module-level `_security_gate_output()` in `post_process_node.py` — the
      shared credential scanner (walks nested structures), plus the agent-class
      hook delegating to it
- [x] `detect_pii` for the contact-identifier screen on caller free text
- [x] `emit_trace_event()` — at least one domain-specific event per node
- [x] `to_json()` / `from_json()` helpers in `src/schemas/state.py`

### Composition Pattern

- **Pattern**: Cat 2 nested two-layer — GraphNode wrapping inner BaseGraph
- **Outer graph**: `MaintenanceReportGeneratorAgent(AgentBaseGraph)` — fixed
  5-node backbone
- **Inner graph**: `DomainWorkflowGraph(BaseGraph)` — 5-node linear domain
  pipeline
- **Error propagation**: propagate (SubgraphError on inner failure; the outer
  backbone's retry routing applies)

## Import Isolation Confirmation

- [x] Template does not import the platform-internal SDK
- [x] Import targets: `framework/` and `shared/` only

## Design Decision Record

| Decision | Option A | Option B | Chosen | Rationale |
|----------|----------|----------|--------|-----------|
| Base type | AgentBaseGraph | AutonomousBaseGraph | AgentBaseGraph | Fixed sequential pipeline; no autonomous reasoning loop required |
| Composition pattern | Flat (single main node) | Nested GraphNode | Nested | 5 sequential domain steps; document-generation pattern |
| Record transport | String channel | Context channel + bridge | Context channel | The string channel is PII-masked at every node boundary; two-word Title-Case labels (components, inspectors) would be rewritten in transit |
| Art 45 compliance | Separate `compliance_check` node | Decided at the output boundary | Output boundary | A determination held by one node among many is enforced only by the topology that includes it: with the node dropped the renderer's `.get("safety_record_required", False)` default rendered "Formal Record Required: NO" and shipped it as `status=success`. Deciding it where the response is assembled removes the bypass, and an unreadable record now withholds instead of defaulting to NO |
| State dict fields | bare dict | JSON-serialised str | JSON-serialised str | msgpack serialisation safety |
| Report generation | Live model | Deterministic templates | Deterministic | Same submission ⇒ same report; audit-ready output needs no model; llm config reserved for a live-model build |
| Unknown severity label | Downgrade to low | Refuse | Refuse | The label drives the statutory determination; silent downgrade understates the finding |
| Numeric output invariant | Rounding grid | Identifier fidelity | Identifier fidelity | Nothing monetary is rendered; the domain's precision obligation is that codes are never rewritten |
