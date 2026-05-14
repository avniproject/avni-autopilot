"""
LangGraph wiring + runtime entry points for the bundle generation pipeline.

Reads node implementations from `pipeline.nodes`; state schema from
`pipeline.state`.
"""

from __future__ import annotations

from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph

from pipeline.nodes import (
    apply_user_decisions,
    discover_files,
    enrich_with_llm,
    generate_entities,
    generate_form_mappings,
    generate_forms,
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


def build_graph(checkpointer=None) -> Any:
    graph = StateGraph(BundleState)

    graph.add_node("discover_files", discover_files)
    graph.add_node("parse_documents", parse_documents)
    graph.add_node("enrich_with_llm", enrich_with_llm)
    graph.add_node("apply_user_decisions", apply_user_decisions)
    graph.add_node("generate_entities", generate_entities)
    graph.add_node("generate_forms", generate_forms)
    graph.add_node("generate_form_mappings", generate_form_mappings)
    graph.add_node("package_zip", package_zip)

    graph.set_entry_point("discover_files")
    graph.add_conditional_edges(
        "discover_files", _can_proceed,
        {"continue": "parse_documents", "abort": END},
    )
    graph.add_conditional_edges(
        "parse_documents", _can_proceed,
        {"continue": "enrich_with_llm", "abort": END},
    )
    graph.add_edge("enrich_with_llm", "apply_user_decisions")
    graph.add_edge("apply_user_decisions", "generate_entities")
    graph.add_edge("generate_entities", "generate_forms")
    graph.add_edge("generate_forms", "generate_form_mappings")
    graph.add_edge("generate_form_mappings", "package_zip")
    graph.add_edge("package_zip", END)

    # A checkpointer is required for `interrupt()` to be resumable.
    return graph.compile(checkpointer=checkpointer or MemorySaver())


def run(org_name: str, input_dir: str, output_dir: str,
        user_instructions: str | None = None,
        thread_id: str | None = None) -> BundleState:
    """Run the pipeline end-to-end. If it interrupts, the partial state is returned.

    The chat host typically uses `build_graph()` directly to stream events
    and handle interrupts; `run()` is a convenience for non-interactive
    callers (no API key, clean inputs, no confirmation needed).
    """
    pipeline = build_graph()
    config = {"configurable": {"thread_id": thread_id or f"thread-{org_name}"}}
    return pipeline.invoke(
        initial_state(org_name, input_dir, output_dir, user_instructions),
        config=config,
    )
