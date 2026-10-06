# MFG-C2-014 — External-output boundary (PostProcessNode)
#
# The report crosses the boundary in two representations (rendered text +
# structured section/compliance data) and the gate walks BOTH, to any nesting
# depth. This template renders no monetary aggregate, so it carries no
# rounding grid; the invariant enforced in its place is the opposite one —
# a precision identifier reaches the reader byte-identical — plus the
# 労働安全衛生法 Art 45 statutory determination, the credential scan, verbatim
# re-emission redaction, and the size cap.

import json

import pytest

from framework.errors import SecurityViolationError
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from framework.security.credential_detector import detect_credentials
from src.nodes.post_process_node import (
    PostProcessNode,
    _security_gate_output,
    _walk_strings,
)
from src.schemas.state import to_json


@pytest.fixture(autouse=True)
def patch_emit(monkeypatch):
    monkeypatch.setattr("src.nodes.post_process_node.emit_trace_event", lambda *a, **k: None)


# Minimal production-shaped validated record. Every state fixture carries one:
# without it the boundary cannot make the Art 45 determination and withholds
# the response outright, which would let a test pass on the withholding path
# instead of on the layer it means to exercise.
_BASE_RECORD = {
    "equipment_class": "general",
    "findings": [{"component": "pump seal", "observation": "light seepage", "severity": "low"}],
    "fault_codes": [],
}


def _state(report: str, *, sections=None, compliance=None, record=None, **extra) -> dict:
    """Outer-graph state as PostProcessNode really receives it.

    The validated record arrives under `inspection_payload` — PreProcessNode
    writes that key and it is present in the OUTER state. `inspection_data` is
    the INNER graph's key and merge_output() does not map it outward, so a
    fixture built on inspection_data exercises a state shape that never occurs
    on the real invoke path (and left the identifier-integrity layer comparing
    against {} in production while these tests stayed green).

    `compliance` seeds a STALE inner compliance_flags value. The gate must not
    read it — the determination is the boundary's own — so it is here only so
    that tests can prove it is ignored.
    """
    state = {
        "maintenance_report": report,
        "report_sections": to_json(sections) if sections is not None else None,
        "compliance_flags": to_json(compliance) if compliance is not None else None,
        "inspection_payload": to_json(record if record is not None else _BASE_RECORD),
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
    }
    state.update(extra)
    return state


class TestNestedCredentialScan:
    def setup_method(self):
        self.node = PostProcessNode()

    def test_top_level_control_gate_fires_on_plain_text(self):
        """The control that proves the verifier itself works: a credential in
        the flat report text is caught."""
        result = self.node.execute(_state("report with Bearer abcdefgh12345678 inside"))
        assert result["status"] == AgentStatus.ERROR.value
        assert "Bearer abcdefgh12345678" not in str(result)

    def test_nested_leak_in_report_sections_is_caught(self):
        """Caller-derived text rides nested inside the structured sections —
        a top-level-only scan would see nothing here."""
        sections = {
            "inspection_findings": {
                "rows": ["1. [HIGH] control cabinet: api_key = sk-abcdefghij0123456789ABCD"],
            },
        }
        result = self.node.execute(_state("clean rendered text", sections=sections))
        assert result["status"] == AgentStatus.ERROR.value
        assert "sk-abcdefghij0123456789ABCD" not in str(result)

    def test_the_boundary_determination_is_one_of_the_scanned_representations(self):
        """The gate walks its own Art 45 determination, not a state-supplied one.

        The determination is built here from the validated record, so this
        pins that the representation is scanned at all — a shape the walk
        skipped would be a representation the gate does not protect.
        """
        from src.nodes.post_process_node import _art45_determination

        determination = _art45_determination({"equipment_class": "crane", "fault_codes": ["E-204"]})
        assert _security_gate_output(determination) is None
        poisoned = dict(determination, reason={"detail": ["password: hunter2secret99"]})
        assert _security_gate_output(poisoned) is not None

    def test_leak_in_a_dict_key_is_caught(self):
        sections = {"eyJhbGciOi.eyJzdWIiOi.c2lnbmF0dXJl": "value"}
        result = self.node.execute(_state("clean rendered text", sections=sections))
        assert result["status"] == AgentStatus.ERROR.value

    def test_walk_strings_reaches_every_depth(self):
        payload = {"a": ["x", {"b": ("y", "z")}], "k": "v"}
        found = {s for _, s in _walk_strings(payload)}
        assert found == {"a", "x", "b", "y", "z", "k", "v"}

    def test_helper_detects_and_clears(self):
        assert _security_gate_output("sk-abcdefghij0123456789ABCDEF") is not None
        assert _security_gate_output({"nested": ["Bearer abcdefgh12345678"]}) is not None
        assert _security_gate_output("A perfectly clean maintenance report.") is None
        assert _security_gate_output({"sections": ["clean text"]}) is None


