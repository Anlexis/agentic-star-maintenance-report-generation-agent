"""AgentCore Platform v1.0"""

# AgentRegistry manifest discovery (config/agent.yaml: module: "src.graph",
# class: "MaintenanceReportGeneratorAgent") imports this package and looks up the
# declared class on it. Re-export the outer graph class (and the `Graph` alias
# used by server.py) so that
# getattr(import_module("src.graph"), "MaintenanceReportGeneratorAgent") resolves.
# Without this re-export the declared class would be absent from the package
# namespace and manifest-based loading would fail.

from src.graph.graph import Graph, MaintenanceReportGeneratorAgent

__all__ = ["MaintenanceReportGeneratorAgent", "Graph"]
