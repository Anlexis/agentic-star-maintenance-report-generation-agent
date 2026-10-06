"""AgentCore Platform v1.0"""

# MFG-C2-014 — PreProcessNode
# Outer backbone pre_process slot: the trust gate + the caller-data contract.
#
# This node owns everything the caller controls. Every field of the inspection
# record is checked here against explicit bounds before any domain node sees
# it, and a rejection names the offending FIELD, never the offending value.
#
# Caller data arrives on two channels and both are validated by the same rules:
#
#   input_context  the structured channel (recommended). Fields:
#                    channel      inert slug, [a-z0-9_]{1,32}
#                    inspection   the inspection record (JSON object, shape below)
#   input          the fallback: a JSON object with the same inspection-record
#                  shape. This exists for direct invocation and test harnesses.
#                  It is lossy by construction — the platform masks two-word
#                  Title-Case runs in the string channel before this node runs,
#                  and a component or inspector label has exactly that shape —
#                  so callers with real inspection data use the context channel.
#                  A record altered that way is refused, never repaired (see
#                  the transit-integrity check below).
#
# Returns ONLY the state keys this node writes (partial-dict contract).

import datetime
import json
import logging
import re
from typing import Any, ClassVar, Dict, List, Optional, Tuple

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import TrustLevel
from framework.security.pii_detector import detect_pii
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import to_json

logger = logging.getLogger(__name__)

# ── Structural bounds ─────────────────────────────────────────────────────────

# Upper bound on the raw request body, matched to the transport adapter's
# request-size cap: a larger body is refused before it is parsed.
_MAX_PAYLOAD_CHARS = 262_144

_MAX_FINDINGS = 100
_MAX_FAULT_CODES = 50
_MAX_CORRECTIVE_ACTIONS = 50

_MAX_COMPONENT_CHARS = 80
_MAX_OBSERVATION_CHARS = 500
_MAX_ACTION_CHARS = 500
_MAX_INSPECTOR_CHARS = 80

# Channel tag used when the caller supplies none.
_DEFAULT_CHANNEL = "unspecified"

# Required top-level keys for a valid inspection record. equipment_id is the
# mandatory identifier; inspection_date and findings are the minimum needed to
# generate a maintenance report.
_REQUIRED_INSPECTION_KEYS = frozenset(
    {
        "equipment_id",
        "inspection_date",
        "findings",
    }
)

# ── Field alphabets ───────────────────────────────────────────────────────────

# Identifiers that are rendered into the report verbatim (equipment ID, fault
# codes) are locked to a bounded identifier alphabet: letters, digits, hyphen
# and underscore. They can neither carry markup nor break the line structure
# of the report, and the output boundary re-checks them byte-identical.
_EQUIPMENT_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
_FAULT_CODE_RE = re.compile(r"[A-Za-z0-9_-]{1,32}")

# Caller strings that select behaviour are inert slugs — lowercase
# alphanumerics and underscore, bounded length.
_SLUG_RE = re.compile(r"[a-z0-9_]{1,32}")

# Dates are ISO-8601 calendar dates, shape-checked then parsed for validity.
_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

# Component / inspector labels and free-text observations may carry Japanese
# maintenance terminology alongside ASCII. The alphabet permits letters,
# digits, common technical punctuation and CJK ranges; it excludes every
# control character (including newline and tab), quotes-as-markup characters
# (angle brackets, braces, backslash) — so a label or observation can neither
# carry markup nor break the one-line-per-entry structure of the report it is
# rendered into.
_TEXT_ALPHABET = (
    "[\\-"  # literal hyphen, escaped so no range forms around it
    "A-Za-z0-9 .,;:'()_/+%#&*=?!°µΩ"
    "　-〿"  # CJK punctuation
    "぀-ヿ"  # hiragana + katakana
    "ㇰ-ㇿ"  # katakana extensions
    "一-鿿"  # CJK unified ideographs
    "＀-￯"  # fullwidth forms
    "]"
)
_TEXT_RE = re.compile(f"{_TEXT_ALPHABET}+")
_SPACES_RE = re.compile(r" +")

# ── Severity vocabulary (closed) ──────────────────────────────────────────────

# Known severity labels and their canonical values. Anything outside this set
# is refused: silently downgrading an unknown label to "low" would understate
# the finding it describes, on the exact field that drives the compliance
# determination.
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

# ── Instruction-override screen ───────────────────────────────────────────────

