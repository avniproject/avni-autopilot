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
    status == "done" and            → publishes `bundle.ready` for the bundle
      bundle_updated                  the tool handed to the session
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from pathlib import Path
from typing import Any, Callable

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from config import settings
from domain.bundle_editor import BundleOrigin, classify_bundle
from domain.spreadsheets import find_spreadsheets
from domain.rules.unchecked_rule_code import mark_unchecked_rule_code, normalize_rule_code
from web.events import (
    agent_message,
    bundle_ready,
    error_payload,
    hitl_pending,
    tool_call,
    tool_result,
)
from web.sessions import ChatSession, OrgSetupStatus

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
    structured-output detection (needs_confirmation, done + bundle_updated) needs
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


def _uploaded_spreadsheet_names(session: ChatSession) -> list[str]:
    """Names of the .xlsx workbooks uploaded for this org."""
    input_dir = Path(settings.input_root) / session.org_slug
    return [Path(p).name for p in find_spreadsheets(str(input_dir))]


def _spreadsheet_context(session: ChatSession) -> str:
    names = _uploaded_spreadsheet_names(session)
    if names:
        return (
            "Spreadsheets uploaded in this chat: "
            + ", ".join(f"`{n}`" for n in names) + "."
        )
    return (
        "No spreadsheet has been uploaded in this chat. Never say a "
        "spreadsheet or file is uploaded or in place. To build or update "
        "the app from spreadsheets, ask the user to upload their scoping / "
        "modelling .xlsx files first, using the upload area at the top of "
        "the chat."
    )


_SEPARATE_BUILD_WARNING = (
    "A build from spreadsheets (generate_bundle) makes a separate, new set "
    "of forms; uploading it adds them next to whatever the organisation "
    "already has and can create duplicate forms and questions. Before "
    "building, tell the user this in plain words and build only if they "
    "still want it, passing confirmed_separate_build=true."
)


def _bundle_origin_context(origin: BundleOrigin) -> str:
    if origin is BundleOrigin.AUTOPILOT:
        return (
            "Its forms were built by Autopilot, so a request to make, update "
            "or rebuild the app updates them (edit_bundle_from_spec from "
            "spreadsheets, edit_bundle_fields for named field changes). Use "
            "generate_bundle only when the user explicitly asks to start "
            "from scratch."
        )
    if origin is BundleOrigin.NO_FORMS:
        return (
            "It has no forms yet, so there is nothing to update: build the "
            "app from spreadsheets with generate_bundle."
        )
    return (
        "Its forms were set up outside Autopilot (for example in App "
        "Designer), so Autopilot cannot update them from spreadsheets — "
        "never offer or call edit_bundle_from_spec for it. Named field "
        "changes (edit_bundle_fields), field lists and rule suggestions do "
        f"work on it. {_SEPARATE_BUILD_WARNING}"
    )


def _bundle_context(session: ChatSession) -> str:
    """What the chat may do with the session's bundle, and why."""
    status = session.org_setup_status
    lines: list[str] = []
    if status is OrgSetupStatus.FAILED:
        lines.append(
            "The organisation's current setup could NOT be loaded because "
            f"{session.org_setup_error}. The user has been shown this reason; "
            "repeat it when it matters. Do not act as if the organisation has "
            "no setup — it may well have one."
        )
    if session.bundle_changed_in_chat:
        origin = session.bundle_origin
        lines.append(
            "A bundle was built or edited in this chat and is the one the "
            "tools now use. Its card, with a Download button and an Upload "
            "to org button where the organisation allows it, is shown in "
            "the chat."
            + (f" {_bundle_origin_context(origin)}" if origin is not None else "")
        )
        return "\n- ".join(lines)
    if status is OrgSetupStatus.LOADING:
        lines.append(
            "The organisation's current setup is still being loaded. If the "
            "user asks to build, update or inspect the app, say it is still "
            "loading and ask them to try again in a moment."
        )
    elif status is OrgSetupStatus.FAILED:
        lines.append(
            "Field lists and edits of the current setup are unavailable, and "
            f"there is no bundle to update. {_SEPARATE_BUILD_WARNING}"
        )
    else:
        lines.append(
            "The organisation's current setup is loaded. "
            f"{_bundle_origin_context(session.bundle_origin)}"
        )
    lines.append("No bundle has been built or edited in this chat yet.")
    return "\n- ".join(lines)


