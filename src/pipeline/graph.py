"""
LangGraph wiring + runtime entry points for the bundle generation pipeline.

Reads node implementations from `pipeline.nodes`; state schema from
`pipeline.state`.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, StateGraph

from config import settings

log = logging.getLogger(__name__)

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


def build_graph(checkpointer=None) -> Any:
    graph = StateGraph(BundleState)

    graph.add_node("discover_files", discover_files)
    graph.add_node("parse_documents", parse_documents)
    graph.add_node("link_forms_to_entities", link_forms_to_entities)
    graph.add_node("enrich_with_llm", enrich_with_llm)
    graph.add_node("apply_user_decisions", apply_user_decisions)
    graph.add_node("generate_entities", generate_entities)
    graph.add_node("generate_forms", generate_forms)
    graph.add_node("generate_form_mappings", generate_form_mappings)
    graph.add_node("generate_rules", generate_rules)
    graph.add_node("package_zip", package_zip)
    # Edit-from-spec branch (BUNDLE_EDIT_FROM_SPEC_SDD)
    graph.add_node("diff_against_bundle", diff_against_bundle)
    graph.add_node("apply_diff_edits", apply_diff_edits)

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
    graph.add_edge("enrich_with_llm", "apply_user_decisions")
    graph.add_edge("apply_user_decisions", "generate_entities")
    graph.add_edge("generate_entities", "generate_forms")
    graph.add_edge("generate_forms", "generate_form_mappings")
    graph.add_edge("generate_form_mappings", "generate_rules")
    # Branch on mode after the desired bundle is fully realized.
    graph.add_conditional_edges(
        "generate_rules", _route_after_generation,
        {"generate": "package_zip", "edit_from_spec": "diff_against_bundle"},
    )
    graph.add_edge("package_zip", END)
    graph.add_edge("diff_against_bundle", "apply_diff_edits")
    graph.add_edge("apply_diff_edits", END)

    # Declarative interrupt: the framework pauses BEFORE apply_user_decisions
    # and persists state via the checkpointer. This avoids the LangGraph 1.2
    # dynamic-interrupt() persistence gap (observed: GraphInterrupt raised but
    # SqliteSaver receives no put()). Resolutions are written via update_state
    # by chat_service.resolve before invoke(None, config) resumes.
    return graph.compile(
        checkpointer=checkpointer or _default_checkpointer(),
        interrupt_before=["apply_user_decisions"],
    )


def _default_checkpointer() -> SqliteSaver:
    """SqliteSaver pointing at the file in `settings.checkpoint_db_path`.

    Persists across process restarts so a user mid-HITL doesn't lose the
    paused state on deploy / OOM. The connection uses
    `check_same_thread=False` because LangChain executes sync tools in
    a thread executor — the writer is still serialised by SQLite's
    file lock; the flag just allows reads/writes from the executor thread.
    """
    path = settings.checkpoint_db_path
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    saver = SqliteSaver(conn)
    saver.setup()
    log.info(f"pipeline checkpointer: SqliteSaver({path})")
    return saver
