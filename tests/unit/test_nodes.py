# MFG-C2-014 — Unit Tests: domain nodes + graph wiring
#
# Real, non-stub unit tests. They import the REAL modules and assert real
# behaviour (report content, 労働安全衛生法 Art 45 thresholds, trust levels,
# the output gate, and the Category 2 two-layer graph composition).
#
# Audit events are patched at the node MODULE level (not via a sys.modules
# stub, which would break the real `shared` package the framework loads at import
# time). Patch pattern per node:
#     monkeypatch.setattr("src.nodes.<mod>.emit_trace_event", lambda *a, **k: None)

import json

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from src.schemas.state import from_json, to_json


# ── Canonical valid payload (kept identical to PB-6 _VALID_PAYLOAD) ────────────

_VALID_PAYLOAD_DICT = {
    "equipment_id": "MFG-EQ-CRANE-014",
    "equipment_class": "crane",
    "inspection_date": "2026-07-12",
    "inspector": "K. Tanaka",
    "findings": [
        {
            "component": "hoist wire rope",
            "observation": "Visible strand fraying near the drum flange; localized corrosion on the outer wires.",
            "severity": "high",
        },
        {
            "component": "upper-travel limit switch",
            "observation": "Delayed actuation observed under rated load during the travel test.",
            "severity": "medium",
        },
    ],
    "fault_codes": ["E-204", "E-118"],
    "corrective_actions": [
        "Replace the hoist wire rope assembly and re-test the rated load per JIS B 8821.",
        "Recalibrate the upper-travel limit switch and re-run the travel interlock test.",
    ],
    "next_maintenance_date": "2026-10-12",
}

VALID_PAYLOAD = json.dumps(_VALID_PAYLOAD_DICT)


def _inspection_payload(**overrides) -> dict:
    """A complete, valid raw inspection payload (as a caller would POST)."""
    payload = json.loads(VALID_PAYLOAD)
    payload.update(overrides)
    return payload


def _inspection_data(
    max_severity: str = "high",
    fault_codes=None,
    equipment_class: str = "crane",
    findings=None,
    **overrides,
) -> dict:
    """The normalised inspection_data dict shape produced by InputValidateNode
    (i.e. the input the downstream inner nodes consume)."""
    data = {
        "equipment_id": "MFG-EQ-CRANE-014",
        "equipment_class": equipment_class,
        "inspection_date": "2026-07-12",
        "findings": findings
        if findings is not None
        else [
            {"component": "hoist wire rope", "observation": "fraying", "severity": max_severity},
        ],
        "fault_codes": ["E-204", "E-118"] if fault_codes is None else fault_codes,
        "corrective_actions": ["Replace the hoist wire rope assembly."],
        "next_maintenance_date": "2026-10-12",
        "inspector": "K. Tanaka",
        "finding_count": 1,
        "max_severity": max_severity,
    }
    data.update(overrides)
    return data


# ── PreProcessNode (outer pre_process, VERIFIED_EXTERNAL trust) ────────────────


