"""AgentCore Platform v1.0"""

# MFG-C2-014 — InputValidateNode
# Inner domain node 1: domain-level normalisation of the inspection record.
#
# Distinct from PreProcessNode (trust gate + the caller-data contract): this
# node applies domain-business rules — findings normalisation, severity
# canonicalisation, and derived-field extraction (finding_count, max_severity).
#
# The record arrives on the inner graph's input_context (seeded from the
# outer graph through src/graph/context_bridge.py — the string channel is
# PII-masked at every node boundary, so the structured record never travels
# as a string). A user_input JSON fallback remains for driving this node or
# the inner graph directly in tests.
#
# Inner node — ANONYMOUS trust: the outer PreProcessNode (VERIFIED_EXTERNAL)
# already enforced caller trust; inner nodes must be ANONYMOUS so the outer
# invocation context passes through the GraphNode boundary without rejection.
#
# Returns ONLY the state keys this node writes (partial-dict contract).

import json
import logging
from typing import Any, ClassVar, Dict, List, Optional

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import to_json

logger = logging.getLogger(__name__)

# Canonical severity ranking (highest → lowest) for max-severity derivation.
_SEVERITY_ORDER: List[str] = ["critical", "high", "medium", "low"]

# Known severity-label aliases for normalisation. The caller contract
# (PreProcessNode) refuses labels outside this vocabulary; the normalisation
# here keeps the inner pipeline well-defined when driven directly.
_SEVERITY_ALIASES: Dict[str, str] = {
    "crit": "critical",
    "critical": "critical",
    "severe": "critical",
    "high": "high",
    "major": "high",
    "med": "medium",
    "medium": "medium",
    "moderate": "medium",
    "minor": "low",
    "low": "low",
    "info": "low",
}


def _normalise_severity(raw: Any) -> str:
    """Normalise a finding severity label to a canonical value."""
    return _SEVERITY_ALIASES.get(str(raw).lower().strip(), "low")


def _normalise_findings(raw_findings: Any) -> List[Dict[str, str]]:
    """Normalise the findings list into a list of {component, observation, severity}."""
    if not isinstance(raw_findings, list):
        raw_findings = [raw_findings] if raw_findings else []
    normalised: List[Dict[str, str]] = []
    for item in raw_findings:
        if isinstance(item, dict):
            normalised.append(
                {
                    "component": str(item.get("component", "unspecified")).strip(),
                    "observation": str(item.get("observation", "")).strip(),
                    "severity": _normalise_severity(item.get("severity", "low")),
                }
            )
        else:
            # Free-text finding — treat the whole string as the observation.
            normalised.append(
                {
                    "component": "unspecified",
                    "observation": str(item).strip(),
                    "severity": "low",
                }
            )
    return normalised


def _max_severity(findings: List[Dict[str, str]]) -> str:
    """Return the highest severity present across all findings."""
    present = {f.get("severity", "low") for f in findings}
    for level in _SEVERITY_ORDER:
        if level in present:
            return level
    return "low"


class InputValidateNode(FunctionNode):
    """Domain normalisation of the inspection record for MFG-C2-014.

    Applies business-rule normalisation beyond the caller contract enforced in
    PreProcessNode: findings normalisation, severity canonicalisation,
    fault-code / corrective-action extraction, derived counts.

    Inner node — ANONYMOUS trust (see module docstring).

    Input state keys:
        input_context: dict — {"inspection": record} seeded by the inner graph
                              from the context bridge (primary channel).
        user_input:    str  — JSON record fallback for driving this node or
                              the inner graph directly in tests.

    Output state keys (partial dict):
        inspection_data: str   — JSON-serialised normalised inspection record
        status:          str
        error_log:       list[str]  (only on ERROR)
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: AgentState, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        input_context = state.get("input_context") or {}
        payload: Any = input_context.get("inspection") if isinstance(input_context, dict) else None

        if payload is None:
            # Fallback: parse the record from the string channel (test path).
            raw = state.get("validated_input") or state.get("user_input", "")
            try:
                payload = json.loads(raw) if isinstance(raw, str) and raw.strip() else {}
            except (json.JSONDecodeError, ValueError):
                logger.error("InputValidateNode: JSON parse error on fallback channel")
                emit_trace_event(
                    "input_validate_failed",
                    {"reason": "json_parse_error"},
                    state,
                )
                return {
                    "status": AgentStatus.ERROR.value,
                    "error_log": ["InputValidateNode: the inspection record is not valid JSON"],
                }

        if not isinstance(payload, dict):
            emit_trace_event(
                "input_validate_failed",
                {"reason": "payload_not_dict"},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["InputValidateNode: the inspection record is not a JSON object"],
            }

        equipment_id = str(payload.get("equipment_id", ""))
        if not equipment_id:
            emit_trace_event(
                "input_validate_failed",
                {"reason": "empty_equipment_id"},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["InputValidateNode: equipment_id is missing or empty"],
            }

        inspection_date = str(payload.get("inspection_date", ""))
        if not inspection_date:
            emit_trace_event(
                "input_validate_failed",
                {"reason": "missing_inspection_date"},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["InputValidateNode: inspection_date is required"],
            }

        # ── Normalise findings ────────────────────────────────────────────────
        findings = _normalise_findings(payload.get("findings", []))
        if not findings:
            emit_trace_event(
                "input_validate_failed",
                {"reason": "no_findings"},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["InputValidateNode: findings is empty"],
            }

        max_severity = _max_severity(findings)

        # ── Extract fault codes + corrective actions ──────────────────────────
        fault_codes = [str(c).strip() for c in payload.get("fault_codes", []) if str(c).strip()]
        corrective_actions = [str(a).strip() for a in payload.get("corrective_actions", []) if str(a).strip()]

        # ── Build normalised inspection_data ──────────────────────────────────
        next_maintenance_raw = payload.get("next_maintenance_date")
        inspection_data: Dict[str, Any] = {
            "equipment_id": equipment_id,
            "equipment_class": str(payload.get("equipment_class", "general")).strip() or "general",
            "inspection_date": inspection_date,
            "findings": findings,
            "fault_codes": fault_codes,
            "corrective_actions": corrective_actions,
            "next_maintenance_date": str(next_maintenance_raw) if next_maintenance_raw else None,
            "inspector": str(payload.get("inspector") or "") or "unassigned",
            "finding_count": len(findings),
            "max_severity": max_severity,
        }

        logger.info(
            "InputValidateNode: equipment_id=%s findings=%d fault_codes=%d max_severity=%s",
            equipment_id,
            len(findings),
            len(fault_codes),
            max_severity,
        )
        emit_trace_event(
            "input_validate_complete",
            {
                "equipment_id": equipment_id,
                "finding_count": len(findings),
                "fault_code_count": len(fault_codes),
                "max_severity": max_severity,
            },
            state,
        )

        return {
            "inspection_data": to_json(inspection_data),
            "status": AgentStatus.SUCCESS.value,
        }
