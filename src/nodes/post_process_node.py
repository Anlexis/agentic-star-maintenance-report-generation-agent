"""AgentCore Platform v1.0"""

# MFG-C2-014 — PostProcessNode
# Outer backbone post_process slot: the external-output boundary for the
# maintenance report.
#
# The agent emits the report in TWO representations — the human-readable text
# and the structured section/compliance data — and both cross the boundary.
# Every layer below therefore runs over both; a representation the gate does
# not walk is a representation it does not protect, and the structured data
# nests its caller-derived text one and two levels deep (findings text inside
# the report_sections mapping), where a scan of top-level strings alone sees
# nothing.
#
# Five independent layers, in order:
#
#   (0) 労働安全衛生法 Article 45 statutory determination — whether the
#       inspected equipment requires a formal, retained 定期自主検査記録 is
#       decided HERE, at the boundary, from the caller's validated record, and
#       the REGULATORY COMPLIANCE NOTE is rendered by this node. It used to be
#       decided by ComplianceCheckNode, a separate node in the inner
#       workflow — the `compliance_check_node` shape the platform architecture
#       anti-pattern, because a check that lives in graph topology is only as
#       strong as the topology: drop the node and the renderer's
#       `compliance.get("safety_record_required", False)` default fabricated a
#       "Formal Record Required: NO" and shipped it as status=success. Measured
#       on 2026-09-03; see the fix issue. Deciding it at the point the response
#       is assembled removes the bypass, and a record the boundary cannot read
#       withholds the response instead of defaulting to NO (fail closed);
#   (1) credential scan — API keys, JWTs, Bearer tokens and password
#       assignments anywhere in either representation withhold the response
#       entirely (sanitised stub, status=ERROR);
#   (2) verbatim caller-text redaction — the report is synthesised from the
#       validated record, so a verbatim embedding of the raw request text or
#       the full serialised record is bulk re-emission, not a feature: any
#       such embedding is replaced with [REDACTED];
#   (3) identifier integrity — the equipment ID and every fault code in the
#       validated record must appear byte-identical in the text report. This
#       template renders no monetary aggregate, so it carries no rounding
#       grid; the invariant its output boundary owes the reader is the
#       opposite one, that a precision identifier is never rewritten (see
#       docs/02_design.md);
#   (4) size cap — a report longer than the cap is truncated, so a malformed
#       upstream state cannot turn the response into a bulk data dump.
#
# The domain output gate is the module-level function `_security_gate_output`
# called from inside execute() — NOT an instance method on the node class.
# The framework auto-wraps node instance-method gate hooks, and a wrapped
# hook returns None on the clean path, which the graph would then pass on as
# the next node's state.
#
# Returns ONLY the state keys this node writes (partial-dict contract).

import logging
import re
from typing import Any, ClassVar, Dict, Iterator, List, Optional, Tuple

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import TrustLevel
from framework.security.credential_detector import detect_credentials
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import from_json, to_json

logger = logging.getLogger(__name__)

# Maximum allowed output size. A maintenance report is normally a few KB; a
# much larger one means raw request data reached the response.
_MAX_OUTPUT_CHARS = 100_000

_TRUNCATION_NOTICE = "\n[Output truncated at the external boundary]"

# Credential patterns that EXTEND the framework recognizer — they are never a
# replacement for it. The framework scans every node result with
# detect_credentials() and RAISES on a hit, and a raise discards this node's
# whole return, taking the containment in _withhold() with it. A local set
# narrower than the framework's is therefore a containment bypass, not merely a
# narrower gate: the value would sail past this gate and blow up one layer
# later, with the clearing discarded. `_credential_findings()` below unions the
# two, so the gate blocks at least everything the framework would refuse.
#
# Only shapes the framework does NOT recognise belong in this list.
_CREDENTIAL_PATTERNS: List[Tuple[str, str]] = [
    (r"(?:sk|pk|ak)-[A-Za-z0-9]{16,}", "api_key_pattern"),
    (r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "jwt_pattern"),
    (r"Bearer\s+[A-Za-z0-9_\-\.]{8,}", "bearer_token"),
    (
        r"(?:password|passwd|secret|api_key|token|access_key|private_key)" r"\s*[:=]\s*\S{8,}",
        "credential_assignment",
    ),
]

