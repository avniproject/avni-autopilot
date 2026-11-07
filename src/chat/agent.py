"""Chat ReAct agent build."""

from __future__ import annotations

from typing import Callable

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import BaseMessage, SystemMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.prebuilt import create_react_agent

from chat.prompts import SYSTEM_PROMPT
from chat.tools import TOOLS
from config import settings


def compose_system_prompt(session_context: str = "") -> str:
    """System prompt with an optional `## Session context` section appended."""
    if not session_context:
        return SYSTEM_PROMPT
    return f"{SYSTEM_PROMPT}\n\n## Session context\n\n{session_context}"


def build_agent(session_context: Callable[[], str] | None = None):
    """Build the chat ReAct agent.

    `session_context` is called before every model call and its text is
    appended to the system prompt. The web service uses it to tell the agent
    the signed-in org and the session's live state (uploaded spreadsheets,
    whether the org's bundle loaded), which change during a chat. The REPL
    passes none.
    """
    model = ChatAnthropic(
        model=settings.model,
        max_tokens=settings.max_tokens,
        thinking={"type": "adaptive"},
    )
    if session_context is None:
        prompt: str | Callable[[dict], list[BaseMessage]] = SYSTEM_PROMPT
    else:
        def prompt(state: dict) -> list[BaseMessage]:
            system = SystemMessage(content=compose_system_prompt(session_context()))
            return [system, *state["messages"]]
    return create_react_agent(
        model=model,
        tools=TOOLS,
        prompt=prompt,
        checkpointer=MemorySaver(),
    )
