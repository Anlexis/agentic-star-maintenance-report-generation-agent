"""AgentCore Platform v1.0"""

# MFG-C2-014 — ParseInspectionDataNode
# Inner domain node 2: enrich and classify the validated inspection data.
#
# Responsibilities:
#   - Classify maintenance criticality from max finding severity + fault count
#   - Derive a 労働安全衛生法 Art 45 safety-record pre-check indicator
#   - Annotate the inspection_data with derived fields for downstream nodes
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

from src.schemas.state import from_json, to_json

logger = logging.getLogger(__name__)

# Equipment classes that are 特定機械等 (specified machinery) under
# 労働安全衛生法 Art 45 — periodic self-inspection records are mandatory.
_SPECIFIED_MACHINERY_CLASSES = frozenset(
    {
        "specified_machinery",
        "特定機械",
        "boiler",
        "crane",
        "pressure_vessel",
        "elevator",
    }
)


def _classify_criticality(max_severity: str, fault_code_count: int) -> str:
    """Classify maintenance criticality from severity and fault-code count.

    Returns "critical", "high", "medium", or "low".
    """
    if max_severity == "critical" or fault_code_count >= 5:
        return "critical"
    if max_severity == "high" or fault_code_count >= 2:
        return "high"
    if max_severity == "medium" or fault_code_count >= 1:
        return "medium"
    return "low"


class ParseInspectionDataNode(FunctionNode):
    """Enrich inspection data with criticality classification and derived fields.

    Inner node — ANONYMOUS trust (see module docstring).

    Input state keys:
        inspection_data: str  — JSON-serialised inspection payload

    Output state keys (partial dict):
        inspection_data: str  — enriched JSON string, same key updated
        status:          str
        error_log:       list[str]  (only on ERROR)
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: AgentState, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        inspection_data: Dict[str, Any] = from_json(state.get("inspection_data"), {})

        if not inspection_data:
            logger.error("ParseInspectionDataNode: inspection_data is empty or missing")
            emit_trace_event(
                "parse_inspection_data_failed",
                {"reason": "missing_inspection_data"},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["ParseInspectionDataNode: inspection_data missing in state"],
            }

        equipment_id = inspection_data.get("equipment_id", "unknown")
        equipment_class = str(inspection_data.get("equipment_class", "general")).lower()
        max_severity = str(inspection_data.get("max_severity", "low"))
        fault_code_count = len(inspection_data.get("fault_codes", []))

        # ── Criticality classification ────────────────────────────────────────
        criticality = _classify_criticality(max_severity, fault_code_count)

        # ── 労働安全衛生法 Art 45 pre-check ────────────────────────────────────
        is_specified_machinery = equipment_class in _SPECIFIED_MACHINERY_CLASSES
        safety_reportable = is_specified_machinery or max_severity in ("high", "critical") or fault_code_count > 0

        # ── Enrich inspection_data ────────────────────────────────────────────
        enriched: Dict[str, Any] = dict(inspection_data)
        enriched["criticality"] = criticality
        enriched["is_specified_machinery"] = is_specified_machinery
        enriched["safety_reportable"] = safety_reportable

        logger.info(
            "ParseInspectionDataNode: equipment_id=%s criticality=%s safety_reportable=%s",
            equipment_id,
            criticality,
            safety_reportable,
        )
        emit_trace_event(
            "parse_inspection_data_complete",
            {
                "equipment_id": equipment_id,
                "criticality": criticality,
                "safety_reportable": safety_reportable,
                "fault_code_count": fault_code_count,
                "max_severity": max_severity,
            },
            state,
        )

        return {
            "inspection_data": to_json(enriched),
            "status": AgentStatus.SUCCESS.value,
        }