# Deliberately NARROW. Genuine maintenance prose borrows the same verbs an
# injection uses: "Override the safety interlock and re-test", "ignore the
# previous reading when the sensor is faulty", "Insert Molded Contact" are all
# real inspection language. So every alternative here is anchored on a whole
# directive PHRASE — the override verb plus the prompt-specific noun — or on a
# chat-template control token, which has no legitimate reading in an
# inspection record.
_INSTRUCTION_OVERRIDE_RE = re.compile(
    # Chat-template control tokens: <|im_start|>, <|system|>, [INST], <<SYS>>.
    r"<\|[a-z_]{2,32}\|>"
    r"|\[/?INST\]"
    r"|<</?SYS>>"
    # "ignore/disregard/forget the previous instructions/rules/prompt"
    r"|\b(?:ignore|disregard|forget)\s+(?:all\s+|any\s+|the\s+)?"
    r"(?:previous|prior|above|earlier|preceding)\s+"
    r"(?:instruction|instructions|prompt|prompts|rule|rules)\b"
    # "ignore all rules/instructions" (no noun of its own — the bare form)
    r"|\b(?:ignore|disregard)\s+all\s+(?:rules|instructions)\b"
    # "reveal/print the system prompt / hidden instructions"
    r"|\b(?:reveal|show|print|repeat|output|disclose|dump)\s+(?:me\s+)?(?:your|the)\s+"
    r"(?:system\s+prompt|system\s+message|initial\s+prompt|hidden\s+instructions)\b"
    # "you are now a ..." — but "you are now an authorized inspector" is not a
    # plausible inspection observation either; no domain carve-out is needed.
    r"|\byou\s+are\s+now\s+(?:a|an)\s"
    # "act as developer/admin/root/jailbroken/unrestricted mode"
    r"|\bact\s+as\s+(?:if\s+you\s+(?:are|were)\s+)?(?:a\s+|an\s+)?"
    r"(?:developer|admin|administrator|root|jailbroken|unrestricted)\s+mode\b"
    # "override your/the instructions/rules" — never "override the safety
    # interlock", which is real maintenance language, so "safety" alone is NOT
    # in the noun set; only instruction-like nouns qualify.
    r"|\boverride\s+(?:your|the)\s+"
    r"(?:instruction|instructions|rule|rules|guardrail|guardrails|"
    r"guideline|guidelines|restriction|restrictions|system\s+prompt)\b"
    # "new system prompt:" header form
    r"|\b(?:new|updated)\s+system\s+(?:prompt|instructions)\s*[:=]",
    re.IGNORECASE,
)

# Schema field names that may be echoed into a rejection message. Any payload
# key outside this set is caller-controlled text, so the path shows a
# placeholder instead — a hostile field NAME is never echoed either.
_KNOWN_FIELD_NAMES = frozenset(
    {
        "equipment_id",
        "equipment_class",
        "inspection_date",
        "findings",
        "fault_codes",
        "corrective_actions",
        "next_maintenance_date",
        "inspector",
        "component",
        "observation",
        "severity",
        "channel",
        "inspection",
    }
)

# ── Contact-identifier screen ─────────────────────────────────────────────────

# Contact identifiers have no place in an inspection record, and the text
# alphabet alone does not exclude them (it permits digits, spaces, hyphens and
# the at-sign is excluded but phone shapes are not). Only high-precision
# detector classes are screened here:
#   - the personal-name heuristics are excluded because they match any two
#     Title Case words — which is what a component label or an inspector
#     sign-off name IS ("Hoist Wire Rope", "Kenji Tanaka"); screening on them
#     would refuse ordinary inspection records. The inspector field is a
#     required element of the report's sign-off block by design.
#   - the 12-digit national-identifier shape is excluded because it is also
#     the shape of a 12-digit serial or article number in an observation.
_SCREENED_PII_TYPES = frozenset({"email", "phone_jp", "phone_us", "ssn_us", "credit_card"})

# The platform masks detected personal names in the string channel before
# execute() runs, and a two-word component or inspector label has exactly that
# shape. A record that arrived through the user_input fallback carrying this
# sentinel was altered in transit, so its labels — and the report generated
# from them — cannot be trusted.
_TRANSIT_MASK_SENTINEL = "[MASKED]"

# ── Helpers ───────────────────────────────────────────────────────────────────


