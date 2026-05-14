"""Chat ReAct agent + REPL.

Public API:
  - run_chat()  → start the interactive REPL
  - build_agent() → construct the LangGraph ReAct agent
  - TOOLS       → list of @tool functions
"""

from chat.agent import build_agent
from chat.repl import run_chat
from chat.tools import TOOLS

__all__ = ["build_agent", "run_chat", "TOOLS"]
