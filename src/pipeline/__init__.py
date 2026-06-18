"""Bundle generation pipeline (LangGraph).

Public API:
  - build_graph(checkpointer=None)  → compiled LangGraph
  - initial_state(org, in_dir, out_dir, user_instructions=None) → BundleState
  - BundleState — TypedDict that flows through the graph
"""

from pipeline.graph import build_graph
from pipeline.state import BundleState, initial_state

__all__ = ["build_graph", "initial_state", "BundleState"]