class TestIdentifierIntegrity:
    """No rounding grid applies (nothing monetary is rendered); the boundary
    instead re-checks that precision identifiers reach the reader unchanged."""

    def setup_method(self):
        self.node = PostProcessNode()

    @pytest.mark.parametrize(
        "equipment_id,fault_codes",
        [
            ("MFG-EQ-CRANE-014", ["E-204", "E-118"]),
            ("SKF-6205", []),  # letter-digit part-number shape
            ("sku_48210", ["F-9"]),  # underscore alphabet
            ("48210", []),  # pure-numeric code — no letters to protect it
        ],
    )
    def test_identifiers_render_byte_identical(self, equipment_id, fault_codes):
        record = {"equipment_id": equipment_id, "fault_codes": fault_codes}
        report = "EQUIPMENT MAINTENANCE REPORT\n" f"Equipment ID: {equipment_id}\n" + "".join(
            f"  - {c}\n" for c in fault_codes
        )
        result = self.node.execute(_state(report, record=record))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert f"Equipment ID: {equipment_id}" in result["formatted_output"]
        for code in fault_codes:
            assert f"- {code}" in result["formatted_output"]

    def test_altered_identifier_withholds_the_report(self):
        """A report whose equipment ID no longer matches the validated record
        (rewritten, masked, digit-grouped) is withheld, not shipped."""
        record = {"equipment_id": "MFG-EQ-CRANE-014", "fault_codes": ["E-204"]}
        report = "EQUIPMENT MAINTENANCE REPORT\nEquipment ID: MFG-EQ-CRANE-14,000\n  - E-204"
        result = self.node.execute(_state(report, record=record))
        assert result["status"] == AgentStatus.ERROR.value
        assert any("equipment_id" in e for e in result["error_log"])

    def test_missing_fault_code_withholds_the_report(self):
        record = {"equipment_id": "PUMP-4021", "fault_codes": ["E-204", "E-118"]}
        report = "EQUIPMENT MAINTENANCE REPORT\nEquipment ID: PUMP-4021\n  - E-204"
        result = self.node.execute(_state(report, record=record))
        assert result["status"] == AgentStatus.ERROR.value
        assert any("fault_codes[1]" in e for e in result["error_log"])


