"""Registry of available agent tools.

Only registered actions can ever execute. The registry exposes OpenAI-style
`tools` definitions for the Muse Spark action-selection call, and future
integrations (Gmail, Tasks, Sheets) register here without touching the voice
pipeline. No dummy registrations: unimplemented services stay absent.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

#: Async handler: validated args + context -> plain-JSON-serializable result.
ToolHandler = Callable[[BaseModel | dict, dict], Awaitable[dict]]


@dataclass
class ToolEntry:
    name: str
    description: str
    args_model: type[BaseModel]
    handler: ToolHandler
    needs_confirmation: bool = False
    parameters: dict = field(default_factory=dict)

    def openai_definition(self) -> dict:
        schema: dict[str, Any] = dict(self.parameters)
        if not schema and self.args_model is not BaseModel:
            schema = self.args_model.model_json_schema()
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": schema or {"type": "object", "properties": {}},
            },
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolEntry] = {}

    def register(
        self,
        name: str,
        description: str,
        args_model: type[BaseModel],
        handler: ToolHandler,
        *,
        needs_confirmation: bool = False,
        parameters: dict | None = None,
    ) -> None:
        self._tools[name] = ToolEntry(
            name=name,
            description=description,
            args_model=args_model,
            handler=handler,
            needs_confirmation=needs_confirmation,
            parameters=parameters or {},
        )

    def get(self, name: str) -> ToolEntry | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools.keys())

    def openai_tools(self) -> list[dict]:
        return [entry.openai_definition() for entry in self._tools.values()]
