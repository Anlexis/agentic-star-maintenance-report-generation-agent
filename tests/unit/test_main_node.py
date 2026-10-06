# MFG-C2-014 — Unit Tests: MainNode (standalone compatibility handler)

import inspect

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from src.nodes.main_node import MainNode


class TestMainNode:
    """Unit tests for the standalone compatibility main handler."""

    def setup_method(self):
        self.node = MainNode()

    def test_echoes_validated_input(self):
        """TC: MainNode echoes validated_input into result and returns SUCCESS."""
        result = self.node(
            {
                "validated_input": "test input",
                "node_history": [],
                "error_log": [],
                "caller_trust_level": TrustLevel.ANONYMOUS.value,
            }
        )
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["result"] == "test input"

    def test_falls_back_to_user_input(self):
        """TC: MainNode falls back to user_input when validated_input is absent."""
        result = self.node({"user_input": "raw input", "caller_trust_level": TrustLevel.ANONYMOUS.value})
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["result"] == "raw input"

    def test_empty_input_still_succeeds(self):
        """TC: MainNode handles empty input gracefully."""
        result = self.node(
            {
                "validated_input": "",
                "node_history": [],
                "error_log": [],
                "caller_trust_level": TrustLevel.ANONYMOUS.value,
            }
        )
        assert result["status"] == AgentStatus.SUCCESS.value

    def test_trust_level_anonymous(self):
        assert MainNode.required_trust_level == TrustLevel.ANONYMOUS

    def test_execute_method_signature(self):
        """Node must implement execute(state), never _invoke_impl.

        Canonical contract:
          - Override: execute(self, state: AgentState) -> dict
          - PROHIBITED: _invoke_impl(), process() override
        """
        assert hasattr(MainNode, "execute"), "MainNode must implement execute()"
        params = list(inspect.signature(MainNode.execute).parameters.keys())
        assert (
            params[0] == "self" and params[1] == "state"
        ), f"execute() must accept (self, state), got params: {params}"
        assert (
            "_invoke_impl" not in MainNode.__dict__
        ), "_invoke_impl() must not be defined in MainNode — use execute() instead"