# State fields that must never be embedded verbatim in the response. The
# report is synthesised from validated fields; the raw request text or the
# full serialised record reappearing verbatim means caller-supplied content
# reached the external surface unprocessed.
_BLOCKED_FIELDS = frozenset({"user_input", "inspection_payload"})

# Only substantial values are matched, so a short incidental overlap between a
# request string and a report line is not redacted.
_MIN_BLOCKED_VALUE_CHARS = 20

_REDACTION_PLACEHOLDER = "[REDACTED]"

# ── 労働安全衛生法 Article 45 — statutory determination (layer 0) ──────────────
#
# Article 45 定期自主検査 mandates a formal, retained self-inspection record for
# 特定機械等 (specified machinery), and the record must be kept for 3 years.
#
# Record-required criteria (rule-based, unchanged from the retired
# ComplianceCheckNode):
#   - equipment is 特定機械等 (specified machinery)  OR
#   - max finding severity is high/critical           OR
#   - one or more fault codes reported
#
# Every input is derived HERE from the caller's validated record, not read back
# from pipeline state: a determination that trusts an upstream node's derived
# field is only as trustworthy as that node's continued presence in the graph,
# which is precisely the property that made the previous shape bypassable.

_ART45_RETENTION_YEARS = 3
_ART45_TRIGGER_SEVERITIES = frozenset({"high", "critical"})

# Equipment classes that are 特定機械等 (specified machinery) under Art 45.
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

# Canonical severity ranking, highest first.
_SEVERITY_RANK: Tuple[str, ...] = ("critical", "high", "medium", "low")

_COMPLIANCE_SEPARATOR = "=" * 72
_COMPLIANCE_SUBSEP = "-" * 72

_ART45_REGULATORY_BASIS = "労働安全衛生法 (Industrial Safety and Health Act) Article 45 — 定期自主検査記録"


def _record_max_severity(findings: Any) -> str:
    """Highest severity present across the record's findings ("low" when none)."""
    if not isinstance(findings, list):
        return "low"
    present = {str(f.get("severity", "low")).strip().lower() for f in findings if isinstance(f, dict)}
    for level in _SEVERITY_RANK:
        if level in present:
            return level
    return "low"


def _art45_determination(record: Dict[str, Any]) -> Dict[str, Any]:
    """Evaluate 労働安全衛生法 Art 45 record-required criteria on a validated record.

    Reads only fields the caller contract guarantees (equipment_class, findings,
    fault_codes) and derives everything else, so the determination holds no
    dependency on any upstream node. Returns the determination plus the trigger
    values, for traceability in the rendered compliance note.
    """
    equipment_class = str(record.get("equipment_class", "general")).strip().lower()
    is_specified_machinery = equipment_class in _SPECIFIED_MACHINERY_CLASSES

    max_severity = _record_max_severity(record.get("findings"))
    severity_triggered = max_severity in _ART45_TRIGGER_SEVERITIES

    fault_codes = record.get("fault_codes")
    fault_code_count = len(fault_codes) if isinstance(fault_codes, list) else 0
    fault_triggered = fault_code_count > 0

    safety_record_required = bool(is_specified_machinery or severity_triggered or fault_triggered)

    reasons: List[str] = []
    if is_specified_machinery:
        reasons.append("Equipment is 特定機械等 (specified machinery) — periodic self-inspection record mandatory")
    if severity_triggered:
        reasons.append(f"Max finding severity ({max_severity}) is high/critical")
    if fault_triggered:
        reasons.append(f"{fault_code_count} fault code(s) reported")
    if not reasons:
        reasons.append(
            "General equipment with low/medium findings and no fault codes — "
            "formal Art 45 record not mandated (retain per internal policy)"
        )

    return {
        "safety_record_required": safety_record_required,
        "reason": "; ".join(reasons),
        "is_specified_machinery": is_specified_machinery,
        "severity_threshold_met": severity_triggered,
        "fault_code_threshold_met": fault_triggered,
        "max_severity": max_severity,
        "fault_code_count": fault_code_count,
        "retention_years": _ART45_RETENTION_YEARS,
        "regulatory_basis": _ART45_REGULATORY_BASIS,
    }