def _build_org_context(session: ChatSession) -> str:
    """Session-specific block appended to the agent's system prompt.

    Rebuilt before every model call so it reflects the session's current
    state: uploaded spreadsheets, whether the org's current bundle loaded
    (and why not), and whether a bundle card is in the chat. The slug is
    derived from `/web/userInfo`'s `organisationName` (NOT user-supplied),
    so the agent cannot be coerced into cross-org access by anything the
    user types.
    """
    return (
        f"The signed-in user is an admin for the **{session.org_name}** "
        f"organisation. Its folder slug is `{session.org_slug}`. "
        f"Whenever a tool takes an `org` argument (generate_bundle, "
        f"edit_bundle_from_spec), use `{session.org_slug}` — do NOT prompt "
        f"the user for the org name; it is implicit from the session.\n\n"
        f"Current state of this chat:\n"
        f"- {_spreadsheet_context(session)}\n"
        f"- {_bundle_context(session)}"
    )


# ── ChatService ──────────────────────────────────────────────────────────────


class ChatService:
    """Per-session adapter over the chat ReAct agent.

    Constructed once per `ChatSession`; lifetime matches the session's. Holds
    a reference to the session record so it can publish events to its bus
    and record the bundle state when a tool changes the bundle.

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
                session_context=lambda: _build_org_context(session),
            )
        self._session = session
        self._agent = agent_factory()
        self._config: dict[str, Any] = {
            "configurable": {
                "thread_id": session.session_id,
                "session": session,
            },
        }
        # interrupt_id → {"thread_id": str, "changes": list[dict]}.
        # Populated when a tool returns `needs_confirmation`; consumed by
        # `resolve(...)` so the browser-side response can be matched back to
        # the right pause point.
        self._pending_interrupts: dict[str, dict[str, Any]] = {}
        self._running_task: asyncio.Task[None] | None = None
        # Normalised JS of every rule a `suggest_*` tool returned as
        # "suggested"; any other rule code in a reply is marked not checked.
        self._checked_rule_code: set[str] = set()

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
                    "agent is still processing the previous question",
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

        thread_id = context.get("thread_id", "")
        lines = [
            "My decisions on the pending changes for the bundle run "
            f"(call resume_bundle with thread_id={thread_id!r}):"
        ]
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
        log.info(
            "resolve sid=%s interrupt_id=%s thread_id=%s n=%d",
            self._session.session_id, interrupt_id, thread_id, len(resolutions),
        )
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
                text = mark_unchecked_rule_code(text, self._checked_rule_code)
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
        if status == "suggested" and result.get("js"):
            self._checked_rule_code.add(normalize_rule_code(result["js"]))
        if status == "needs_confirmation":
            self._publish_hitl_pending(result)
        elif status == "done" and result.get("bundle_updated"):
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
        """Convert a generate/edit tool return that changed the session's bundle into `bundle.ready`."""
        zip_path = str(self._session.bundle_path)
        try:
            self._session.bundle_origin = classify_bundle(zip_path)
        except FileNotFoundError as exc:
            log.warning(f"bundle origin unknown sid={self._session.session_id}: {exc}")
            self._session.bundle_origin = None
        self._session.bundle_changed_in_chat = True
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
        # Log every parse / enrich warning and error individually so they
        # are recorded in the backend log (greppable by session id) even
        # though the inline `BundleSummary` card no longer surfaces
        # warnings — it stays quiet for showcase recordings while errors
        # remain visible there.
        sid = self._session.session_id
        for warning in summary.get("parse_warnings") or []:
            log.info(f"bundle.warning sid={sid} parse: {warning}")
        for warning in summary.get("enrich_warnings") or []:
            log.info(f"bundle.warning sid={sid} enrich: {warning}")
        for err in summary.get("errors") or []:
            log.error(f"bundle.error sid={sid}: {err}")
        self._session.bus.publish(
            "bundle.ready", bundle_ready(zip_path, summary),
        )
