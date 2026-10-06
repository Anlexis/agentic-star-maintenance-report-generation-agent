# MFG-C2-014 — Regression: outer-graph get_output() surfaces the domain result
#
# MaintenanceReportGeneratorAgent
# did NOT override get_output(), so AgentBaseGraph.get_output() returned only the
# minimal {output, status, trace_id, correlation_id, node_history} envelope. On
# the compiled outer-graph success path that dropped every domain field that
# MaintenanceReportGraphNode.merge_output() and PostProcessNode add to state
# (maintenance_report, report_sections, compliance_flags, safety_record_required,
# formatted_output, result) — all None to the caller even on a successful
# maintenance-report generation. The get_output() override in graph.py extends
# the base envelope with the post-gate domain result on success (fail-closed:
# structured fields withheld unless status == SUCCESS).
#
# This test compiles the REAL outer graph and invokes it on the VERIFIED_EXTERNAL
# external path (the real deployed caller path — never for_internal()).

import json

import pytest

from framework.schemas.agent_status import AgentStatus

_VALID_PAYLOAD = json.dumps(
    {
        "equipment_id": "MFG-EQ-CRANE-014",
        "equipment_class": "crane",
        "inspection_date": "2026-07-12",
        "inspector": "K. Tanaka",
        "findings": [
            {
                "component": "hoist wire rope",
                "observation": "Visible strand fraying near the drum flange.",
                "severity": "high",
            },
            {
                "component": "upper-travel limit switch",
                "observation": "Delayed actuation under rated load.",
                "severity": "medium",
            },
        ],
        "fault_codes": ["E-204", "E-118"],
        "corrective_actions": ["Replace the hoist wire rope assembly."],
        "next_maintenance_date": "2026-10-12",
    }
)


class TestOuterGraphGetOutput:
    @pytest.fixture(autouse=True)
    def patch_emit(self, monkeypatch):
        for mod in (
            "pre_process_node",
            "input_validate_node",
            "parse_inspection_data_node",
            "generate_report_sections_node",
            "output_format_node",
            "post_process_node",
        ):
            monkeypatch.setattr(f"src.nodes.{mod}.emit_trace_event", lambda *a, **k: None)

    def _invoke(self):
        from framework.schemas.invocation_context import InvocationContext, TrustLevel
        from src.graph.graph import Graph

        agent = Graph()
        agent.compile()
        ctx = InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)
        return agent.invoke(_VALID_PAYLOAD, ctx=ctx)

    def test_success_surfaces_gated_domain_result(self):
        result = self._invoke()
        assert result["status"] == AgentStatus.SUCCESS.value
        # Post-gate, caller-facing report — surfaced, not dropped to None.
        assert result["maintenance_report"] is not None
        assert "EQUIPMENT MAINTENANCE REPORT" in result["maintenance_report"]
        assert result["formatted_output"] == result["maintenance_report"]
        assert result["result"] == result["maintenance_report"]
        # Structured domain fields — surfaced only on the gated success path.
        assert result["report_sections"] is not None
        assert result["compliance_flags"] is not None
        # crane = 特定機械等 (specified machinery) ⇒ 労働安全衛生法 Art 45 record required.
        assert result["safety_record_required"] is True

    def test_base_envelope_preserved(self):
        result = self._invoke()
        # Base AgentBaseGraph envelope stays intact (get_output extends, not replaces).
        assert result["output"] is not None
        assert "EQUIPMENT MAINTENANCE REPORT" in result["output"]
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "node_history" in result


# ── Envelope containment ─────────────────────────────────────────────────────
#
# The envelope is the last thing between graph state and the caller, and the
# second half of the output-gate contract. AgentBaseGraph.get_output() resolves
# its `output` key as `formatted_output or result` WITHOUT consulting status, so
# an envelope that forwards state verbatim hands back the very report the output
# gate refused, inside an error envelope. A falsy `formatted_output` does not
# suppress that fallback — it ACTIVATES it.
#
# These call get_output() directly on a state dict so the resolution rule itself
# is pinned, independent of which node happened to produce that state.