class TestArt45DeterminationAtTheBoundary:
    """The 労働安全衛生法 Art 45 determination is made HERE, not by a graph node.

    It used to be ComplianceCheckNode, one node in the inner linear pipeline —
    the `compliance_check_node` shape the platform architecture rules name an anti-pattern.
    Its output reached the reader through a renderer whose
    `compliance.get("safety_record_required", False)` default turned a missing
    determination into a rendered "Formal Record Required: NO" shipped as
    status=success: a statutorily false statement with no signal to the reader.
    """

    def setup_method(self):
        self.node = PostProcessNode()

    _CRANE = {
        "equipment_id": "MFG-EQ-CRANE-014",
        "equipment_class": "crane",
        "findings": [{"component": "hoist wire rope", "observation": "fraying", "severity": "high"}],
        "fault_codes": ["E-204"],
    }
    _CLEAN_GENERAL = {
        "equipment_id": "PUMP-4021",
        "equipment_class": "general",
        "findings": [{"component": "pump seal", "observation": "light seepage", "severity": "low"}],
        "fault_codes": [],
    }

    def _report(self, record) -> str:
        codes = "".join(f"  - {c}\n" for c in record["fault_codes"])
        return f"EQUIPMENT MAINTENANCE REPORT\nEquipment ID: {record['equipment_id']}\n{codes}"

    def test_gate_renders_the_note_the_report_body_does_not_carry(self):
        record = self._CRANE
        body = self._report(record)
        result = self.node.execute(_state(body, record=record))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "REGULATORY COMPLIANCE NOTE" not in body, "the body must arrive without a determination"
        assert "REGULATORY COMPLIANCE NOTE" in result["formatted_output"]
        assert "Formal Record Required:    YES — retain for 3 years" in result["formatted_output"]

    def test_not_required_for_clean_general_equipment(self):
        record = self._CLEAN_GENERAL
        result = self.node.execute(_state(self._report(record), record=record))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "Formal Record Required:    NO" in result["formatted_output"]
        assert json.loads(result["compliance_flags"])["safety_record_required"] is False
        assert result["safety_record_required"] is False

    def test_determination_is_surfaced_as_the_gates_own_output(self):
        record = self._CRANE
        result = self.node.execute(_state(self._report(record), record=record))
        flags = json.loads(result["compliance_flags"])
        assert result["safety_record_required"] is True
        assert flags["safety_record_required"] is True
        assert flags["retention_years"] == 3
        assert "Article 45" in flags["regulatory_basis"]

    def test_a_stale_inner_compliance_flags_value_is_not_trusted(self):
        """The state key the retired node used to write is ignored.

        A pipeline that still wrote compliance_flags could otherwise dictate the
        determination the reader acts on — which is the bypass, restated.
        """
        record = self._CRANE
        result = self.node.execute(
            _state(
                self._report(record),
                record=record,
                compliance={"safety_record_required": False, "reason": "stale inner value", "retention_years": 3},
            )
        )
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["safety_record_required"] is True
        assert "Formal Record Required:    YES — retain for 3 years" in result["formatted_output"]
        assert "stale inner value" not in result["formatted_output"]

    @pytest.mark.parametrize(
        "record,expected",
        [
            ({"equipment_class": "boiler", "findings": [], "fault_codes": []}, True),
            ({"equipment_class": "特定機械", "findings": [], "fault_codes": []}, True),
            (
                {
                    "equipment_class": "general",
                    "findings": [{"severity": "critical"}],
                    "fault_codes": [],
                },
                True,
            ),
            ({"equipment_class": "general", "findings": [], "fault_codes": ["E-9"]}, True),
            (
                {
                    "equipment_class": "general",
                    "findings": [{"severity": "medium"}, {"severity": "low"}],
                    "fault_codes": [],
                },
                False,
            ),
        ],
    )
    def test_rule_set_is_preserved_from_the_retired_node(self, record, expected):
        """The rule itself is unchanged — only where it is evaluated moved."""
        from src.nodes.post_process_node import _art45_determination

        assert _art45_determination(record)["safety_record_required"] is expected

    def test_determination_is_derived_from_the_record_not_a_pipeline_field(self):
        """The boundary derives its own inputs.

        A determination that reads an upstream node's derived field
        (is_specified_machinery / max_severity) is only as present as that node.
        """
        from src.nodes.post_process_node import _art45_determination

        determination = _art45_determination(
            {
                "equipment_class": "crane",
                "is_specified_machinery": False,  # upstream says no — ignored
                "max_severity": "low",  # upstream says low — ignored
                "findings": [{"severity": "high"}],
                "fault_codes": [],
            }
        )
        assert determination["is_specified_machinery"] is True
        assert determination["max_severity"] == "high"

    def test_no_record_withholds_rather_than_defaulting_to_NO(self):
        """Fail closed. This is the inversion the fix buys.

        Before: no determination -> renderer default -> "Formal Record Required:
        NO", status=success. After: the response is withheld.
        """
        state = {
            "maintenance_report": "EQUIPMENT MAINTENANCE REPORT\nEquipment ID: MFG-EQ-CRANE-014",
            "caller_trust_level": TrustLevel.ANONYMOUS.value,
        }
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert any("inspection_payload" in e for e in result["error_log"])
        assert "Formal Record Required" not in result["formatted_output"]
        assert result["compliance_flags"] is None
        assert result["safety_record_required"] is None


class TestVerbatimRedaction:
    def setup_method(self):
        self.node = PostProcessNode()

    def test_raw_request_embedding_is_redacted(self):
        raw = json.dumps({"equipment_id": "X-1", "findings": ["a" * 40]})
        report = f"EQUIPMENT MAINTENANCE REPORT\nDEBUG DUMP: {raw}\nEnd."
        result = self.node.execute(_state(report, user_input=raw))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert raw not in result["formatted_output"]
        assert "[REDACTED]" in result["formatted_output"]

    def test_serialised_record_embedding_is_redacted(self):
        """The bulk dump is redacted while the properly rendered identifier
        survives.

        Redaction (layer 2) runs BEFORE identifier integrity (layer 3), so the
        report must still render `Equipment ID: X-1` in its own right — a report
        whose only copy of the identifier was inside the redacted dump is
        correctly withheld by layer 3, which is asserted separately below.
        """
        payload = to_json({"equipment_id": "X-1", "inspector": "Kenji Tanaka"})
        report = f"EQUIPMENT MAINTENANCE REPORT\nEquipment ID: X-1\nRecord: {payload}"
        result = self.node.execute(_state(report, inspection_payload=payload))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert payload not in result["formatted_output"]
        assert "[REDACTED]" in result["formatted_output"]
        assert "Equipment ID: X-1" in result["formatted_output"]

    def test_redaction_that_erases_the_identifier_withholds_the_report(self):
        """Layers 2 and 3 in combination: when redaction is the thing that
        removes the equipment ID, the report is withheld rather than shipped
        with an unidentifiable subject — the case that reaches the gate through
        the real /invoke path when a caller's `input` equals their own
        equipment ID."""
        payload = to_json({"equipment_id": "X-1-LONG-IDENTIFIER-VALUE", "inspector": "Kenji Tanaka"})
        report = f"EQUIPMENT MAINTENANCE REPORT\nRecord: {payload}"
        result = self.node.execute(_state(report, inspection_payload=payload))
        assert result["status"] == AgentStatus.ERROR.value
        assert any("equipment_id" in e for e in result["error_log"])


