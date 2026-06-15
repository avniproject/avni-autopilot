"""Chat ReAct agent build."""

from __future__ import annotations

from langchain_anthropic import ChatAnthropic
from langgraph.checkpoint.memory import MemorySaver
from langgraph.prebuilt import create_react_agent

from chat.prompts import SYSTEM_PROMPT
from chat.tools import TOOLS
from config import settings


def build_agent(extra_context: str = ""):
    """Build the chat ReAct agent.

    `extra_context` is appended to the system prompt. The web service uses
    this to tell the agent which org the user is signed in as (so it does
    not prompt for an org name on every generate request). The REPL passes
    no extra context — its behaviour is unchanged.
    """
    model = ChatAnthropic(
        model=settings.model,
        max_tokens=settings.max_tokens,
        thinking={"type": "adaptive"},
    )
    prompt = SYSTEM_PROMPT
    if extra_context:
        prompt = f"{SYSTEM_PROMPT}\n\n## Session context\n\n{extra_context}"
    return create_react_agent(
        model=model,
        tools=TOOLS,
        prompt=prompt,
        checkpointer=MemorySaver(),
    )
