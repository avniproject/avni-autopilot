"""Adapter — LangGraph `astream` events → SSE events on the session bus.

One `ChatService` per session. Wraps the existing chat ReAct agent built by
`chat.agent.build_agent` (the same agent the REPL uses) and exposes three
operations:

  send_message(text)   Run one user turn through the agent.
  resolve(...)         Translate structured HITL resolutions into a
                       message the agent processes by calling
                       `resume_bundle` itself.
  cancel()             Cancel the in-flight turn.

The adapter does not own the graph or the checkpointer; `build_agent` does.
It does not own validation, generation, or rule-writing; those are existing
modules the agent's tools already invoke. Per SDD §2 "out of scope" — this
file is pure transport.

Event translation contract (SDD §4.2):

  AIMessage(content=text)           → publishes one `agent.message`
  AIMessage(tool_calls=[...])       → publishes one `tool.call` per call
  ToolMessage(content=...)          → publishes `tool.result`; additionally:
    status == "needs_confirmation"  → publishes `hitl.pending`, stores the
                                      interrupt context for `resolve(...)`
    status == "done" and zip_path   → publishes `bundle.ready`, captures
                                      bundle_path on the session
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from pathlib import Path
from typing import Any, Callable

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from web.events import (
    agent_message,
    bundle_ready,
    error_payload,
    hitl_pending,
    tool_call,
    tool_result,
)
from web.sessions import ChatSession

log = logging.getLogger(__name__)


# Max characters of a tool result preserved in the `tool.result` summary.
# The full result is also delivered through the agent's next `agent.message`
# (where the model summarises what happened), so this is just a debug aid.
TOOL_RESULT_SUMMARY_MAX = 400


# ── Helpers ──────────────────────────────────────────────────────────────────


def _extract_text(content: Any) -> str:
    """Anthropic content can be str or list of {type, text}. Concatenate text.

    Mirrors `chat.repl._extract_text` so the wire output for an `agent.message`
    matches what the REPL prints today.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                t = block.get("text") or ""
                if t:
                    out.append(t)
        return "".join(out)
    return ""


def _parse_tool_result(content: Any) -> dict[str, Any] | None:
    """Parse a `ToolMessage.content` back into a dict, if possible.

    LangChain serialises tool returns to JSON strings on the message; the
    structured-output detection (needs_confirmation, done + zip_path) needs
    the dict back. Returns `None` when the content isn't a recognisable
    JSON object — non-fatal, the tool.result event still fires.
    """
    if isinstance(content, dict):
        return content
    if isinstance(content, str):
        try:
            parsed = json.loads(content)
        except (ValueError, json.JSONDecodeError):
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _summarize_tool_result(content: Any) -> str:
    """One-line summary for the `tool.result` SSE event."""
    if isinstance(content, str):
        return content[:TOOL_RESULT_SUMMARY_MAX]
    try:
        return json.dumps(content)[:TOOL_RESULT_SUMMARY_MAX]
    except (TypeError, ValueError):
        return repr(content)[:TOOL_RESULT_SUMMARY_MAX]


def _build_org_context(session: ChatSession) -> str:
    """Session-specific block appended to the agent's system prompt.

    Tells the agent which org the signed-in user belongs to so it can call
    `generate_bundle(org=…)`, `edit_bundle_from_spec(org=…)`, etc. without
    asking the user. The slug is derived from `/web/userInfo`'s
    `organisationName` (NOT user-supplied), so the agent cannot be coerced
    into cross-org access by anything the user types.
    """
    return (
        f"The signed-in user is an admin for the **{session.org_name}** "
        f"organisation. Its folder slug is `{session.org_slug}`. "
        f"Whenever a tool takes an `org` argument (generate_bundle, "
        f"edit_bundle_from_spec, resume_bundle's underlying flow), use "
        f"`{session.org_slug}` — do NOT prompt the user for the org name; "
        f"it is implicit from the session.\n\n"
        f"Their uploaded scoping/modelling .xlsx files have already been "
        f"placed under `resources/input/{session.org_slug}/`. The generated "
        f"bundle ZIP will land at "
        f"`resources/output/{session.org_slug}/{session.org_name.capitalize()}.zip`."
    )