def _render_compliance_note(determination: Dict[str, Any]) -> str:
    """Render the REGULATORY COMPLIANCE NOTE trailer for the report.

    Rendered by the output boundary, from the boundary's own determination, so
    the note a reader acts on and the determination the agent made are the same
    value — there is no state key between them to drop.
    """
    required = bool(determination["safety_record_required"])
    retention = int(determination["retention_years"])
    required_text = f"YES — retain for {retention} years" if required else "NO"
    return "\n".join(
        [
            _COMPLIANCE_SEPARATOR,
            "REGULATORY COMPLIANCE NOTE",
            _COMPLIANCE_SUBSEP,
            f"  Regulatory Basis:          {determination['regulatory_basis']}",
            f"  Formal Record Required:    {required_text}",
            f"  Determination Basis:       {determination['reason']}",
            _COMPLIANCE_SEPARATOR,
        ]
    )


def _credential_findings(text: str) -> List[str]:
    """Names of credential shapes in `text`, from the FRAMEWORK detector UNION the local set.

    The framework half is what makes this fail-closed: FunctionNode scans every
    node result with detect_credentials() and raises on a hit, discarding the
    return — so any shape the framework catches and this gate missed would
    bypass the containment in _withhold(). Unioning them makes drift impossible
    by construction. The local half adds only what the framework does not
    recognise (e.g. `password: ...` assignments).
    """
    names: List[str] = [str(finding["type"]) for finding in detect_credentials(text)]
    for pattern, name in _CREDENTIAL_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            names.append(name)
    return names


def _walk_strings(payload: Any, path: str = "") -> Iterator[Tuple[str, str]]:
    """Yield (path, string) for every string nested anywhere in payload.

    Walks dicts (keys included), lists and tuples to any depth — the gate
    must see caller-derived text wherever it rides, not only at top level.
    """
    if isinstance(payload, str):
        yield (path or "value", payload)
    elif isinstance(payload, dict):
        for key, value in payload.items():
            child = f"{path}.{key}" if path else str(key)
            if isinstance(key, str):
                yield (f"{child}<key>", key)
            yield from _walk_strings(value, child)
    elif isinstance(payload, (list, tuple)):
        for index, value in enumerate(payload):
            yield from _walk_strings(value, f"{path}[{index}]")


def _security_gate_output(content: Any) -> Optional[str]:
    """Scan output for disallowed credential/secret patterns.

    Walks every string nested anywhere in ``content`` (strings, mappings,
    sequences — keys included) and returns the first violation name, or None
    if the output is clean. Module-level function (not a node instance
    method) — the framework auto-wraps instance-method gate hooks, which
    breaks the real invoke path.
    """
    for _, value in _walk_strings(content):
        names = _credential_findings(value)
        if names:
            return names[0]
    return None


def _redact_blocked_fields(report: str, state: AgentState) -> Tuple[str, List[str]]:
    """Replace verbatim embeddings of blocked state fields with [REDACTED].

    Returns (redacted_report, redacted_field_names).
    """
    redacted_fields: List[str] = []
    result = report
    for field in sorted(_BLOCKED_FIELDS):
        value = state.get(field)
        if isinstance(value, str) and len(value) >= _MIN_BLOCKED_VALUE_CHARS and value in result:
            result = result.replace(value, _REDACTION_PLACEHOLDER)
            redacted_fields.append(field)
    return result, redacted_fields


