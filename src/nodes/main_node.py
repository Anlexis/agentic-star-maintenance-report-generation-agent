"""AgentCore Platform v1.0"""

# MFG-C2-014 — MainNode (standalone compatibility handler)
#
# The production `main` backbone slot of MaintenanceReportGeneratorAgent is
# filled by MaintenanceReportGraphNode (src/graph/graph.py), which delegates the
# full maintenance-report workflow to DomainWorkflowGraph. MainNode is NOT part
# of that backbone.
#
# MainNode is a supported, minimal, single-responsibility standalone node: a
# FunctionNode that echoes the validated caller input into `result`. It exists
# for direct single-node invocation and smoke checks (e.g. verifying the
# FunctionNode execute() contract and the node invoke order in isolation) and
# is covered by tests/unit/test_main_node.py plus the invoke-order sweep. It
# carries no domain logic and never runs the report pipeline.
#
# Standalone node — ANONYMOUS trust. Returns ONLY the keys it writes
# (partial-dict contract).

import logging
from typing import Any, ClassVar, Dict, Optional

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import TrustLevel
from shared.utils.audit_logger import emit_trace_event

logger = logging.getLogger(__name__)


class MainNode(FunctionNode):
    """Standalone compatibility main handler (NOT the Cat 2 backbone main slot).

    Echoes the validated caller input into `result`. Supported for direct
    single-node invocation and smoke tests; carries no domain logic.

    Input state keys:
        validated_input | user_input: str

    Output state keys (partial dict):
        result: str
        status: str
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: AgentState, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload = state.get("validated_input", state.get("user_input", "")) or ""
        # Every execute() emits an audit event.
        emit_trace_event(
            "main_node_complete",
            {"input_length": len(payload)},
            state,
        )
        logger.info("MainNode: echo handler input_length=%d", len(payload))
        return {
            "result": payload,
            "status": AgentStatus.SUCCESS.value,
        }