class TestSizeCap:
    def setup_method(self):
        self.node = PostProcessNode()

    def test_oversized_report_is_truncated(self):
        record = {"equipment_id": "X-1", "fault_codes": []}
        report = "EQUIPMENT MAINTENANCE REPORT\nEquipment ID: X-1\n" + ("y" * 150_000)
        result = self.node.execute(_state(report, record=record))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert len(result["formatted_output"]) <= 100_000
        assert result["formatted_output"].endswith("[Output truncated at the external boundary]")


class TestAgentClassGate:
    def test_agent_gate_raises_on_credential(self):
        from src.graph.graph import MaintenanceReportGeneratorAgent

        agent = MaintenanceReportGeneratorAgent()
        with pytest.raises(SecurityViolationError):
            agent._security_gate_output("report with Bearer abcdefgh12345678")

    def test_agent_gate_clears_clean_text(self):
        from src.graph.graph import MaintenanceReportGeneratorAgent

        agent = MaintenanceReportGeneratorAgent()
        assert agent._security_gate_output("A clean maintenance report.") is None


class TestIntegrityLayerReadsOuterState:
    """The identifier-integrity layer must read a key that EXISTS in outer state.

    Regression: the layer read `inspection_data`, an inner-graph key that
    MaintenanceReportGraphNode.merge_output() does not map outward. On the real
    invoke path it therefore always compared against {} and never fired —
    measured: a report whose equipment ID had been erased shipped as SUCCESS.
    """

    def setup_method(self):
        self.node = PostProcessNode()

    def test_integrity_fires_on_the_production_key(self):
        record = {"equipment_id": "MFG-EQ-CRANE-014-ALPHA", "fault_codes": []}
        state = {
            "maintenance_report": "EQUIPMENT MAINTENANCE REPORT\nEquipment ID: [REDACTED]",
            "inspection_payload": to_json(record),
            "caller_trust_level": TrustLevel.ANONYMOUS.value,
        }
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert any("equipment_id" in e for e in result["error_log"])

    def test_inner_key_still_honoured_for_direct_execute(self):
        record = {"equipment_id": "PUMP-4021", "fault_codes": ["E-204"]}
        state = {
            "maintenance_report": "EQUIPMENT MAINTENANCE REPORT\nEquipment ID: PUMP-4021",
            "inspection_data": to_json(record),
            "caller_trust_level": TrustLevel.ANONYMOUS.value,
        }
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert any("fault_codes[0]" in e for e in result["error_log"])


class TestDetectorParityWithFramework:
    """The gate must block at least everything the FRAMEWORK would refuse.

    FunctionNode scans every node result with detect_credentials() and RAISES on
    a hit — and a raise discards the node's return, taking the containment in
    _withhold() with it. A shape the framework catches and this gate misses is
    therefore a containment bypass, not merely a narrower gate.
    """

    @pytest.mark.parametrize(
        "shape",
        [
            "sk_live_" + "a" * 20,  # stripe secret key
            "sk_test_" + "b" * 20,  # stripe test key
            "sk-" + "c" * 24,  # generic API key
            "eyJ" + "d" * 20,  # JWT, no dots
            "AKIA" + "E" * 16,  # AWS access key id
            "Bearer " + "f" * 24,  # bearer token
            "postgresql://" + "user:pw@host:5432/plant",  # connection string
            "mongodb://" + "user:pw@host:27017/db",
        ],
    )
    def test_blocks_every_shape_the_framework_would_refuse(self, shape):
        # Control that keeps the parametrization honest: each shape really is
        # one the framework refuses.
        assert detect_credentials(shape), "probe shape is not a credential the framework refuses"
        violation = _security_gate_output({"observation": shape})
        assert violation is not None, f"gate missed a framework-recognised shape: {shape[:12]}..."

    def test_local_set_still_covers_what_the_framework_does_not(self):
        """The local patterns EXTEND the framework recognizer; they must not be
        lost when the two are unioned."""
        assert detect_credentials("password: hunter2secret99") == []
        assert _security_gate_output({"note": "password: hunter2secret99"}) is not None

    def test_ordinary_report_text_is_not_flagged(self):
        """The other direction — real maintenance content passes untouched."""
        assert (
            _security_gate_output(
                {
                    "equipment_summary": "Equipment ID: MFG-EQ-CRANE-014\nClass: crane",
                    "findings": ["1. [HIGH] Hoist Wire Rope: strand fraying near the drum flange."],
                    "fault_codes": ["E-204", "E-118"],
                }
            )
            is None
        )