def _identifier_integrity_violations(report: str, record: Dict[str, Any]) -> List[str]:
    """Names of record identifiers that do not appear byte-identical in the report.

    The equipment ID and every fault code are precision identifiers: a report
    that renders them altered in any way (masked, reformatted, digit-grouped)
    would send a technician to the wrong equipment or the wrong fault. The
    boundary re-checks them instead of assuming the pipeline preserved them.
    """
    violations: List[str] = []
    equipment_id = record.get("equipment_id")
    if isinstance(equipment_id, str) and equipment_id and equipment_id not in report:
        violations.append("equipment_id")
    fault_codes = record.get("fault_codes")
    if isinstance(fault_codes, list):
        for index, code in enumerate(fault_codes):
            if isinstance(code, str) and code and code not in report:
                violations.append(f"fault_codes[{index}]")
    return violations


class PostProcessNode(FunctionNode):
    """External-output boundary for the maintenance report.

    Outer backbone post_process slot. Declared ANONYMOUS — trust was
    already enforced at PreProcessNode (VERIFIED_EXTERNAL).

    Input state keys:
        maintenance_report: str  — formatted report body from inner OutputFormatNode
        report_sections:    str  — JSON-serialised section dict (structured repr)
        inspection_payload: str  — JSON-serialised validated record (outer state)
        inspection_data:    str  — inner-graph record shape, honoured for
                                   direct-execute callers only

    Output state keys (partial dict):
        formatted_output:       str
        result:                 str
        compliance_flags:       str   — JSON-serialised Art 45 determination
        safety_record_required: bool  — statutory record-required flag
        status:                 str
        error_log:              list[str]  (only on ERROR)
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def _withhold(self, state: AgentState, violation: str, reason: str) -> Dict[str, Any]:
        """Withhold the response entirely, and CONTAIN it.

        Blocking is not a status flip. The framework envelope resolves the
        caller-facing value as ``formatted_output or result`` with NO regard for
        status (AgentBaseGraph.get_output), so returning ERROR while leaving the
        output-bearing fields populated still ships the refused report inside
        the error envelope. Every field that carries report content is therefore
        overwritten here — not only the two the envelope reads directly, but the
        structured representations merged in from the inner graph, which would
        otherwise sit in state as a second copy of the same answer.

        The replacement for ``formatted_output`` is deliberately NON-EMPTY: a
        falsy stand-in ("" or {}) is exactly what re-opens the ``or result``
        fallback the clearing just closed.

        ``violation``/``reason`` name LOCATIONS and pattern NAMES only, never a
        matched value. Echoing the value would put the refused string back into
        this node's own result, where the framework's output-side credential
        scan raises — and a raise DISCARDS this entire return, so the clearing
        below would never be applied at all.
        """
        emit_trace_event(
            "post_process_output_blocked",
            {"violation": violation, "reason": reason},
            state,
        )
        sanitised = (
            f"[MAINTENANCE REPORT WITHHELD: output contained a disallowed pattern "
            f"({violation}). Contact the plant maintenance security team for the "
            f"original report.]"
        )
        return {
            # Caller-facing values — truthy stand-in, see the docstring.
            "formatted_output": sanitised,
            "result": sanitised,
            # Report-bearing state merged in from the inner graph. Cleared so no
            # second copy of the refused answer survives the block.
            "maintenance_report": "",
            "report_sections": None,
            # Statutory determination — this node's own product. Cleared by
            # PRESENCE, not omission: LangGraph merges partial deltas, so a key
            # left out of the delta keeps whatever value state already held.
            "compliance_flags": None,
            "safety_record_required": None,
            "status": AgentStatus.ERROR.value,
            "error_log": [f"PostProcessNode: {reason} — {violation}"],
        }

    def execute(self, state: AgentState, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        maintenance_report: str = state.get("maintenance_report") or ""
        report_generated = bool(maintenance_report.strip())

        # ── Fallback for empty report ─────────────────────────────────────────
        if not maintenance_report.strip():
            logger.warning("PostProcessNode: maintenance_report is empty — using fallback message")
            maintenance_report = (
                "[Maintenance Report] No report content generated. " "Check error_log for upstream failures."
            )

        structured_sections = from_json(state.get("report_sections"), None)
        # The validated record as it exists in the OUTER state. PreProcessNode
        # writes it to inspection_payload; `inspection_data` is the INNER
        # graph's key and MaintenanceReportGraphNode.merge_output() does not
        # map it outward, so reading inspection_data alone left this layer
        # permanently comparing against {} — i.e. silently disabled on the real
        # invoke path (measured: an erased equipment ID shipped as SUCCESS).
        # inspection_data is still honoured for direct-execute callers.
        record = from_json(state.get("inspection_payload"), None)
        if not isinstance(record, dict):
            record = from_json(state.get("inspection_data"), {}) or {}

        # ── (0) 労働安全衛生法 Art 45 statutory determination ─────────────────
        # Decided here, from the validated record, and rendered here. A report
        # the boundary cannot annotate is WITHHELD — the retired
        # ComplianceCheckNode shape defaulted the missing determination to
        # "Formal Record Required: NO" and shipped it as success, which is
        # worse than a refusal: the reader gets no signal at all.
        determination: Optional[Dict[str, Any]] = None
        if report_generated:
            if not record:
                logger.error("PostProcessNode: no validated record at the boundary — Art 45 determination impossible")
                return self._withhold(
                    state,
                    "inspection_payload",
                    "the statutory Art 45 determination could not be made at the output boundary",
                )
            determination = _art45_determination(record)
            maintenance_report = f"{maintenance_report}\n{_render_compliance_note(determination)}"
            emit_trace_event(
                "post_process_art45_determined",
                {
                    "safety_record_required": determination["safety_record_required"],
                    "severity_threshold_met": determination["severity_threshold_met"],
                    "fault_code_threshold_met": determination["fault_code_threshold_met"],
                },
                state,
            )
        safety_record_required: bool = bool(determination["safety_record_required"]) if determination else False

        # ── (1) Credential scan — every representation, nested walk ──────────
        for representation, content in (
            ("report_text", maintenance_report),
            ("report_sections", structured_sections),
            ("compliance_flags", determination),
        ):
            violation = _security_gate_output(content)
            if violation:
                logger.error(
                    "PostProcessNode: credential pattern in %s — %s",
                    representation,
                    violation,
                )
                return self._withhold(state, violation, f"credential pattern detected in {representation}")

        # ── (2) Verbatim caller-text redaction ────────────────────────────────
        maintenance_report, redacted = _redact_blocked_fields(maintenance_report, state)
        if redacted:
            emit_trace_event(
                "post_process_verbatim_redacted",
                {"fields": redacted},
                state,
            )

        # ── (3) Identifier integrity (only meaningful for a generated report) ─
        integrity = _identifier_integrity_violations(maintenance_report, record) if report_generated else []
        if integrity:
            logger.error("PostProcessNode: identifier integrity violation — %s", integrity)
            return self._withhold(
                state,
                ", ".join(integrity),
                "identifier altered between record and rendered report",
            )

        # ── (4) Size cap ──────────────────────────────────────────────────────
        if len(maintenance_report) > _MAX_OUTPUT_CHARS:
            emit_trace_event(
                "post_process_output_truncated",
                {"original_length": len(maintenance_report)},
                state,
            )
            maintenance_report = maintenance_report[: _MAX_OUTPUT_CHARS - len(_TRUNCATION_NOTICE)] + _TRUNCATION_NOTICE

        logger.info(
            "PostProcessNode: output gate passed — length=%d safety_record_required=%s",
            len(maintenance_report),
            safety_record_required,
        )
        emit_trace_event(
            "post_process_complete",
            {
                "output_length": len(maintenance_report),
                "safety_record_required": safety_record_required,
            },
            state,
        )

        return {
            "formatted_output": maintenance_report,
            "result": maintenance_report,
            # The boundary's own statutory determination — the single source
            # surfaced to the caller by MaintenanceReportGeneratorAgent.get_output().
            "compliance_flags": to_json(determination) if determination is not None else None,
            "safety_record_required": safety_record_required if determination is not None else None,
            "status": AgentStatus.SUCCESS.value,
        }
