"""AgentCore Platform v1.0"""

# src/graph/context_bridge.py — carries the validated inspection record across
# the outer→inner graph boundary.
#
# Why this exists: GraphNode.execute() (framework, SDK 1.0.1) invokes the inner
# graph as `subgraph.invoke(user_input, session_id=..., ctx=...)` WITHOUT
# forwarding the outer state's input_context — and the string channel
# (user_input / validated_input) is PII-masked at every node's input gate, so
# a structured record travelling as a string would arrive with two-word
# Title-Case labels (component names, inspector names) rewritten to a mask
# sentinel. The record therefore crosses the boundary out-of-band, via the
# sanctioned subclass hooks:
#
#   MaintenanceReportGraphNode.extract_input(state)  [runs BEFORE subgraph.invoke]
#       → set_caller_input_context({"inspection": <validated record>})
#   DomainWorkflowGraph._extra_initial_state()       [runs INSIDE subgraph.invoke]
#       → returns {"input_context": get_caller_input_context()}
#
# A ContextVar keeps the hand-off correct per thread/task, so concurrent
# invocations in one process cannot see each other's record.

from contextvars import ContextVar
from typing import Any

_CALLER_INPUT_CONTEXT: ContextVar[dict[str, Any] | None] = ContextVar("mfg_c2_014_caller_input_context", default=None)


def set_caller_input_context(input_context: dict[str, Any] | None) -> None:
    """Stash the outer graph's context for the imminent inner-graph invoke."""
    set_value = dict(input_context) if input_context else {}
    _CALLER_INPUT_CONTEXT.set(set_value)


def get_caller_input_context() -> dict[str, Any]:
    """Read (without consuming) the stashed context; {} when none was set."""
    return _CALLER_INPUT_CONTEXT.get() or {}