class TestOutputGateContainment:
    """Blocking a report must also CONTAIN it.

    The envelope resolves the caller-facing value as `formatted_output or
    result` with no regard for status, so a gate that returns ERROR while
    leaving those fields populated still ships the refused report inside the
    error envelope. These pin the clearing itself; the end-to-end consequence is
    pinned on the real /invoke surface in tests/proof_of_boundary.
    """

    def setup_method(self):
        self.node = PostProcessNode()

    def _blocked(self) -> dict:
        record = {"equipment_id": "MFG-EQ-CRANE-014-ALPHA", "fault_codes": ["E-204"]}
        report = (
            "EQUIPMENT MAINTENANCE REPORT\nEquipment ID: [REDACTED]\n"
            "  - E-204\nInspector: Kenji Tanaka\nObservation: strand fraying"
        )
        return self.node.execute(
            _state(report, record=record, sections={"a": "b"}, compliance={"safety_record_required": True})
        )

    def test_block_clears_every_report_bearing_field(self):
        result = self._blocked()
        assert result["status"] == AgentStatus.ERROR.value
        # Presence AND emptiness: LangGraph merges partial deltas, so a key
        # merely OMITTED from the return leaves the old value standing in state
        # — `not result.get(field)` would pass on a gate that clears nothing.
        for field in ("maintenance_report", "report_sections", "compliance_flags", "safety_record_required"):
            assert field in result, f"{field} omitted from the block return — state keeps its old value"
        assert result["maintenance_report"] == ""
        assert result["report_sections"] is None
        assert result["compliance_flags"] is None
        assert result["safety_record_required"] is None

    def test_the_replacement_defeats_the_envelope_fallback(self):
        """`formatted_output or result` must resolve to the withholding notice.

        A falsy replacement ("" or {}) hands the resolution straight back to
        `result` — the exact hole the clearing closes.
        """
        result = self._blocked()
        assert result["formatted_output"], "the replacement must be TRUTHY — see the fallback"
        resolved = result["formatted_output"] or result["result"]
        assert resolved is result["formatted_output"]
        assert "WITHHELD" in resolved

    def test_block_releases_no_report_content(self):
        blob = json.dumps(self._blocked())
        for released in ("EQUIPMENT MAINTENANCE REPORT", "E-204", "Kenji Tanaka", "strand fraying"):
            assert released not in blob, f"{released!r} released on the block path"

    def test_violation_names_a_location_never_a_value(self):
        """Echoing the matched value would put the refused string back into this
        node's own result, where the framework credential scan raises — and a
        raise DISCARDS the whole return, so the clearing above would never be
        applied at all."""
        bearer = "Bearer " + "e" * 24
        result = self.node(_state("clean text", sections={"agenda": bearer}))
        assert result["status"] == AgentStatus.ERROR.value
        blob = json.dumps(result)
        assert bearer not in blob
        assert "Traceback" not in blob, "the framework scan discarded the clearing return"
        assert result["maintenance_report"] == ""

    _MAY_SURVIVE_A_BLOCK = {
        "formatted_output",  # the truthy withholding notice
        "result",  # the same notice
        "status",  # ERROR
        "error_log",  # location names only
        "maintenance_report",
        "report_sections",
        "compliance_flags",
        "safety_record_required",  # cleared to None — the boundary's own field
    }

    def test_inventory_guard_no_new_field_joins_the_block_return(self):
        """Pin the block return's key set.

        A future field carrying report text could otherwise be added upstream
        and quietly ride out through the block path without anyone noticing.
        """
        keys = set(self._blocked())
        unexpected = keys - self._MAY_SURVIVE_A_BLOCK
        assert not unexpected, (
            f"new field(s) on the block return: {sorted(unexpected)} — "
            "confirm they carry no report content, then add them to the inventory"
        )
