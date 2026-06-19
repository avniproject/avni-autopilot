"""Bundle generation pipeline (LangGraph).

Public API:
  - build_phase1_graph()  → compiled LangGraph for parse + enrich
  - build_phase2_graph()  → compiled LangGraph for apply + generate + package
  - initial_state(org, in_dir, out_dir, user_instructions=None) → BundleState
  - BundleState — TypedDict that flows through the graphs
"""

from pipeline.graph import build_phase1_graph, build_phase2_graph
from pipeline.state import BundleState, initial_state

__all__ = ["build_phase1_graph", "build_phase2_graph", "initial_state", "BundleState"]
