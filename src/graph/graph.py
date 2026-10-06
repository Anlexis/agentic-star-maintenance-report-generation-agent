"""AgentCore Platform v1.0"""

# MFG-C2-014 — Outer graph (AgentBaseGraph; Cat 2 two-layer nested architecture)
#
# Architecture (Cat 2):
#
#   Outer backbone (fixed — identical to Cat 1, do NOT override add_edges()):
#     START → initialize → pre_process → main → {route} → post_process → finalize → END
#                                             ↓ (RETRY, max 3)
#                                          pre_process
#
#   `main` slot is a GraphNode subclass (MaintenanceReportGraphNode) that
#   delegates the full domain workflow to DomainWorkflowGraph (inner BaseGraph).
#
#   Domain complexity is fully encapsulated inside the inner graph. The outer
#   backbone is never modified.
#
# Directory layout:
#   src/graph/graph.py                 ← outer graph (this file)
#   src/graph/domain_workflow_graph.py ← inner graph (multi-step topology)
#   src/graph/context_bridge.py        ← inspection-record hand-off (outer → inner)
#
# Rules enforced:
#   ✅ MaintenanceReportGeneratorAgent inherits AgentBaseGraph (framework base
#      class, direct inheritance)
#   ✅ super().register_nodes() called first (fills initialize + finalize)
#   ✅ MaintenanceReportGraphNode assigned to self._nodes["main"]
#   ✅ PreProcessNode (VERIFIED_EXTERNAL) in pre_process slot (trust gate)
#   ✅ PostProcessNode (ANONYMOUS) in post_process slot (output gate — also
#      makes and renders the 労働安全衛生法 Art 45 statutory determination;
#      there is no compliance_check node, per the mandatory output-gate rule)
#   ✅ _security_gate_output on the agent class (canonical output gate)
#   ✅ merge_output() returns only changed keys
#   ✅ get_output() surfaces the post-gate domain result
#   ✅ class name matches config/agent.yaml class: field exactly
#   ❌ add_edges() NOT overridden on the outer graph
#   ❌ No platform-SDK imports (framework/ and shared/ only)

import math
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Optional, cast

from framework.errors import SecurityViolationError
from framework.graph.agent_base_graph import AgentBaseGraph
from framework.nodes.graph_node import GraphNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from src.graph.context_bridge import set_caller_input_context
from src.nodes.post_process_node import PostProcessNode, _security_gate_output
from src.nodes.pre_process_node import PreProcessNode
from src.schemas.state import State, from_json

if TYPE_CHECKING:
    from src.graph.domain_workflow_graph import DomainWorkflowGraph

# Runtime-parameter file: src/graph/graph.py -> parents[2] is the repo root.
# config/agent.yaml (the static manifest) holds only registration identity;
# every runtime parameter lives in config/config.yaml.
_RUNTIME_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "config.yaml"