def _scan_for_instruction_override(value: Any, path: str) -> Optional[str]:
    """Depth-first scan of every string in the payload — keys included.

    Returns the JSON path of the first string carrying an instruction-override
    directive, or None. Path components outside the declared schema are
    masked, so the returned path is always safe to name in an error message.
    """
    if isinstance(value, str):
        return (path or "payload") if _INSTRUCTION_OVERRIDE_RE.search(value) else None
    if isinstance(value, dict):
        for key, item in value.items():
            safe_key = key if isinstance(key, str) and key in _KNOWN_FIELD_NAMES else "<unrecognised-field>"
            key_path = f"{path}.{safe_key}" if path else safe_key
            if isinstance(key, str) and _INSTRUCTION_OVERRIDE_RE.search(key):
                return key_path
            found = _scan_for_instruction_override(item, key_path)
            if found is not None:
                return found
    if isinstance(value, list):
        for index, item in enumerate(value):
            found = _scan_for_instruction_override(item, f"{path}[{index}]")
            if found is not None:
                return found
    return None


def _contains_mask_sentinel(value: Any) -> bool:
    """True if any string anywhere in the payload carries the transit-mask sentinel."""
    if isinstance(value, str):
        return _TRANSIT_MASK_SENTINEL in value
    if isinstance(value, dict):
        return any(_contains_mask_sentinel(k) or _contains_mask_sentinel(v) for k, v in value.items())
    if isinstance(value, list):
        return any(_contains_mask_sentinel(item) for item in value)
    return False


def _validate_identifier(
    value: Any, field: str, pattern: re.Pattern[str], limit: int
) -> Tuple[Optional[str], Optional[str]]:
    """Validate one rendered identifier. Returns (value, error).

    Fail CLOSED: only a string that fullmatches the identifier alphabet within
    the length limit is accepted. The rejected value is never echoed — the
    error names the field and restates the contract.
    """
    if not isinstance(value, str) or not pattern.fullmatch(value):
        return None, (f"PreProcessNode: {field} must be {limit} characters or fewer of letters, digits, '-' or '_'")
    return value, None


def _validate_text(value: Any, field: str, limit: int) -> Tuple[Optional[str], Optional[str]]:
    """Validate one bounded free-text field. Returns (normalised_value, error).

    Fail CLOSED: only a string of the permitted text alphabet within the length
    limit is accepted. The alphabet is checked on the value AS SUPPLIED, before
    spaces are collapsed — normalising first would silently repair a value
    carrying a newline or a tab into an acceptable one, and caller-controlled
    line structure is exactly what this contract exists to keep out of the
    report. The rejected value is never echoed.
    """
    if not isinstance(value, str) or not _TEXT_RE.fullmatch(value):
        return None, (
            f"PreProcessNode: {field} must be {limit} characters or fewer of plain text "
            f"(no control characters, newlines, or markup)"
        )
    collapsed = _SPACES_RE.sub(" ", value).strip()
    if not collapsed or len(collapsed) > limit:
        return None, (
            f"PreProcessNode: {field} must be {limit} characters or fewer of plain text "
            f"(no control characters, newlines, or markup)"
        )
    contact = sorted({f["type"] for f in detect_pii(collapsed) if f.get("type") in _SCREENED_PII_TYPES})
    if contact:
        return None, f"PreProcessNode: {field} must not contain contact identifiers"
    return collapsed, None


def _validate_date(value: Any, field: str) -> Tuple[Optional[str], Optional[str]]:
    """Validate one ISO-8601 calendar date. Returns (value, error)."""
    if not isinstance(value, str) or not _ISO_DATE_RE.fullmatch(value):
        return None, f"PreProcessNode: {field} must be an ISO-8601 date (YYYY-MM-DD)"
    try:
        datetime.date.fromisoformat(value)
    except ValueError:
        return None, f"PreProcessNode: {field} must be an ISO-8601 date (YYYY-MM-DD)"
    return value, None


