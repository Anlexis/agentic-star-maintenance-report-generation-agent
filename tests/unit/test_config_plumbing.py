# MFG-C2-014 — Runtime-config plumbing
#
# config/config.yaml is the single runtime-parameter file: the platform
# registry passes it as Graph(config=...), and the standalone server reads it
# through _runtime_config(). These tests prove the declared values ARRIVE —
# at the outer graph (max_retry, consumed by the framework's retry routing)
# and across the layer boundary at the inner graph (the validated llm block) —
# rather than being silently dropped to defaults. They also pin the
# fail-closed validator for declared numerics: NaN/Infinity/bools/strings and
# out-of-range values are never forwarded.

import math

from src.graph.graph import (
    MaintenanceReportGeneratorAgent,
    MaintenanceReportGraphNode,
    _config_number,
    _runtime_config,
)


class TestRuntimeConfigFile:
    def test_runtime_config_reads_the_declared_file(self):
        cfg = _runtime_config()
        assert cfg.get("max_retry") == 3
        assert cfg.get("timeout_s") == 30
        assert cfg.get("llm", {}).get("max_tokens") == 3500

    def test_declared_max_retry_reaches_the_outer_graph(self):
        """The framework's retry routing reads max_retry from the constructor
        config — the same dict the registry and the server both pass."""
        agent = MaintenanceReportGeneratorAgent(config=_runtime_config())
        assert agent.config.get("max_retry") == 3

    def test_declared_llm_values_reach_the_inner_graph(self):
        """The layer boundary forwards the validated llm block: the inner
        graph is constructed exactly as production does it (through the
        main-slot wrapper), and the declared values arrive in its config."""
        subgraph = MaintenanceReportGraphNode().get_subgraph()
        assert subgraph.config.get("system_prompt_template") == "prompts/maintenance_report.j2"
        assert subgraph.config.get("temperature") == 0.1
        assert subgraph.config.get("max_tokens") == 3500


class TestConfigNumberValidator:
    def test_accepts_finite_in_range(self):
        assert _config_number(0.5, 0.0, 2.0) == 0.5
        assert _config_number(3, 0, 10) == 3.0

    def test_rejects_non_finite(self):
        assert _config_number(float("nan"), 0.0, 1e9) is None
        assert _config_number(float("inf"), 0.0, 1e9) is None
        assert _config_number(float("-inf"), 0.0, 1e9) is None

    def test_rejects_bools_and_non_numerics(self):
        assert _config_number(True, 0.0, 2.0) is None
        assert _config_number("0.5", 0.0, 2.0) is None
        assert _config_number(None, 0.0, 2.0) is None
        assert _config_number([0.5], 0.0, 2.0) is None

    def test_rejects_out_of_range(self):
        assert _config_number(-0.1, 0.0, 2.0) is None
        assert _config_number(2.1, 0.0, 2.0) is None

    def test_malformed_llm_values_are_not_forwarded(self, monkeypatch):
        """A NaN temperature or an oversized max_tokens must never cross the
        layer boundary — the inner graph keeps its built-in behaviour."""
        monkeypatch.setattr(
            "src.graph.graph._runtime_config",
            lambda: {"llm": {"temperature": float("nan"), "max_tokens": math.inf, "system_prompt_template": 42}},
        )
        forwarded = MaintenanceReportGraphNode()._parent_config()
        assert forwarded == {}
