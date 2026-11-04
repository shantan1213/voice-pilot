"""Raala agent-tool framework.

Calendar (and later Gmail/Tasks/Sheets) integrations register a strict,
validated set of actions here. The voice pipeline in `app.main` stays
untouched: it asks the registry what is available and executes only
registered, validated actions. The LLM never runs code or touches tokens.
"""

from app.agent.action_models import Selection
from app.agent.confirmation import ConfirmationStore, PendingAction
from app.agent.tool_executor import ToolResult, execute_tool
from app.agent.tool_registry import ToolRegistry

__all__ = [
    "ConfirmationStore",
    "PendingAction",
    "Selection",
    "ToolRegistry",
    "ToolResult",
    "execute_tool",
]