def _runtime_config() -> dict[str, Any]:
    """Read the runtime parameters from config/config.yaml.

    This is the same file the platform registry loads and passes as
    Graph(config=...); the standalone server (src/api/server.py) reads it here
    so the deployed agent and a registry-loaded agent see identical
    configuration. Returns an empty dict — never raises — when the file is
    absent, unreadable, not valid YAML, or not a mapping (the graph then runs
    on its built-in defaults).
    """
    try:
        import yaml

        loaded = yaml.safe_load(_RUNTIME_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(loaded, dict):
        return {}
    return loaded


def _config_number(value: Any, lo: float, hi: float) -> Optional[float]:
    """Validate a declared numeric setting: a real number, finite, within [lo, hi].

    Bools, strings, non-numerics, NaN/Infinity, and out-of-range values return
    None (the caller then keeps the built-in default). A non-finite value is
    the dangerous case: NaN comparisons are always False, so an unvalidated
    NaN would silently disable whatever decision the value feeds.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    parsed = float(value)
    if not math.isfinite(parsed) or not lo <= parsed <= hi:
        return None
    return parsed


class MaintenanceReportGraphNode(GraphNode):
    """GraphNode subclass assigned to the `main` slot of MaintenanceReportGeneratorAgent.

    Wraps DomainWorkflowGraph (inner Cat 2 BaseGraph).
    Called by AgentBaseGraph backbone after pre_process and before post_process.

    Contracts:
      get_subgraph()  — instantiate and return DomainWorkflowGraph
      extract_input() — pull validated_input from outer state; bridge the record
      merge_output()  — map sub_result fields into outer state delta (changed keys only)
      error_strategy  — "propagate": re-raise inner errors as SubgraphError (fail-fast)
    """

    error_strategy: ClassVar[str] = "propagate"
    propagate_hitl: ClassVar[bool] = False

    def get_subgraph(self) -> "DomainWorkflowGraph":
        """Instantiate and return the inner domain workflow graph.

        DomainWorkflowGraph is imported lazily (inside the method) to avoid
        circular-import risk at module load time and to match the Cat 2 pattern.
        """
        from src.graph.domain_workflow_graph import DomainWorkflowGraph

        return DomainWorkflowGraph(config=self._parent_config())

    def extract_input(self, state: AgentState) -> str:
        """Return the string input passed into inner_graph.invoke().

        PreProcessNode validates the caller's inspection record and writes an
        inert request summary to validated_input; the record itself travels as
        JSON under the template-specific inspection_payload key. Only the
        summary string is passed as the inner graph's user_input — the string
        channel is PII-masked at every node's input gate, and a masked record
        would name the wrong components in the finished report.

        The validated record is bridged out-of-band instead:
        GraphNode.execute() does not forward input_context on subgraph.invoke()
        (SDK 1.0.1), and extract_input is the last hook in this repo's code
        that sees the outer state before the inner invoke — see
        src/graph/context_bridge.py.
        """
        record = from_json(cast(Optional[str], state.get("inspection_payload")), None)
        set_caller_input_context({"inspection": record} if isinstance(record, dict) else {})
        return cast(str, state.get("validated_input", state.get("user_input", "")))

    def merge_output(self, state: AgentState, sub_result: dict[str, Any]) -> dict[str, Any]:
        """Map inner graph sub_result back into the outer state delta.

        sub_result is the dict returned by DomainWorkflowGraph.get_output().
        Returns ONLY changed keys — never the full state.

        Key coupling (designed together with DomainWorkflowGraph.get_output()):
          Inner get_output() emits  → "maintenance_report", "report_sections",
                                       "status"
          This merge_output() reads → sub_result.get(...) for each of these keys.

        PostProcessNode (outer post_process) reads maintenance_report +
        inspection_payload from state to apply the output gate, make the
        労働安全衛生法 Art 45 determination, and set formatted_output.
        compliance_flags / safety_record_required are NOT mapped here: they are
        the boundary's own product, written by PostProcessNode. Mapping an
        inner value onto those keys would reintroduce a determination the
        boundary does not own.
        """
        return {
            "maintenance_report": sub_result.get("maintenance_report"),
            "report_sections": sub_result.get("report_sections"),
            "status": sub_result.get("status"),
        }

    def _parent_config(self) -> dict[str, Any]:
        """Forward the declared runtime settings to the inner graph.

        Reads config/config.yaml (see _runtime_config) and returns the settings
        dict handed to DomainWorkflowGraph(config=...). Every forwarded value
        is validated here (type, finiteness, range) so a malformed
        configuration file can neither crash graph construction nor smuggle a
        non-finite number into the pipeline. Invalid or absent keys are simply
        not forwarded; the graph then runs on its built-in defaults.

        LLM path note: the llm keys (system_prompt_template / temperature /
        max_tokens) are forwarded for a live-model build. In the bundled build
        no model is provisioned, so GenerateReportSectionsNode uses its
        documented deterministic synthesis — it never fabricates a model
        response. See src/nodes/generate_report_sections_node.py.
        """
        cfg = _runtime_config()
        llm_raw = cfg.get("llm")
        llm: dict[str, Any] = llm_raw if isinstance(llm_raw, dict) else {}

        declared: dict[str, Any] = {}

        template = llm.get("system_prompt_template")
        if isinstance(template, str) and template:
            declared["system_prompt_template"] = template

        temperature = _config_number(llm.get("temperature"), 0.0, 2.0)
        if temperature is not None:
            declared["temperature"] = temperature

        max_tokens = _config_number(llm.get("max_tokens"), 1, 100_000)
        if max_tokens is not None and max_tokens == int(max_tokens):
            declared["max_tokens"] = int(max_tokens)

        return declared


class MaintenanceReportGeneratorAgent(AgentBaseGraph):
    """Outer graph for MFG-C2-014 (Cat 2 — DocGenerationAgent).

    Inherits AgentBaseGraph directly (framework base class). Domain logic is
    fully encapsulated in MaintenanceReportGraphNode (main slot), which
    delegates to DomainWorkflowGraph (inner BaseGraph).

    Backbone (fixed — identical to Cat 1):
        START → initialize → pre_process → main → post_process → finalize → END

    register_nodes() and get_output() are the overrides:
      - super().register_nodes() fills: initialize, finalize (framework defaults)
      - pre_process:  PreProcessNode  (VERIFIED_EXTERNAL — trust gate + caller contract)
      - main:         MaintenanceReportGraphNode (delegates to DomainWorkflowGraph)
      - post_process: PostProcessNode (ANONYMOUS — output gate)
      - get_output(): surfaces the post-gate domain result

    Runtime configuration: the platform registry loads config/config.yaml and
    passes it as Graph(config=...); the standalone server does the same via
    _runtime_config(). AgentBaseGraph itself consumes max_retry from that
    config (retry routing), so the declared value is live in both deployments.

    The canonical `_security_gate_output` lives on this agent class and
    delegates to the shared scanner in post_process_node.py (one source of
    truth for the pattern set).

    add_edges() is NOT overridden — backbone wiring belongs to the framework.
    Class name MUST match config/agent.yaml `class:` field exactly.
    server.py imports this as `Graph` via the alias below.
    """

    @property
    def name(self) -> str:
        """Agent identifier registered with the platform registry."""
        return "MaintenanceReportGeneratorAgent"

    @property
    def state_schema(self) -> type:
        return State

    def register_nodes(self) -> None:
        """Fill all 5 backbone slots.

        super().register_nodes() MUST be called first — it injects the
        framework's default InitializeNode (sets schema_version, session_id,
        trust_level) and FinalizeNode (builds response_metadata, total_time_ms).
        """
        super().register_nodes()  # fills: initialize, finalize

        self._nodes["pre_process"] = PreProcessNode()
        self._nodes["main"] = MaintenanceReportGraphNode()
        self._nodes["post_process"] = PostProcessNode()

    def get_output(self, state: AgentState) -> dict[str, Any]:
        """Surface the domain maintenance-report result on the outer invoke() return.

        AgentBaseGraph.get_output() returns only the minimal ``{output, status,
        trace_id, correlation_id, node_history}`` envelope. On the compiled
        outer-graph success path that would drop the structured domain result —
        the fields the inner DomainWorkflowGraph produces (merged into outer
        state by MaintenanceReportGraphNode.merge_output(): maintenance_report,
        report_sections) and the gated PostProcessNode outputs
        (formatted_output, result, compliance_flags, safety_record_required) — from the
        dict returned by ``agent.invoke()``. They would all be None to the
        caller even on a successful report generation. This override extends
        the base envelope so a successful invocation actually returns the
        domain result.

        Output-gate invariant preserved (fail-closed):
          * ``formatted_output`` is what the gated PostProcessNode produced, so
            it is the only caller-facing value on either path (on a block it is
            the gate's own content-free withholding notice).
          * ``result`` is surfaced ONLY on the gated success path. The framework
            base resolves its ``output`` key as ``formatted_output or result``
            WITHOUT consulting status, so on a non-success outcome that fallback
            is re-resolved here as well: an absent gate output stays absent and
            never degrades into the report. Not status-guarding ``result``
            leaves the error envelope free to carry the very answer the output
            gate refused, and a falsy ``formatted_output`` ACTIVATES that
            fallback rather than suppressing it.
          * ``maintenance_report`` and the structured fields (report_sections /
            compliance_flags / safety_record_required) are surfaced ONLY when
            the gate passed (status == SUCCESS). On any non-success outcome —
            including a credential block, an identifier-integrity block, or a
            statutory-determination block — they are withheld (None).
            compliance_flags / safety_record_required are PostProcessNode's own
            output: the 労働安全衛生法 Art 45 determination is made at the
            boundary, so the value the caller reads is the value the gate used.
        """
        output = super().get_output(state)  # {output, status, trace_id, correlation_id, node_history}
        succeeded = state.get("status") == AgentStatus.SUCCESS.value
        formatted_output = state.get("formatted_output")

        # Caller-facing value produced by the gate itself — safe on both paths.
        output["formatted_output"] = formatted_output
        if succeeded:
            output["result"] = state.get("result")
            output["maintenance_report"] = formatted_output or state.get("result")
        else:
            output["result"] = None
            output["maintenance_report"] = None
            # Re-resolve the base envelope's value without the `or result`
            # fallback: on a non-success outcome the gate's own output is all
            # the caller may see, and an absent one stays absent.
            output["output"] = formatted_output or None

        # Structured domain result — surfaced only on the gated success path.
        output["report_sections"] = state.get("report_sections") if succeeded else None
        output["compliance_flags"] = state.get("compliance_flags") if succeeded else None
        output["safety_record_required"] = state.get("safety_record_required") if succeeded else None
        return cast("dict[str, Any]", output)

    # ── Mandatory output gate on the agent class ──────────────────────────────
    def _security_gate_output(self, output_text: str) -> None:
        """Raise SecurityViolationError if a credential pattern is in the output.

        Canonical output gate for the template. Delegates the pattern set to
        the shared scanner in post_process_node.py (the post_process backbone
        slot applies the same scanner at runtime — defence in depth, one
        source of truth).
        """
        if not output_text:
            return None
        violation = _security_gate_output(str(output_text))
        if violation:
            raise SecurityViolationError(
                f"MaintenanceReportGeneratorAgent output gate: "
                f"credential-like pattern detected in output ({violation})"
            )
        return None

    # add_edges() is NOT overridden — backbone wiring belongs to the framework.


# Alias for the standalone HTTP entry point (src/api/server.py imports `Graph`).
# Class name MaintenanceReportGeneratorAgent matches config/agent.yaml class: field.
Graph = MaintenanceReportGeneratorAgent