_PRE_GATE_REPORT = (
    "========================================================================\n"
    "EQUIPMENT MAINTENANCE REPORT\n"
    "Equipment ID: MFG-EQ-CRANE-014\n"
    "  - E-204\n"
    "Inspector: K. Tanaka\n"
)


def _envelope(**overrides) -> dict:
    from src.graph.graph import MaintenanceReportGeneratorAgent

    state = {
        "status": AgentStatus.SUCCESS.value,
        "formatted_output": _PRE_GATE_REPORT,
        "result": _PRE_GATE_REPORT,
        "maintenance_report": _PRE_GATE_REPORT,
        "report_sections": '{"equipment_summary": "Equipment ID: MFG-EQ-CRANE-014"}',
        "compliance_flags": '{"safety_record_required": true}',
        "safety_record_required": True,
        "trace_id": "envelope-test",
        "correlation_id": "envelope-test",
        "node_history": ["PostProcessNode"],
    }
    state.update(overrides)
    return MaintenanceReportGeneratorAgent().get_output(state)


class TestErrorEnvelopeContainment:
    """A non-success envelope must not carry the report it refused."""

    def test_success_control_really_surfaces_the_report(self):
        """The control: without it every assertion below could pass on an
        envelope that returns nothing at all."""
        envelope = _envelope()
        assert envelope["output"] == _PRE_GATE_REPORT
        assert envelope["result"] == _PRE_GATE_REPORT
        assert envelope["maintenance_report"] == _PRE_GATE_REPORT
        assert envelope["report_sections"] is not None
        assert envelope["safety_record_required"] is True

    def test_result_is_never_surfaced_on_an_error(self):
        envelope = _envelope(status=AgentStatus.ERROR.value)
        assert envelope["result"] is None
        assert envelope["maintenance_report"] is None

    def test_the_or_result_fallback_is_dead_on_an_error(self):
        """The hole in the base envelope: with no formatted_output, `output`
        falls through to `result`. On a non-success outcome an absent gate
        output must stay absent, never become the refused report."""
        envelope = _envelope(status=AgentStatus.ERROR.value, formatted_output=None)
        assert not envelope["output"]
        assert "EQUIPMENT MAINTENANCE REPORT" not in json.dumps(envelope)
        assert "E-204" not in json.dumps(envelope)

    def test_a_falsy_gate_output_does_not_reopen_the_fallback(self):
        """An empty string is the trap: falsy, so `formatted_output or result`
        resolves to the pre-gate report."""
        envelope = _envelope(status=AgentStatus.ERROR.value, formatted_output="")
        assert not envelope["output"]
        assert "MFG-EQ-CRANE-014" not in json.dumps(envelope)

    def test_error_surfaces_the_gate_notice_and_nothing_else(self):
        withheld = "[MAINTENANCE REPORT WITHHELD: output contained a disallowed pattern (equipment_id).]"
        envelope = _envelope(status=AgentStatus.ERROR.value, formatted_output=withheld)
        assert envelope["output"] == withheld
        assert envelope["formatted_output"] == withheld
        assert envelope["result"] is None
        assert "EQUIPMENT MAINTENANCE REPORT" not in json.dumps(envelope)

    def test_structured_fields_are_withheld_on_an_error(self):
        envelope = _envelope(status=AgentStatus.ERROR.value)
        for key in ("maintenance_report", "report_sections", "compliance_flags", "safety_record_required"):
            assert envelope[key] is None, key

    def test_containment_holds_for_every_non_success_status(self):
        """Not an ERROR special case: any status that is not SUCCESS means the
        output gate did not pass the response — including the terminal statuses
        that route straight to finalize without post_process running at all."""
        for status in (
            AgentStatus.TIMEOUT.value,
            AgentStatus.CANCELLED.value,
            AgentStatus.RETRY.value,
            AgentStatus.PENDING.value,
        ):
            envelope = _envelope(status=status, formatted_output=None)
            assert envelope["result"] is None, status
            assert not envelope["output"], status
            assert envelope["maintenance_report"] is None, status