def _validate_findings(raw: Any) -> Tuple[List[Dict[str, str]], Optional[str]]:
    """Validate the findings list. Returns (entries, error).

    Every entry is validated in full; errors name the field and the entry
    index, never the rejected value. Severity labels outside the closed
    vocabulary are refused, not downgraded.
    """
    if not isinstance(raw, list):
        return [], "PreProcessNode: findings must be a list of finding records"
    if not raw:
        return [], "PreProcessNode: findings must contain at least one finding record"
    if len(raw) > _MAX_FINDINGS:
        return [], f"PreProcessNode: findings must contain {_MAX_FINDINGS} entries or fewer"

    entries: List[Dict[str, str]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            return [], f"PreProcessNode: findings[{index}] must be an object with component, observation, severity"
        component, err = _validate_text(
            item.get("component", "unspecified"), f"findings[{index}].component", _MAX_COMPONENT_CHARS
        )
        if err:
            return [], err
        observation, err = _validate_text(
            item.get("observation", "(no observation text)"), f"findings[{index}].observation", _MAX_OBSERVATION_CHARS
        )
        if err:
            return [], err
        severity_raw = item.get("severity", "low")
        if not isinstance(severity_raw, str) or severity_raw.lower().strip() not in _SEVERITY_ALIASES:
            allowed = ", ".join(sorted(set(_SEVERITY_ALIASES)))
            return [], f"PreProcessNode: findings[{index}].severity must be one of: {allowed}"
        entries.append(
            {
                "component": component or "unspecified",
                "observation": observation or "",
                "severity": _SEVERITY_ALIASES[severity_raw.lower().strip()],
            }
        )
    return entries, None


def _validate_string_list(
    raw: Any,
    field: str,
    max_entries: int,
    item_validator: Any,
    item_limit: int,
) -> Tuple[List[str], Optional[str]]:
    """Validate a list of bounded strings. Returns (values, error)."""
    if raw is None:
        return [], None
    if not isinstance(raw, list):
        return [], f"PreProcessNode: {field} must be a list"
    if len(raw) > max_entries:
        return [], f"PreProcessNode: {field} must contain {max_entries} entries or fewer"
    values: List[str] = []
    for index, item in enumerate(raw):
        value, err = item_validator(item, f"{field}[{index}]", item_limit)
        if err:
            return [], err
        values.append(value)
    return values, None


class PreProcessNode(FunctionNode):
    """Trust gate and caller-data contract for MFG-C2-014.

    Validates the caller-supplied inspection record before the domain workflow
    runs. This is the outer backbone's pre_process slot — the only node with
    VERIFIED_EXTERNAL trust, so unauthenticated or anonymous callers are
    rejected here (fail-fast; inner domain nodes carry ANONYMOUS trust and
    never see untrusted input directly).

    The refusal rules live in this node rather than depending on a platform
    gate: the agent must behave the same way wherever it runs, so the
    instruction-override screen, the contact-identifier screen and every field
    bound are enforced here, in execute(), and proven by driving execute()
    directly in tests.

    Input state keys:
        user_input:    str   — fallback channel: JSON inspection record
        input_context: dict  — structured channel: {"inspection": record, "channel": slug}

    Output state keys (partial dict):
        inspection_payload: str        — JSON-serialised validated record
        validated_input:    str        — inert request summary (identifier text only)
        enriched_context:   str        — JSON-serialised channel metadata
        status:             str        — AgentStatus.SUCCESS.value or AgentStatus.ERROR.value
        error_log:          list[str]  — set only on ERROR
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def _reject(self, state: AgentState, reason: str, message: str) -> Dict[str, Any]:
        """Fail closed, naming the field — never the rejected value."""
        logger.warning("PreProcessNode: %s", message)
        emit_trace_event("pre_process_validation_failed", {"reason": reason}, state)
        return {
            "status": AgentStatus.ERROR.value,
            "error_log": [f"PreProcessNode: {message}"],
        }

    def execute(self, state: AgentState, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        user_input = state.get("user_input", "")
        input_context = state.get("input_context") or {}
        if not isinstance(input_context, dict):
            return self._reject(state, "input_context_not_object", "input_context must be a JSON object")

        # ── Channel selection ────────────────────────────────────────────────
        payload: Any = input_context.get("inspection")
        from_fallback_channel = False
        if payload is None:
            # Fallback: the record rides in user_input as a JSON object.
            if not user_input or not isinstance(user_input, str) or not user_input.strip():
                return self._reject(state, "empty_input", "user_input is empty or missing")
            if len(user_input) > _MAX_PAYLOAD_CHARS:
                return self._reject(state, "payload_too_large", "user_input exceeds the maximum request size")
            try:
                payload = json.loads(user_input.strip())
            except (json.JSONDecodeError, ValueError):
                return self._reject(state, "json_parse_error", "user_input must be a valid JSON object")
            from_fallback_channel = True

        if not isinstance(payload, dict):
            return self._reject(state, "payload_not_object", "the inspection record must be a JSON object")

        # ── Transit integrity (fallback channel only) ────────────────────────
        # The platform's string-channel masking rewrites two-word Title-Case
        # labels before this node runs. A masked record would name the wrong
        # components in the finished report, so it is refused, never repaired.
        if from_fallback_channel and _contains_mask_sentinel(payload):
            return self._reject(
                state,
                "record_altered_in_transit",
                "the inspection record was altered in transit; " "submit it on the structured input_context channel",
            )

        # ── Instruction-override screen (post-parse, keys included) ─────────
        override_path = _scan_for_instruction_override(payload, "")
        if override_path is not None:
            return self._reject(
                state,
                "instruction_override_content",
                f"{override_path} refused - instruction-override content",
            )

        # ── Required keys ────────────────────────────────────────────────────
        missing = _REQUIRED_INSPECTION_KEYS - payload.keys()
        if missing:
            return self._reject(
                state,
                "missing_required_fields",
                f"missing required fields: {sorted(missing)}",
            )

        # ── Field validation (fail closed; never echo the value) ─────────────
        equipment_id, err = _validate_identifier(payload.get("equipment_id"), "equipment_id", _EQUIPMENT_ID_RE, 64)
        if err:
            return self._reject(state, "invalid_equipment_id", err)

        equipment_class_raw = payload.get("equipment_class", "general")
        if (
            not isinstance(equipment_class_raw, str)
            or not (_SLUG_RE.fullmatch(equipment_class_raw) or _TEXT_RE.fullmatch(equipment_class_raw))
            or len(equipment_class_raw) > 32
        ):
            return self._reject(
                state,
                "invalid_equipment_class",
                "equipment_class must be 32 characters or fewer of plain text",
            )
        equipment_class = equipment_class_raw.strip() or "general"

        inspection_date, err = _validate_date(payload.get("inspection_date"), "inspection_date")
        if err:
            return self._reject(state, "invalid_inspection_date", err)

        next_maintenance_date: Optional[str] = None
        if payload.get("next_maintenance_date") not in (None, ""):
            next_maintenance_date, err = _validate_date(payload.get("next_maintenance_date"), "next_maintenance_date")
            if err:
                return self._reject(state, "invalid_next_maintenance_date", err)

        inspector = "unassigned"
        if payload.get("inspector") not in (None, ""):
            inspector_value, err = _validate_text(payload.get("inspector"), "inspector", _MAX_INSPECTOR_CHARS)
            if err:
                return self._reject(state, "invalid_inspector", err)
            inspector = inspector_value or "unassigned"

        findings, err = _validate_findings(payload.get("findings"))
        if err:
            return self._reject(state, "invalid_findings", err)

        fault_codes, err = _validate_string_list(
            payload.get("fault_codes"),
            "fault_codes",
            _MAX_FAULT_CODES,
            lambda v, f, limit: _validate_identifier(v, f, _FAULT_CODE_RE, limit),
            32,
        )
        if err:
            return self._reject(state, "invalid_fault_codes", err)

        corrective_actions, err = _validate_string_list(
            payload.get("corrective_actions"),
            "corrective_actions",
            _MAX_CORRECTIVE_ACTIONS,
            _validate_text,
            _MAX_ACTION_CHARS,
        )
        if err:
            return self._reject(state, "invalid_corrective_actions", err)

        # ── Channel slug ─────────────────────────────────────────────────────
        channel = input_context.get("channel", _DEFAULT_CHANNEL)
        if not isinstance(channel, str) or not _SLUG_RE.fullmatch(channel):
            return self._reject(
                state,
                "invalid_channel",
                "channel must be 32 characters or fewer of lowercase letters, digits or '_'",
            )

        # ── Success ──────────────────────────────────────────────────────────
        record: Dict[str, Any] = {
            "equipment_id": equipment_id,
            "equipment_class": equipment_class,
            "inspection_date": inspection_date,
            "findings": findings,
            "fault_codes": fault_codes,
            "corrective_actions": corrective_actions,
            "next_maintenance_date": next_maintenance_date,
            "inspector": inspector,
        }

        logger.info(
            "PreProcessNode: validated equipment_id=%s findings=%d fault_codes=%d channel=%s",
            equipment_id,
            len(findings),
            len(fault_codes),
            channel,
        )
        emit_trace_event(
            "pre_process_validated",
            {
                "equipment_id": equipment_id,
                "finding_count": len(findings),
                "fault_code_count": len(fault_codes),
                "channel": channel,
                "structured_channel": not from_fallback_channel,
            },
            state,
        )

        return {
            "inspection_payload": to_json(record),
            # Inert summary only — the string channel is scanned and masked at
            # every node boundary, so nothing label-shaped travels on it.
            "validated_input": f"maintenance report request for equipment {equipment_id}",
            "enriched_context": to_json(
                {
                    "source": "MaintenanceReportGeneratorAgent",
                    "channel": channel,
                    "equipment_id": equipment_id,
                }
            ),
            "status": AgentStatus.SUCCESS.value,
        }
