"""AgentCore Platform v1.0"""

# MFG-C2-014 — OutputFormatNode (inner domain node 4, last in DomainWorkflowGraph)
# Assembles the maintenance report BODY from report_sections.  This is the last
# inner node — it produces the maintenance_report string that the outer
# PostProcessNode gates at the external-output boundary.
#
# It does NOT render the REGULATORY COMPLIANCE NOTE.  The 労働安全衛生法 Art 45
# determination is made and rendered at the output boundary (PostProcessNode
# layer 0) so that it cannot be dropped by a change of graph topology; reading
# a compliance_flags state key here reintroduced exactly that dependency, and
# its `.get("safety_record_required", False)` default turned a missing
# determination into a rendered "Formal Record Required: NO".
#
# Inner node — ANONYMOUS trust (the outer trust gate already ran).
# Returns ONLY the state keys this node writes (partial-dict contract).

import logging
from typing import Any, ClassVar, Dict, Optional

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import from_json

logger = logging.getLogger(__name__)

# Section order for the final report.
_SECTION_ORDER = [
    "equipment_summary",
    "inspection_findings",
    "fault_codes",
    "corrective_actions",
    "next_maintenance_schedule",
    "compliance_status",
    "sign_off",
]

# Human-readable section headers.
_SECTION_HEADERS: Dict[str, str] = {
    "equipment_summary": "1. Equipment Summary",
    "inspection_findings": "2. Inspection Findings",
    "fault_codes": "3. Fault Codes",
    "corrective_actions": "4. Corrective Actions",
    "next_maintenance_schedule": "5. Next Maintenance Schedule",
    "compliance_status": "6. Compliance Status",
    "sign_off": "7. Sign-off",
}

_SEPARATOR = "=" * 72
_SUBSEP = "-" * 72


def _assemble_report(equipment_id: str, sections: Dict[str, str]) -> str:
    """Assemble the maintenance report body from the generated sections.

    The REGULATORY COMPLIANCE NOTE is appended downstream by PostProcessNode —
    see the module docstring.
    """
    lines = [
        _SEPARATOR,
        "EQUIPMENT MAINTENANCE REPORT",
        f"Equipment ID: {equipment_id}",
        _SEPARATOR,
        "",
    ]

    for key in _SECTION_ORDER:
        header = _SECTION_HEADERS.get(key, key.replace("_", " ").title())
        content = sections.get(key, "(Section not generated)")
        lines.append(header)
        lines.append(_SUBSEP)
        lines.append(content)
        lines.append("")

    return "\n".join(lines)


class OutputFormatNode(FunctionNode):
    """Assemble the maintenance report body (inner domain node).

    Reads report_sections from State, renders the maintenance report body, and
    writes it to maintenance_report (and result) for the outer PostProcessNode,
    which applies the output gate and appends the statutory compliance note.

    Inner node — ANONYMOUS trust (see module docstring).

    Input state keys:
        report_sections:  str  — JSON-serialised section dict
        inspection_data:  str  — JSON-serialised inspection payload

    Output state keys (partial dict):
        maintenance_report: str
        result:             str  (same as maintenance_report — backbone convention)
        status:             str
        error_log:          list[str]  (only on ERROR)
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: AgentState, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        sections: Dict[str, str] = from_json(state.get("report_sections"), {})
        inspection_data: Dict[str, Any] = from_json(state.get("inspection_data"), {})

        equipment_id = inspection_data.get("equipment_id", "unknown")

        if not sections:
            logger.error(
                "OutputFormatNode: report_sections missing in state for equipment_id=%s",
                equipment_id,
            )
            emit_trace_event(
                "output_format_failed",
                {"reason": "missing_report_sections", "equipment_id": equipment_id},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"OutputFormatNode: report_sections missing for equipment_id={equipment_id}"],
            }

        # ── Assemble the report body ──────────────────────────────────────────
        report = _assemble_report(equipment_id, sections)

        logger.info(
            "OutputFormatNode: equipment_id=%s report_chars=%d sections=%d",
            equipment_id,
            len(report),
            len(sections),
        )
        emit_trace_event(
            "output_format_complete",
            {
                "equipment_id": equipment_id,
                "report_length": len(report),
                "section_count": len(sections),
            },
            state,
        )

        return {
            "maintenance_report": report,
            "result": report,
            "status": AgentStatus.SUCCESS.value,
        }
