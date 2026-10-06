"""AgentCore Platform v1.0"""

# State must be a flat TypedDict — never Pydantic BaseModel.
# LangGraph checkpoints use msgpack serialization; Pydantic objects
# cause silent corruption.  Extend AgentState with agent-specific
# fields only.  Do NOT add credentials, secrets, or Pydantic models.
#
# MFG-C2-014 — Maintenance Report Generator
# Two-layer nested Cat 2 graph: outer backbone (AgentBaseGraph) + inner
# domain workflow (BaseGraph).  Fields below cover both layers.
#
# All dict/list-valued fields are stored as JSON-serialized
# Optional[str].  Use to_json() / from_json() helpers below at every
# producer and consumer node — one contract end-to-end.  Never type a
# dict/list field as a bare dict/list; that causes msgpack
# serialization failures.

import json
from typing import Any, NotRequired, Optional

from framework.schemas.agent_state import AgentState


def to_json(value: Any) -> Optional[str]:
    """Serialize a value to a JSON string for State storage."""
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


def from_json(value: Optional[str], default: Any = None) -> Any:
    """Deserialize a JSON string from State storage."""
    if value is None:
        return default
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


class State(AgentState):
    """Flat TypedDict for MFG-C2-014.

    All shared fields (user_input, status, session_id, node_history,
    error_log, hitl_*, etc.) are inherited from AgentState.

    Dict/list fields use JSON-serialized Optional[str].
    formatted_output is NOT re-declared here — it is inherited from AgentState.
    """

    # ------------------------------------------------------------------
    # Outer layer — set by PreProcessNode (pre_process backbone, trust gate)
    # ------------------------------------------------------------------

    # Inert request-summary string (identifier text only). The string
    # channel is PII-masked at every node boundary, so nothing label-shaped
    # travels on it; the record itself rides in inspection_payload below.
    validated_input: NotRequired[Optional[str]]

    # JSON-serialised validated inspection record (stored as str). Produced
    # by PreProcessNode; bridged into the inner graph's input_context by
    # MaintenanceReportGraphNode.extract_input() (src/graph/context_bridge.py)
    # and consumed by inner InputValidateNode.
    inspection_payload: NotRequired[Optional[str]]

    # JSON-serialised channel/request metadata dict (stored as str).
    # Shape: {"source": str, "channel": str, "equipment_id": str}
    enriched_context: NotRequired[Optional[str]]

    # ------------------------------------------------------------------
    # Inner layer — domain nodes (DomainWorkflowGraph)
    # ------------------------------------------------------------------

    # JSON-serialised parsed inspection payload (stored as str).
    # Shape: {equipment_id, equipment_class, inspection_date,
    #   findings (list[dict]), fault_codes (list), corrective_actions (list),
    #   next_maintenance_date (str|None), inspector (str), finding_count (int),
    #   max_severity (str), criticality (str), safety_reportable (bool)}
    inspection_data: NotRequired[Optional[str]]

    # JSON-serialised report section dict (stored as str).
    # Keys match the 7 maintenance-report sections:
    #   equipment_summary, inspection_findings, fault_codes,
    #   corrective_actions, next_maintenance_schedule,
    #   compliance_status, sign_off
    # Each value is the rendered text for that section.
    report_sections: NotRequired[Optional[str]]

    # Maintenance report BODY (plain text, audit-ready) assembled by inner
    # OutputFormatNode from report_sections. PostProcessNode appends the
    # REGULATORY COMPLIANCE NOTE at the output boundary.
    maintenance_report: NotRequired[Optional[str]]

    # ------------------------------------------------------------------
    # Outer layer — set by PostProcessNode (post_process backbone, output gate)
    # ------------------------------------------------------------------

    # Primary result surfaced to the caller.
    # Set to the same content as the gated report after the output gate passes.
    # formatted_output (from AgentState) is also set by PostProcessNode.
    result: NotRequired[Optional[str]]

    # JSON-serialised 労働安全衛生法 Art 45 determination (stored as str),
    # produced by PostProcessNode at the output boundary — NOT by a node inside
    # the domain pipeline (the platform architecture rules name compliance_check_node an
    # anti-pattern; a determination held in graph topology is bypassable).
    # Shape: {safety_record_required: bool, reason: str, is_specified_machinery:
    #   bool, severity_threshold_met: bool, fault_code_threshold_met: bool,
    #   max_severity: str, fault_code_count: int, retention_years: int,
    #   regulatory_basis: str}
    compliance_flags: NotRequired[Optional[str]]

    # True if a formal 定期自主検査記録 (periodic self-inspection record) is
    # legally required under 労働安全衛生法 Article 45. Written by PostProcessNode.
    safety_record_required: NotRequired[Optional[bool]]

    # ------------------------------------------------------------------
    # Tracing / audit — framework-managed; do NOT write from node code
    # ------------------------------------------------------------------

    trace_id: NotRequired[Optional[str]]
    correlation_id: NotRequired[Optional[str]]
    # node_history inherited from AgentState
