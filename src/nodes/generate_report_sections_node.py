"""AgentCore Platform v1.0"""

# MFG-C2-014 — GenerateReportSectionsNode
# Inner domain node 3: generate all 7 maintenance-report sections.
#
# DETERMINISTIC template-based generation: the section text is synthesised
# from the inspection_data fields using fixed Python templates — no model
# call is made in the bundled build.
#
# The llm block in config/config.yaml (system_prompt_template =
# prompts/maintenance_report.j2, temperature, max_tokens) and the
# prompts/maintenance_report.j2 template are RESERVED for a live-model build.
# They are validated and forwarded to this graph but intentionally NOT
# consumed here: the .j2 is not loaded and no configurable prompt drives
# generation — no model call is faked.
#
# Sections produced:
#   1. equipment_summary
#   2. inspection_findings
#   3. fault_codes
#   4. corrective_actions
#   5. next_maintenance_schedule
#   6. compliance_status
#   7. sign_off
#
# Inner node — ANONYMOUS trust (the outer trust gate already ran).
# Returns ONLY the state keys this node writes (partial-dict contract).

import logging
from typing import Any, ClassVar, Dict, List, Optional

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import from_json, to_json

logger = logging.getLogger(__name__)


def _section_equipment_summary(d: Dict[str, Any]) -> str:
    """Generate the Equipment Summary section."""
    equipment_id = d.get("equipment_id", "N/A")
    equipment_class = d.get("equipment_class", "general")
    inspection_date = d.get("inspection_date", "N/A")
    criticality = str(d.get("criticality", "unknown")).upper()
    finding_count = d.get("finding_count", 0)
    lines = [
        f"Equipment ID:    {equipment_id}",
        f"Equipment Class: {equipment_class}",
        f"Inspection Date: {inspection_date}",
        f"Criticality:     {criticality}",
        f"Findings Logged: {finding_count}",
    ]
    return "\n".join(lines)


def _section_inspection_findings(d: Dict[str, Any]) -> str:
    """Generate the Inspection Findings section."""
    findings: List[Dict[str, Any]] = d.get("findings", [])
    if not findings:
        return "No findings recorded."
    lines = []
    for i, f in enumerate(findings, 1):
        component = f.get("component", "unspecified")
        observation = f.get("observation", "") or "(no observation text)"
        severity = str(f.get("severity", "low")).upper()
        lines.append(f"  {i}. [{severity}] {component}: {observation}")
    return "\n".join(lines)


def _section_fault_codes(d: Dict[str, Any]) -> str:
    """Generate the Fault Codes section."""
    codes: List[Any] = d.get("fault_codes", [])
    if not codes:
        return "No fault codes reported."
    lines = [f"  - {code}" for code in codes]
    return "\n".join(lines)


def _section_corrective_actions(d: Dict[str, Any]) -> str:
    """Generate the Corrective Actions section."""
    actions: List[Any] = d.get("corrective_actions", [])
    if not actions:
        return "No corrective actions recorded."
    lines = [f"  {i + 1}. {action}" for i, action in enumerate(actions)]
    return "\n".join(lines)


def _section_next_maintenance(d: Dict[str, Any]) -> str:
    """Generate the Next Maintenance Schedule section."""
    next_date = d.get("next_maintenance_date")
    if not next_date:
        return (
            "Next maintenance date not scheduled. "
            "Schedule per the equipment maintenance plan and regulatory interval."
        )
    return f"  Next Scheduled Maintenance: {next_date}"


def _section_compliance_status(d: Dict[str, Any]) -> str:
    """Generate the Compliance Status section (pre-determination summary)."""
    is_specified = d.get("is_specified_machinery", False)
    safety_reportable = d.get("safety_reportable", False)
    max_severity = str(d.get("max_severity", "low")).upper()
    lines = [
        f"  Specified Machinery (特定機械等): {'YES' if is_specified else 'NO'}",
        f"  Max Finding Severity:            {max_severity}",
        f"  Safety-Record Pre-Check:         {'REPORTABLE' if safety_reportable else 'NOT REPORTABLE'}",
        "  (Final determination under 労働安全衛生法 Art 45 — see compliance note below.)",
    ]
    return "\n".join(lines)


def _section_sign_off(d: Dict[str, Any]) -> str:
    """Generate the Sign-off section."""
    inspector = d.get("inspector", "unassigned")
    inspection_date = d.get("inspection_date", "N/A")
    lines = [
        f"  Inspected By: {inspector}",
        f"  Date:         {inspection_date}",
        "  Approved By:  __________________________  (Maintenance Manager)",
        "  Approval Date: _________________________",
    ]
    return "\n".join(lines)


class GenerateReportSectionsNode(FunctionNode):
    """Generate all 7 maintenance report sections.

    Deterministic template-based synthesis — no model call. The llm block in
    config/config.yaml + prompts/maintenance_report.j2 are reserved for a
    live-model build and are NOT consumed here.

    Inner node — ANONYMOUS trust (see module docstring).

    Input state keys:
        inspection_data: str  — JSON-serialised enriched inspection payload

    Output state keys (partial dict):
        report_sections: str  — JSON-serialised section dict
        status:          str
        error_log:       list[str]  (only on ERROR)
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: AgentState, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        inspection_data: Dict[str, Any] = from_json(state.get("inspection_data"), {})

        if not inspection_data:
            logger.error("GenerateReportSectionsNode: inspection_data missing in state")
            emit_trace_event(
                "generate_report_sections_failed",
                {"reason": "missing_inspection_data"},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["GenerateReportSectionsNode: inspection_data missing in state"],
            }

        equipment_id = inspection_data.get("equipment_id", "unknown")

        # ── Generate all sections (deterministic; no model call — see docstring) ─
        sections: Dict[str, str] = {
            "equipment_summary": _section_equipment_summary(inspection_data),
            "inspection_findings": _section_inspection_findings(inspection_data),
            "fault_codes": _section_fault_codes(inspection_data),
            "corrective_actions": _section_corrective_actions(inspection_data),
            "next_maintenance_schedule": _section_next_maintenance(inspection_data),
            "compliance_status": _section_compliance_status(inspection_data),
            "sign_off": _section_sign_off(inspection_data),
        }

        logger.info(
            "GenerateReportSectionsNode: equipment_id=%s sections=%d",
            equipment_id,
            len(sections),
        )
        emit_trace_event(
            "generate_report_sections_complete",
            {
                "equipment_id": equipment_id,
                "section_count": len(sections),
                "section_keys": sorted(sections.keys()),
            },
            state,
        )

        return {
            "report_sections": to_json(sections),
            "status": AgentStatus.SUCCESS.value,
        }
