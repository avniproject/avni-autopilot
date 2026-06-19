"""
LangGraph wiring for the bundle generation pipeline.

The pipeline is split into two compiled graphs around the HITL gate:

- Phase 1: `discover_files → parse_documents → link_forms_to_entities →
  enrich_with_llm → END`. Produces a state with `pending_changes` populated
  when human-in-the-loop decisions are required, or an empty list when no
  decisions are needed.
- Phase 2: `apply_user_decisions → generate_entities → generate_forms →
  generate_form_mappings → generate_rules → package_zip` (or the edit-from-
  spec branch). Consumes `user_resolutions` from state, applies them, and
  finishes the bundle.

The chat tool (`chat.tools`) drives both phases and keeps the saved state
between them in a module-level dict. This avoids LangGraph 1.x's HITL
machinery (both dynamic `interrupt()` and declarative `interrupt_before`
observed to fail in our async-from-thread runtime — `interrupt()` raised
without persisting, `interrupt_before` hung indefinitely inside ainvoke).
Neither phase uses a checkpointer; state lives where we can debug it.
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, StateGraph

from pipeline.nodes import (
    apply_diff_edits,
    apply_user_decisions,
    diff_against_bundle,
    discover_files,
    enrich_with_llm,
    generate_entities,
    generate_form_mappings,
    generate_forms,
    generate_rules,
    link_forms_to_entities,
    package_zip,
    parse_documents,
)
from pipeline.state import BundleState, initial_state


def _can_proceed(state: BundleState) -> str:
    if state.get("errors") and not state.get("file_paths"):
        return "abort"
    if state.get("errors") and state.get("entity_spec") is None:
        return "abort"
    return "continue"


def _route_after_generation(state: BundleState) -> str:
    """Branch after the desired bundle is fully realized in state.

    Generate mode → write a fresh bundle (package_zip).
    Edit-from-spec mode → diff against existing bundle, apply edits.
    """
    return "edit_from_spec" if state.get("mode") == "edit_from_spec" else "generate"


def build_phase1_graph() -> Any:
    """Parse + enrich. Ends with `pending_changes` populated when HITL needed."""
    graph = StateGraph(BundleState)

    graph.add_node("discover_files", discover_files)
    graph.add_node("parse_documents", parse_documents)
    graph.add_node("link_forms_to_entities", link_forms_to_entities)
    graph.add_node("enrich_with_llm", enrich_with_llm)

    graph.set_entry_point("discover_files")
    graph.add_conditional_edges(
        "discover_files", _can_proceed,
        {"continue": "parse_documents", "abort": END},
    )
    graph.add_conditional_edges(
        "parse_documents", _can_proceed,
        {"continue": "link_forms_to_entities", "abort": END},
    )
    graph.add_edge("link_forms_to_entities", "enrich_with_llm")
    graph.add_edge("enrich_with_llm", END)

    return graph.compile()


def build_phase2_graph() -> Any:
    """Apply HITL decisions + generate JSON + package. Reads `user_resolutions`."""
    graph = StateGraph(BundleState)

    graph.add_node("apply_user_decisions", apply_user_decisions)
    graph.add_node("generate_entities", generate_entities)
    graph.add_node("generate_forms", generate_forms)
    graph.add_node("generate_form_mappings", generate_form_mappings)
    graph.add_node("generate_rules", generate_rules)
    graph.add_node("package_zip", package_zip)
    graph.add_node("diff_against_bundle", diff_against_bundle)
    graph.add_node("apply_diff_edits", apply_diff_edits)

    graph.set_entry_point("apply_user_decisions")
    graph.add_edge("apply_user_decisions", "generate_entities")
    graph.add_edge("generate_entities", "generate_forms")
    graph.add_edge("generate_forms", "generate_form_mappings")
    graph.add_edge("generate_form_mappings", "generate_rules")
    graph.add_conditional_edges(
        "generate_rules", _route_after_generation,
        {"generate": "package_zip", "edit_from_spec": "diff_against_bundle"},
    )
    graph.add_edge("package_zip", END)
    graph.add_edge("diff_against_bundle", "apply_diff_edits")
    graph.add_edge("apply_diff_edits", END)

    return graph.compile()


