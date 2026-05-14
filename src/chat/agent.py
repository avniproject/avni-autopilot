"""Chat ReAct agent build."""

from __future__ import annotations

from langchain_anthropic import ChatAnthropic
from langgraph.checkpoint.memory import MemorySaver
from langgraph.prebuilt import create_react_agent

from chat.prompts import SYSTEM_PROMPT
from chat.tools import TOOLS
from config import settings


def build_agent():
    model = ChatAnthropic(
        model=settings.model,
        max_tokens=settings.max_tokens,
        thinking={"type": "adaptive"},
    )
    return create_react_agent(
        model=model,
        tools=TOOLS,
        prompt=SYSTEM_PROMPT,
        checkpointer=MemorySaver(),
    )