class TestPreProcessNode:
    @pytest.fixture(autouse=True)
    def patch_emit(self, monkeypatch):
        monkeypatch.setattr("src.nodes.pre_process_node.emit_trace_event", lambda *a, **k: None)

    def setup_method(self):
        from src.nodes.pre_process_node import PreProcessNode

        self.node = PreProcessNode()

    def test_valid_payload_returns_success(self):
        result = self.node(
            {"user_input": VALID_PAYLOAD, "input_context": {}, "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value}
        )
        assert result["status"] == AgentStatus.SUCCESS.value
        # State status must be the `.value` string, never a bare enum. Exact
        # type on purpose: a str-mixin enum passes isinstance(str) and must
        # fail here.
        assert type(result["status"]) is str  # noqa: E721
        assert result["validated_input"] is not None
        # The validated record travels on its own state key, not the string channel.
        assert json.loads(result["inspection_payload"])["equipment_id"] == "MFG-EQ-CRANE-014"

    def test_enriched_context_carries_equipment_id(self):
        result = self.node(
            {
                "user_input": VALID_PAYLOAD,
                "input_context": {"channel": "cmms"},
                "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value,
            }
        )
        ctx = from_json(result["enriched_context"])
        assert ctx["equipment_id"] == "MFG-EQ-CRANE-014"
        assert ctx["channel"] == "cmms"
        assert ctx["source"] == "MaintenanceReportGeneratorAgent"

    def test_empty_input_returns_error(self):
        result = self.node(
            {"user_input": "", "input_context": {}, "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value}
        )
        assert result["status"] == AgentStatus.ERROR.value
        assert any("empty" in e for e in result["error_log"])

    def test_invalid_json_returns_error(self):
        result = self.node(
            {
                "user_input": "{not valid json}",
                "input_context": {},
                "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value,
            }
        )
        assert result["status"] == AgentStatus.ERROR.value
        assert any("JSON" in e or "json" in e for e in result["error_log"])

    def test_non_object_json_returns_error(self):
        result = self.node(
            {"user_input": "[1, 2, 3]", "input_context": {}, "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value}
        )
        assert result["status"] == AgentStatus.ERROR.value
        assert any("object" in e for e in result["error_log"])

    def test_missing_required_field_returns_error(self):
        payload = {"equipment_id": "X", "inspection_date": "2026-07-12"}  # no 'findings'
        result = self.node(
            {
                "user_input": json.dumps(payload),
                "input_context": {},
                "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value,
            }
        )
        assert result["status"] == AgentStatus.ERROR.value
        assert any("findings" in e for e in result["error_log"])

    def test_trust_level_is_verified_external(self):
        assert self.node.required_trust_level == TrustLevel.VERIFIED_EXTERNAL

    def test_execute_signature_is_state_first(self):
        import inspect
        from src.nodes.pre_process_node import PreProcessNode

        params = list(inspect.signature(PreProcessNode.execute).parameters.keys())
        assert params[0] == "self" and params[1] == "state"
        assert "_invoke_impl" not in PreProcessNode.__dict__


# ── InputValidateNode (inner domain node 1, ANONYMOUS) ─────────────────────────


class TestInputValidateNode:
    @pytest.fixture(autouse=True)
    def patch_emit(self, monkeypatch):
        monkeypatch.setattr("src.nodes.input_validate_node.emit_trace_event", lambda *a, **k: None)

    def setup_method(self):
        from src.nodes.input_validate_node import InputValidateNode

        self.node = InputValidateNode()

    def test_valid_input_builds_inspection_data(self):
        result = self.node({"validated_input": VALID_PAYLOAD, "caller_trust_level": TrustLevel.ANONYMOUS.value})
        assert result["status"] == AgentStatus.SUCCESS.value
        data = from_json(result["inspection_data"])
        assert data["equipment_id"] == "MFG-EQ-CRANE-014"
        assert data["equipment_class"] == "crane"
        assert data["finding_count"] == 2
        assert data["fault_codes"] == ["E-204", "E-118"]

    def test_max_severity_is_highest_present(self):
        # findings severities high + medium ⇒ max_severity high
        data = from_json(
            self.node({"validated_input": VALID_PAYLOAD, "caller_trust_level": TrustLevel.ANONYMOUS.value})[
                "inspection_data"
            ]
        )
        assert data["max_severity"] == "high"

    def test_severity_aliases_are_normalised(self):
        payload = _inspection_payload(
            findings=[
                {"component": "a", "observation": "x", "severity": "crit"},
                {"component": "b", "observation": "y", "severity": "major"},
                {"component": "c", "observation": "z", "severity": "moderate"},
                {"component": "d", "observation": "w", "severity": "minor"},
            ]
        )
        data = from_json(
            self.node({"validated_input": json.dumps(payload), "caller_trust_level": TrustLevel.ANONYMOUS.value})[
                "inspection_data"
            ]
        )
        sevs = [f["severity"] for f in data["findings"]]
        assert sevs == ["critical", "high", "medium", "low"]
        assert data["max_severity"] == "critical"

    def test_unknown_severity_defaults_to_low(self):
        payload = _inspection_payload(findings=[{"component": "a", "observation": "x", "severity": "wobbly"}])
        data = from_json(
            self.node({"validated_input": json.dumps(payload), "caller_trust_level": TrustLevel.ANONYMOUS.value})[
                "inspection_data"
            ]
        )
        assert data["findings"][0]["severity"] == "low"

    def test_free_text_finding_normalised_to_low(self):
        payload = _inspection_payload(findings=["loose bolt on the guard rail"])
        data = from_json(
            self.node({"validated_input": json.dumps(payload), "caller_trust_level": TrustLevel.ANONYMOUS.value})[
                "inspection_data"
            ]
        )
        assert data["findings"][0]["component"] == "unspecified"
        assert data["findings"][0]["observation"] == "loose bolt on the guard rail"
        assert data["findings"][0]["severity"] == "low"

    def test_falls_back_to_user_input(self):
        result = self.node({"user_input": VALID_PAYLOAD, "caller_trust_level": TrustLevel.ANONYMOUS.value})
        assert result["status"] == AgentStatus.SUCCESS.value

    def test_empty_equipment_id_returns_error(self):
        payload = _inspection_payload(equipment_id="")
        result = self.node({"validated_input": json.dumps(payload), "caller_trust_level": TrustLevel.ANONYMOUS.value})
        assert result["status"] == AgentStatus.ERROR.value
        assert any("equipment_id" in e for e in result["error_log"])

    def test_missing_inspection_date_returns_error(self):
        payload = _inspection_payload(inspection_date="")
        result = self.node({"validated_input": json.dumps(payload), "caller_trust_level": TrustLevel.ANONYMOUS.value})
        assert result["status"] == AgentStatus.ERROR.value
        assert any("inspection_date" in e for e in result["error_log"])

    def test_empty_findings_returns_error(self):
        payload = _inspection_payload(findings=[])
        result = self.node({"validated_input": json.dumps(payload), "caller_trust_level": TrustLevel.ANONYMOUS.value})
        assert result["status"] == AgentStatus.ERROR.value
        assert any("findings" in e for e in result["error_log"])

    def test_defaults_for_optional_fields(self):
        payload = {
            "equipment_id": "EQ-1",
            "inspection_date": "2026-07-12",
            "findings": [{"component": "x", "observation": "y", "severity": "low"}],
        }
        data = from_json(
            self.node({"validated_input": json.dumps(payload), "caller_trust_level": TrustLevel.ANONYMOUS.value})[
                "inspection_data"
            ]
        )
        assert data["equipment_class"] == "general"
        assert data["inspector"] == "unassigned"
        assert data["fault_codes"] == []
        assert data["next_maintenance_date"] is None

    def test_trust_level_anonymous(self):
        assert self.node.required_trust_level == TrustLevel.ANONYMOUS


# ── ParseInspectionDataNode (inner domain node 2, ANONYMOUS) ───────────────────


class TestParseInspectionDataNode:
    @pytest.fixture(autouse=True)
    def patch_emit(self, monkeypatch):
        monkeypatch.setattr("src.nodes.parse_inspection_data_node.emit_trace_event", lambda *a, **k: None)

    def setup_method(self):
        from src.nodes.parse_inspection_data_node import ParseInspectionDataNode

        self.node = ParseInspectionDataNode()

    def _run(self, **kw):
        state = {"inspection_data": to_json(_inspection_data(**kw)), "caller_trust_level": TrustLevel.ANONYMOUS.value}
        return from_json(self.node(state)["inspection_data"])

    def test_critical_by_severity(self):
        assert self._run(max_severity="critical", fault_codes=[])["criticality"] == "critical"

    def test_critical_by_fault_code_count(self):
        assert self._run(max_severity="low", fault_codes=["1", "2", "3", "4", "5"])["criticality"] == "critical"

    def test_high_by_severity(self):
        assert self._run(max_severity="high", fault_codes=[])["criticality"] == "high"

    def test_high_by_fault_codes(self):
        assert self._run(max_severity="low", fault_codes=["a", "b"])["criticality"] == "high"

    def test_medium_by_severity(self):
        assert self._run(max_severity="medium", fault_codes=[])["criticality"] == "medium"

    def test_low(self):
        assert self._run(max_severity="low", fault_codes=[])["criticality"] == "low"

    def test_specified_machinery_flag_true_for_crane(self):
        assert self._run(equipment_class="crane", max_severity="low", fault_codes=[])["is_specified_machinery"] is True

    def test_specified_machinery_flag_false_for_general(self):
        assert (
            self._run(equipment_class="general", max_severity="low", fault_codes=[])["is_specified_machinery"] is False
        )

    def test_safety_reportable_when_specified_machinery(self):
        # general low-severity no-fault equipment is NOT reportable
        assert self._run(equipment_class="general", max_severity="low", fault_codes=[])["safety_reportable"] is False
        # crane is specified machinery ⇒ reportable even with a clean inspection
        assert self._run(equipment_class="crane", max_severity="low", fault_codes=[])["safety_reportable"] is True

    def test_safety_reportable_when_high_severity(self):
        assert self._run(equipment_class="general", max_severity="high", fault_codes=[])["safety_reportable"] is True

    def test_missing_inspection_data_returns_error(self):
        result = self.node({"caller_trust_level": TrustLevel.ANONYMOUS.value})
        assert result["status"] == AgentStatus.ERROR.value

    def test_trust_level_anonymous(self):
        assert self.node.required_trust_level == TrustLevel.ANONYMOUS


# ── GenerateReportSectionsNode (inner domain node 3, ANONYMOUS) ────────────────


class TestGenerateReportSectionsNode:
    @pytest.fixture(autouse=True)
    def patch_emit(self, monkeypatch):
        monkeypatch.setattr("src.nodes.generate_report_sections_node.emit_trace_event", lambda *a, **k: None)

    def setup_method(self):
        from src.nodes.generate_report_sections_node import GenerateReportSectionsNode

        self.node = GenerateReportSectionsNode()

    def _sections(self, **kw):
        data = _inspection_data(**kw)
        data.setdefault("is_specified_machinery", True)
        data.setdefault("safety_reportable", True)
        state = {"inspection_data": to_json(data), "caller_trust_level": TrustLevel.ANONYMOUS.value}
        return from_json(self.node(state)["report_sections"])

    def test_generates_all_seven_sections(self):
        sections = self._sections()
        assert set(sections.keys()) == {
            "equipment_summary",
            "inspection_findings",
            "fault_codes",
            "corrective_actions",
            "next_maintenance_schedule",
            "compliance_status",
            "sign_off",
        }

    def test_summary_reflects_equipment_fields(self):
        sections = self._sections()
        assert "MFG-EQ-CRANE-014" in sections["equipment_summary"]
        assert "crane" in sections["equipment_summary"]

    def test_no_fault_codes_placeholder(self):
        sections = self._sections(fault_codes=[])
        assert "No fault codes reported." in sections["fault_codes"]

    def test_no_next_maintenance_placeholder(self):
        sections = self._sections(next_maintenance_date=None)
        assert "not scheduled" in sections["next_maintenance_schedule"].lower()

    def test_missing_inspection_data_returns_error(self):
        result = self.node({"caller_trust_level": TrustLevel.ANONYMOUS.value})
        assert result["status"] == AgentStatus.ERROR.value

    def test_trust_level_anonymous(self):
        assert self.node.required_trust_level == TrustLevel.ANONYMOUS


# ── 労働安全衛生法 Art 45 determination — no longer a node ─────────────────────
#
# The rule used to live in ComplianceCheckNode, a fourth node in the inner
# linear pipeline. It now runs inside the output gate (PostProcessNode layer 0)
# — see tests/unit/test_output_gate.py::TestArt45DeterminationAtTheBoundary for
# the full rule coverage and the fail-closed behaviour. What is pinned here is
# that no such node exists any more: a check registered in graph topology is
# enforced only by that topology (mandatory output-gate rule).


class TestNoComplianceCheckNodeRemains:
    def test_the_module_is_gone(self):
        import importlib

        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("src.nodes.compliance_check_node")

    def test_no_node_class_carries_the_compliance_check_shape(self):
        import importlib
        import inspect
        import pkgutil

        import src.nodes as nodes_pkg
        from framework.nodes.base_node import BaseNode

        offenders = []
        for _, modname, _ in pkgutil.walk_packages(nodes_pkg.__path__, prefix="src.nodes."):
            module = importlib.import_module(modname)
            for attr in vars(module).values():
                if (
                    isinstance(attr, type)
                    and issubclass(attr, BaseNode)
                    and attr.__module__ == modname
                    and not inspect.isabstract(attr)
                    and "compliance" in attr.__name__.lower()
                ):
                    offenders.append(f"{modname}.{attr.__name__}")
        assert not offenders, f"compliance_check_node anti-pattern reintroduced: {offenders}"


# ── OutputFormatNode (inner domain node 4, ANONYMOUS) ──────────────────────────


class TestOutputFormatNode:
    @pytest.fixture(autouse=True)
    def patch_emit(self, monkeypatch):
        monkeypatch.setattr("src.nodes.output_format_node.emit_trace_event", lambda *a, **k: None)

    def setup_method(self):
        from src.nodes.output_format_node import OutputFormatNode

        self.node = OutputFormatNode()

    def _state(self, **extra):
        sections = {
            "equipment_summary": "Equipment ID:    MFG-EQ-CRANE-014",
            "inspection_findings": "  1. [HIGH] hoist wire rope: fraying",
            "fault_codes": "  - E-204",
            "corrective_actions": "  1. Replace rope.",
            "next_maintenance_schedule": "  Next Scheduled Maintenance: 2026-10-12",
            "compliance_status": "  Specified Machinery (特定機械等): YES",
            "sign_off": "  Inspected By: K. Tanaka",
        }
        state = {
            "report_sections": to_json(sections),
            "inspection_data": to_json(_inspection_data()),
            "caller_trust_level": TrustLevel.ANONYMOUS.value,
        }
        state.update(extra)
        return state

    def test_assembles_full_report(self):
        result = self.node(self._state())
        assert result["status"] == AgentStatus.SUCCESS.value
        report = result["maintenance_report"]
        assert result["result"] == report
        assert "EQUIPMENT MAINTENANCE REPORT" in report
        assert "MFG-EQ-CRANE-014" in report
        for header in (
            "1. Equipment Summary",
            "2. Inspection Findings",
            "3. Fault Codes",
            "4. Corrective Actions",
            "5. Next Maintenance Schedule",
            "6. Compliance Status",
            "7. Sign-off",
        ):
            assert header in report, f"missing section header: {header}"

    def test_body_carries_no_statutory_determination(self):
        """The body must NOT state a determination.

        Rendering it here from a state key is what made the determination
        droppable: with the compliance node gone the renderer's default
        fabricated "Formal Record Required: NO" and shipped it as success. The
        note is now rendered by the output gate, from the gate's own
        determination.
        """
        report = self.node(self._state())["maintenance_report"]
        assert "REGULATORY COMPLIANCE NOTE" not in report
        assert "Formal Record Required" not in report

    def test_a_stale_compliance_flags_state_key_changes_nothing(self):
        stale = to_json({"safety_record_required": True, "reason": "stale", "retention_years": 3})
        report = self.node(self._state(compliance_flags=stale))["maintenance_report"]
        assert "stale" not in report
        assert "Formal Record Required" not in report

    def test_missing_sections_returns_error(self):
        result = self.node(
            {"inspection_data": to_json(_inspection_data()), "caller_trust_level": TrustLevel.ANONYMOUS.value}
        )
        assert result["status"] == AgentStatus.ERROR.value

    def test_trust_level_anonymous(self):
        assert self.node.required_trust_level == TrustLevel.ANONYMOUS


# ── PostProcessNode (outer post_process, output gate, ANONYMOUS) ───────────────


class TestPostProcessNode:
    @pytest.fixture(autouse=True)
    def patch_emit(self, monkeypatch):
        monkeypatch.setattr("src.nodes.post_process_node.emit_trace_event", lambda *a, **k: None)

    def setup_method(self):
        from src.nodes.post_process_node import PostProcessNode

        self.node = PostProcessNode()

    # Production-shaped validated record: PreProcessNode writes it to
    # inspection_payload, and the boundary needs it to make the Art 45
    # determination. Without it the gate withholds, and a fixture that omits it
    # exercises the withholding path instead of the layer under test.
    _RECORD = to_json(
        {
            "equipment_id": "MFG-EQ-1",
            "equipment_class": "general",
            "findings": [{"component": "pump seal", "observation": "light seepage", "severity": "low"}],
            "fault_codes": [],
        }
    )

    def test_clean_report_passes_gate(self):
        report = "EQUIPMENT MAINTENANCE REPORT\nEquipment ID: MFG-EQ-1\nAll clear."
        result = self.node(
            {
                "maintenance_report": report,
                "inspection_payload": self._RECORD,
                "caller_trust_level": TrustLevel.ANONYMOUS.value,
            }
        )
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["formatted_output"].startswith(report)
        assert result["result"] == result["formatted_output"]
        # The gate appends its own determination — the body arrived without one.
        assert "REGULATORY COMPLIANCE NOTE" in result["formatted_output"]
        assert result["safety_record_required"] is False

    def test_empty_report_uses_fallback(self):
        result = self.node(
            {
                "maintenance_report": "",
                "inspection_payload": self._RECORD,
                "caller_trust_level": TrustLevel.ANONYMOUS.value,
            }
        )
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "No report content generated" in result["formatted_output"]
        # No report ⇒ no determination is asserted to the reader either.
        assert result["compliance_flags"] is None
        assert result["safety_record_required"] is None

    def test_output_gate_withholds_credential_leak(self):
        leaky = "EQUIPMENT MAINTENANCE REPORT\ntoken=sk-abcdefghij0123456789ABCDEF"
        result = self.node(
            {
                "maintenance_report": leaky,
                "inspection_payload": self._RECORD,
                "caller_trust_level": TrustLevel.ANONYMOUS.value,
            }
        )
        assert result["status"] == AgentStatus.ERROR.value
        # Behaviour: the response is withheld and the credential never leaves.
        assert "WITHHELD" in result["formatted_output"]
        assert result["formatted_output"] == result["result"]
        assert "sk-abcdefghij0123456789ABCDEF" not in str(result)

    def test_security_gate_output_helper_detects_and_clears(self):
        from src.nodes.post_process_node import _security_gate_output

        assert _security_gate_output("sk-abcdefghij0123456789ABCDEF") is not None
        assert _security_gate_output("Bearer abcdefgh12345678") is not None
        assert _security_gate_output("password = supersecret123") is not None
        assert _security_gate_output("A perfectly clean maintenance report.") is None

    def test_trust_level_anonymous(self):
        assert self.node.required_trust_level == TrustLevel.ANONYMOUS


# ── Graph wiring: outer AgentBaseGraph + inner BaseGraph (Cat 2 nested) ─────────


class TestOuterGraphComposition:
    def test_registers_five_backbone_slots(self):
        from src.graph.graph import (
            MaintenanceReportGeneratorAgent,
            MaintenanceReportGraphNode,
        )
        from src.nodes.post_process_node import PostProcessNode
        from src.nodes.pre_process_node import PreProcessNode

        agent = MaintenanceReportGeneratorAgent()
        agent.compile()
        assert set(agent._nodes.keys()) == {
            "initialize",
            "pre_process",
            "main",
            "post_process",
            "finalize",
        }
        assert isinstance(agent._nodes["pre_process"], PreProcessNode)
        assert isinstance(agent._nodes["main"], MaintenanceReportGraphNode)
        assert isinstance(agent._nodes["post_process"], PostProcessNode)

    def test_name_and_state_schema(self):
        from src.schemas.state import State
        from src.graph.graph import MaintenanceReportGeneratorAgent

        agent = MaintenanceReportGeneratorAgent()
        assert agent.name == "MaintenanceReportGeneratorAgent"
        assert agent.state_schema is State

    def test_graph_alias_matches_real_class(self):
        from src.graph.graph import Graph, MaintenanceReportGeneratorAgent

        assert Graph is MaintenanceReportGeneratorAgent

    def test_main_slot_graphnode_contracts(self):
        from src.graph.graph import MaintenanceReportGraphNode

        node = MaintenanceReportGraphNode()
        assert node.error_strategy == "propagate"
        assert node.propagate_hitl is False
        # extract_input prefers validated_input, falls back to user_input
        assert node.extract_input({"validated_input": "V", "user_input": "U"}) == "V"
        assert node.extract_input({"user_input": "U"}) == "U"

    def test_merge_output_maps_subresult_keys(self):
        from src.graph.graph import MaintenanceReportGraphNode

        node = MaintenanceReportGraphNode()
        sub_result = {
            "maintenance_report": "REPORT",
            "report_sections": "{}",
            # An inner pipeline must not be able to dictate the statutory
            # determination: the boundary owns it, so these are NOT mapped.
            "compliance_flags": '{"safety_record_required": false}',
            "safety_record_required": False,
            "status": AgentStatus.SUCCESS.value,
            "node_history": ["x"],  # not forwarded by merge_output
        }
        delta = node.merge_output({}, sub_result)
        assert delta["maintenance_report"] == "REPORT"
        assert delta["status"] == AgentStatus.SUCCESS.value
        assert set(delta.keys()) == {
            "maintenance_report",
            "report_sections",
            "status",
        }


class TestInnerDomainGraph:
    @pytest.fixture(autouse=True)
    def patch_emit(self, monkeypatch):
        for mod in (
            "input_validate_node",
            "parse_inspection_data_node",
            "generate_report_sections_node",
            "output_format_node",
        ):
            monkeypatch.setattr(f"src.nodes.{mod}.emit_trace_event", lambda *a, **k: None)

    def test_registers_four_domain_nodes(self):
        from src.graph.domain_workflow_graph import DomainWorkflowGraph

        g = DomainWorkflowGraph()
        g.register_nodes()
        assert set(g._nodes.keys()) == {
            "input_validate",
            "parse_inspection_data",
            "generate_report_sections",
            "output_format",
        }

    def test_name_and_state_schema(self):
        from src.schemas.state import State
        from src.graph.domain_workflow_graph import DomainWorkflowGraph

        g = DomainWorkflowGraph()
        assert g.name == "mfg_c2_014_maintenance_report_workflow"
        assert g.state_schema is State

    def test_inner_graph_invoke_produces_report(self):
        """Standalone inner-graph invoke (ANONYMOUS caller) runs the linear pipeline
        and shapes the get_output() dict consumed by the outer merge_output()."""
        from framework.schemas.invocation_context import InvocationContext, TrustLevel
        from src.graph.domain_workflow_graph import DomainWorkflowGraph

        g = DomainWorkflowGraph()
        g.compile()
        ctx = InvocationContext(caller_trust_level=TrustLevel.ANONYMOUS)
        result = g.invoke(VALID_PAYLOAD, ctx=ctx)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["maintenance_report"] is not None
        assert "EQUIPMENT MAINTENANCE REPORT" in result["maintenance_report"]
        # The inner pipeline makes NO statutory determination and asserts none
        # in the body it hands to the boundary — the outer PostProcessNode does
        # both. crane ⇒ Art 45 record required is asserted end-to-end in
        # tests/proof_of_boundary/test_invoke_e2e.py.
        assert "safety_record_required" not in result
        assert "compliance_flags" not in result
        assert "Formal Record Required" not in result["maintenance_report"]


# ── Trust gate (BaseNode.__call__) — PreProcessNode VERIFIED_EXTERNAL ──────────
#
# The trust gate runs inside BaseNode.__call__ BEFORE execute(), so these two
# tests route PreProcessNode through __call__ (node(state)) — the only node
# whose required_trust_level > ANONYMOUS:
#   - an under-trusted (ANONYMOUS) caller is REFUSED before execute() runs;
#   - a trusted (VERIFIED_EXTERNAL) caller is admitted and reaches SUCCESS.
# On denial __call__ RETURNS an error dict (it does not raise), so we assert on
# the returned dict. The payload is PII-free (lowercase, no '@', no digit groups,
# no consecutive Title-Case words) so the platform input gate leaves it intact.

_TRUST_GATE_PAYLOAD = json.dumps(
    {
        "equipment_id": "pump-alpha",
        "equipment_class": "pump",
        "inspection_date": "2026-07-12",
        "inspector": "shift crew",
        "findings": [
            {"component": "valve seal", "observation": "minor seepage at the flange", "severity": "low"},
        ],
    }
)


class TestTrustGate:
    """Route PreProcessNode through BaseNode.__call__ to exercise the trust gate."""

    @pytest.fixture(autouse=True)
    def patch_emit(self, monkeypatch):
        monkeypatch.setattr("src.nodes.pre_process_node.emit_trace_event", lambda *a, **k: None)

    def setup_method(self):
        from src.nodes.pre_process_node import PreProcessNode

        self.node = PreProcessNode()

    def test_trust_gate_rejects_untrusted_caller_before_execute(self):
        # ANONYMOUS < required VERIFIED_EXTERNAL ⇒ __call__ refuses and RETURNS an
        # error dict (does not raise); execute() never runs.
        result = self.node(
            {
                "user_input": _TRUST_GATE_PAYLOAD,
                "input_context": {},
                "caller_trust_level": TrustLevel.ANONYMOUS.value,
            }
        )
        assert result["status"] == AgentStatus.ERROR.value
        assert any("trust gate denied" in e for e in result["error_log"])
        # execute()-only output keys are absent — proof the gate ran before execute()
        assert "validated_input" not in result
        assert "enriched_context" not in result

    def test_trust_gate_admits_trusted_caller(self):
        # VERIFIED_EXTERNAL == required ⇒ gate passes, execute() runs to SUCCESS.
        result = self.node(
            {
                "user_input": _TRUST_GATE_PAYLOAD,
                "input_context": {},
                "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value,
            }
        )
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["validated_input"] is not None
        assert json.loads(result["inspection_payload"])["equipment_id"] == "pump-alpha"
