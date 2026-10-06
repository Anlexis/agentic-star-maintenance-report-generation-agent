"""AgentCore Platform v1.0"""

# MFG-C2-014 — DomainWorkflowGraph (inner BaseGraph)
#
# This is the INNER graph for the Cat 2 two-layer nested architecture.
# It encapsulates the maintenance report generation pipeline:
#
#   START
#     → input_validate           (InputValidateNode)
#     → parse_inspection_data    (ParseInspectionDataNode)
#     → generate_report_sections (GenerateReportSectionsNode)
#     → output_format            (OutputFormatNode)
#     → END
#
# Called by MaintenanceReportGraphNode.get_subgraph() (graph.py).
# get_output() shapes the sub_result dict consumed by merge_output() there.
#
# Rules enforced:
#   ✅ Inherits BaseGraph (fully custom topology — no forced backbone)
#   ✅ Implements all 7 BaseGraph ABC methods
#   ✅ register_nodes() does NOT call super() (abstract in BaseGraph)
#   ✅ Does NOT register initialize / finalize (outer backbone concerns)
#   ✅ All inner nodes declare required_trust_level = TrustLevel.ANONYMOUS
#   ✅ get_output() designed together with MaintenanceReportGraphNode.merge_output()
#   ✅ _extra_initial_state() seeds the bridged inspection record (context_bridge)
#   ❌ No platform-SDK imports (framework/ and shared/ only)

from typing import Any

from langgraph.graph import END, START

from framework.graph.base_graph import BaseGraph
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from src.graph.context_bridge import get_caller_input_context
from src.nodes.generate_report_sections_node import GenerateReportSectionsNode
from src.nodes.input_validate_node import InputValidateNode
from src.nodes.output_format_node import OutputFormatNode
from src.nodes.parse_inspection_data_node import ParseInspectionDataNode
from src.schemas.state import State


class DomainWorkflowGraph(BaseGraph):
    """Inner domain workflow graph for MFG-C2-014.

    Inherits BaseGraph directly for a fully custom node topology.
    Called by MaintenanceReportGraphNode.get_subgraph() in graph.py.

    Pipeline (linear):
        START
          → input_validate            (InputValidateNode)
          → parse_inspection_data     (ParseInspectionDataNode)
          → generate_report_sections  (GenerateReportSectionsNode)
          → output_format             (OutputFormatNode)
          → END

    All nodes are FunctionNode subclasses with ANONYMOUS trust_level.
    initialize / finalize are outer backbone concerns — not registered here.

    The 労働安全衛生法 Art 45 statutory determination is NOT a node here. It is
    made at the output boundary by PostProcessNode, which also renders the
    REGULATORY COMPLIANCE NOTE — see src/nodes/post_process_node.py layer (0).
    A determination registered as one node among many is only enforced by the
    topology that happens to include it.
    """

    # ── Identity ──────────────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return "mfg_c2_014_maintenance_report_workflow"

    @property
    def state_schema(self) -> type:
        return State

    # ── Config validation ─────────────────────────────────────────────────────

    def _validate_config(self) -> None:
        """No mandatory config keys.

        The settings forwarded by MaintenanceReportGraphNode._parent_config()
        (the llm block, reserved for a live-model build) are already validated
        for type, finiteness and range there; absent keys leave the pipeline
        on its built-in deterministic behaviour.
        """

    # ── Initial state ─────────────────────────────────────────────────────────

    def _extra_initial_state(self) -> dict[str, Any]:
        """Seed the validated inspection record bridged from the outer graph.

        The framework does not forward the outer input_context into a nested
        graph's invoke(), and the string channel is PII-masked at every node
        boundary — so the record crosses via the context bridge instead
        (src/graph/context_bridge.py). This hook runs inside subgraph.invoke()
        and places the record on the inner state's input_context, where
        InputValidateNode reads it.
        """
        return {"input_context": get_caller_input_context()}

    # ── Node registration ─────────────────────────────────────────────────────

    def register_nodes(self) -> None:
        """Register all 4 domain nodes.

        No super() call — BaseGraph.register_nodes() is abstract.
        Do NOT register initialize or finalize; those are outer backbone
        concerns handled by AgentBaseGraph in graph.py.
        Every key registered here is referenced in add_edges().
        The node contract is ``execute(self, state) -> dict``; these nodes
        take no constructor arguments.
        """
        self._nodes["input_validate"] = InputValidateNode()
        self._nodes["parse_inspection_data"] = ParseInspectionDataNode()
        self._nodes["generate_report_sections"] = GenerateReportSectionsNode()
        self._nodes["output_format"] = OutputFormatNode()

    # ── Edge wiring ───────────────────────────────────────────────────────────

    def add_edges(self) -> None:
        """Wire the linear maintenance-report generation topology.

        Linear flow:
            input_validate → parse_inspection_data → generate_report_sections
            → output_format → END.

        No conditional branching — all paths through the report pipeline are
        linear.  route() satisfies the ABC but is not used at runtime.
        """
        self._sg.add_edge(START, "input_validate")
        self._sg.add_edge("input_validate", "parse_inspection_data")
        self._sg.add_edge("parse_inspection_data", "generate_report_sections")
        self._sg.add_edge("generate_report_sections", "output_format")
        self._sg.add_edge("output_format", END)

    # ── Routing ───────────────────────────────────────────────────────────────

    def route(self, state: AgentState) -> str:
        """Conditional routing — required by BaseGraph ABC.

        Linear topology; add_conditional_edges() is not used, so this method
        is never called at runtime.  Returns END on error so an unexpected
        invocation does not re-enter a processing node.
        """
        if state.get("status") == AgentStatus.ERROR.value:
            return END
        return "output_format"

    # ── Output shape ──────────────────────────────────────────────────────────

    def get_output(self, state: AgentState) -> dict[str, Any]:
        """Shape the output dict returned to the outer graph as sub_result.

        This dict is received by MaintenanceReportGraphNode.merge_output()
        in graph.py as the `sub_result` argument.  Both methods are designed
        together to guarantee field-name consistency:

            Inner get_output() emits:   "maintenance_report", "report_sections",
                                        "status"
            Outer merge_output() reads: sub_result.get(...) for each key above.

        compliance_flags / safety_record_required are deliberately NOT emitted
        here: the Art 45 determination is made at the output boundary by
        PostProcessNode, not inside this pipeline.
        """
        return {
            "maintenance_report": state.get("maintenance_report"),
            "report_sections": state.get("report_sections"),
            "status": state.get("status"),
            "node_history": state.get("node_history", []),
            "correlation_id": state.get("correlation_id"),
        }