# ── ChatService ──────────────────────────────────────────────────────────────


class ChatService:
    """Per-session adapter over the chat ReAct agent.

    Constructed once per `ChatSession`; lifetime matches the session's. Holds
    a reference to the session record so it can publish events to its bus
    and update `bundle_path` when the agent finishes a generation.

    `agent_factory` defaults to `chat.agent.build_agent` but is injectable so
    tests can substitute a stub that drives a canned event stream.
    """

    def __init__(
        self,
        session: ChatSession,
        agent_factory: Callable[[], Any] | None = None,
    ) -> None:
        if agent_factory is None:
            # Late import — `chat.agent` pulls in ChatAnthropic which fails
            # without ANTHROPIC_API_KEY. Importing here lets unit tests
            # construct ChatService with a stub before the env is set.
            from chat.agent import build_agent
            # Inject session-specific org context into the system prompt so
            # the agent never asks the user for an org name (the org is
            # implicit from their authenticated session).
            agent_factory = lambda: build_agent(  # noqa: E731
                extra_context=_build_org_context(session),
            )
        self._session = session
        self._agent = agent_factory()
        self._config: dict[str, Any] = {
            "configurable": {"thread_id": session.session_id},
        }
        # interrupt_id → {"thread_id": str, "changes": list[dict]}.
        # Populated when a tool returns `needs_confirmation`; consumed by
        # `resolve(...)` so the browser-side response can be matched back to
        # the right pause point.
        self._pending_interrupts: dict[str, dict[str, Any]] = {}
        self._running_task: asyncio.Task[None] | None = None

    # ── Public API ──────────────────────────────────────────────────────────

    async def send_message(self, text: str) -> None:
        """Start one user turn. Returns immediately; events stream to the bus.

        If a previous turn is still running, emits a recoverable `error`
        event and drops the new message — the SDD §9 "concurrent tab" rule
        already gates a second consumer, so this is defence in depth.
        """
        if self._running_task and not self._running_task.done():
            self._session.bus.publish(
                "error",
                error_payload(
                    "E_BUSY",
                    "agent is still processing the previous turn",
                    recoverable=True,
                ),
            )
            return
        self._running_task = asyncio.create_task(self._run_turn(text))

    async def resolve(
        self, interrupt_id: str, resolutions: list[dict[str, Any]],
    ) -> bool:
        """Translate structured HITL resolutions into an agent turn.

        Returns False (caller should 400) when the interrupt_id is unknown
        — either it was never issued or it was already resolved.

        Implementation: format the resolutions as a structured message and
        feed them to the agent. The agent then calls `resume_bundle` on its
        own (the same path the REPL uses today, see chat/tools.py:184).
        This keeps the agent as the single orchestrator and lets it emit a
        coherent "applied N changes" follow-up message.
        """
        context = self._pending_interrupts.pop(interrupt_id, None)
        if context is None:
            return False

        # Let the interrupt-emitting turn finish flushing before queuing the
        # resume — otherwise `send_message`'s busy guard races and rejects
        # the resolution as E_BUSY. Exception is swallowed so a failed prior
        # turn doesn't permanently block resolution.
        if self._running_task and not self._running_task.done():
            try:
                await self._running_task
            except Exception:  # noqa: BLE001
                pass

        lines = ["My decisions on the pending changes:"]
        for r in resolutions:
            change_id = r.get("change_id", "")
            decision = (r.get("decision") or "").lower()
            value = r.get("value", "")
            if not change_id or decision not in {"yes", "no", "edit"}:
                continue
            if decision == "edit":
                lines.append(f"- {change_id}: edit:{value}")
            else:
                lines.append(f"- {change_id}: {decision}")
        await self.send_message("\n".join(lines))
        return True

    async def cancel(self) -> None:
        """Cancel the in-flight turn, if any. Idempotent."""
        task = self._running_task
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass

    @property
    def has_pending_interrupt(self) -> bool:
        """True when at least one HITL interrupt is awaiting `resolve(...)`."""
        return bool(self._pending_interrupts)

    # ── Internals ───────────────────────────────────────────────────────────

    async def _run_turn(self, text: str) -> None:
        """Drive `agent.astream` for one turn, translating updates to events."""
        try:
            async for chunk in self._agent.astream(
                {"messages": [HumanMessage(content=text)]},
                config=self._config,
                stream_mode="updates",
            ):
                self._handle_update(chunk)
        except asyncio.CancelledError:
            self._session.bus.publish(
                "error",
                error_payload("E_CANCELLED", "turn cancelled", recoverable=True),
            )
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("agent turn failed sid=%s", self._session.session_id)
            self._session.bus.publish(
                "error",
                error_payload("E_AGENT", str(exc), recoverable=False),
            )
            # Terminal — the in-process MemorySaver may be in an
            # inconsistent state; close the bus per SDD §9.
            self._session.bus.close("agent error")

    def _handle_update(self, chunk: dict[str, Any]) -> None:
        """One `updates` chunk → zero or more SSE events.

        Chunk shape (LangGraph): `{node_name: {"messages": [Message, ...]}}`.
        We care about the `agent` node (AI/Tool messages) and the `tools` node
        (ToolMessage). Other nodes — currently none for this graph — are
        ignored so a future graph addition degrades gracefully.
        """
        for _node, state_update in chunk.items():
            if not state_update:
                continue
            # LangGraph 1.x emits non-dict values for special chunks like
            # `__interrupt__` (tuple of Interrupt objects). HITL in this graph
            # is surfaced via the `needs_confirmation` tool result instead, so
            # skip everything that isn't a node-state dict.
            if not isinstance(state_update, dict):
                continue
            for msg in state_update.get("messages", []) or []:
                self._handle_message(msg)

    def _handle_message(self, msg: Any) -> None:
        bus = self._session.bus
        if isinstance(msg, AIMessage):
            # Tool calls come first — the agent issued them in this same
            # message; the human-readable text (if any) explains them.
            for tc in msg.tool_calls or []:
                bus.publish(
                    "tool.call",
                    tool_call(
                        tool=tc.get("name", ""),
                        args=tc.get("args") or {},
                        call_id=tc.get("id") or "",
                    ),
                )
            text = _extract_text(msg.content)
            if text:
                bus.publish("agent.message", agent_message("assistant", text))
            return
        if isinstance(msg, ToolMessage):
            self._handle_tool_message(msg)
            return
        # HumanMessage and SystemMessage are echoes of input — skip.

    def _handle_tool_message(self, msg: ToolMessage) -> None:
        bus = self._session.bus
        result = _parse_tool_result(msg.content)
        ok = True
        if result is not None:
            status = result.get("status", "")
            ok = status not in {"error", "rejected"}
        bus.publish(
            "tool.result",
            tool_result(
                call_id=msg.tool_call_id or "",
                ok=ok,
                summary=_summarize_tool_result(msg.content),
            ),
        )
        if result is None:
            return

        status = result.get("status", "")
        if status == "needs_confirmation":
            self._publish_hitl_pending(result)
        elif status == "done" and result.get("zip_path"):
            self._publish_bundle_ready(result)

    def _publish_hitl_pending(self, result: dict[str, Any]) -> None:
        """Convert a `needs_confirmation` tool result into an `hitl.pending` event."""
        payload = result.get("payload") or {}
        changes = payload.get("changes") or []
        interrupt_id = secrets.token_urlsafe(8)
        self._pending_interrupts[interrupt_id] = {
            "thread_id": result.get("thread_id", ""),
            "changes": changes,
        }
        self._session.bus.publish(
            "hitl.pending", hitl_pending(interrupt_id, changes),
        )

    def _publish_bundle_ready(self, result: dict[str, Any]) -> None:
        """Convert a successful generate/edit tool return into `bundle.ready`."""
        zip_path = result.get("zip_path", "")
        self._session.bundle_path = Path(zip_path) if zip_path else None
        # The summary the REPL prints today (subject_types, programs, etc.)
        # is already in `result` — pass it through unchanged for the browser.
        summary = {
            k: v for k, v in result.items()
            if k in {
                "subject_types", "programs", "encounter_types",
                "main_forms", "cancellation_forms", "concepts",
                "form_mappings", "applied_changes", "enrich_warnings",
                "parse_warnings", "errors", "mode", "org",
            }
        }
        self._session.bus.publish(
            "bundle.ready", bundle_ready(zip_path, summary),
        )
