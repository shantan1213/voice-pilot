"""Validated tool execution.

Every call passes through: registered? args valid? Google connected?
Only then does the integration handler run, and its errors are mapped to
structured codes. Nothing here executes LLM-generated code or touches
arbitrary endpoints.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from app.agent.tool_registry import ToolRegistry


@dataclass
class ToolResult:
    ok: bool
    code: str
    message: str
    data: dict[str, Any] | None = None
    needs_confirmation: bool = False
    proposal: str = ""
    arguments: dict[str, Any] | None = None


def _friendly_validation_error(exc: ValidationError) -> str:
    parts = []
    for error in exc.errors():
        location = ".".join(str(p) for p in error.get("loc", ())) or "arguments"
        parts.append(f"{location}: {error.get('msg', 'invalid')}")
    return "; ".join(parts)


async def execute_tool(
    registry: ToolRegistry,
    action: str,
    raw_arguments: dict[str, Any] | None,
    context: dict[str, Any],
) -> ToolResult:
    """Validate and run one registered action."""
    entry = registry.get(action)
    if entry is None:
        return ToolResult(
            ok=False,
            code="unsupported_action",
            message=(
                f"'{action}' is not a supported Calendar action. Supported: "
                + ", ".join(registry.names())
                + "."
            ),
        )
    try:
        if entry.args_model is BaseModel:
            validated: BaseModel | dict[str, Any] = {}
        else:
            validated = entry.args_model.model_validate(raw_arguments or {})
    except ValidationError as exc:
        return ToolResult(
            ok=False,
            code="invalid_arguments",
            message=f"Invalid arguments for '{action}': {_friendly_validation_error(exc)}",
        )
    except Exception as exc:
        return ToolResult(
            ok=False, code="invalid_arguments", message=f"Invalid arguments: {exc}"
        )
    try:
        outcome = await entry.handler(validated, context)
    except PermissionError as exc:
        return ToolResult(ok=False, code="not_connected", message=str(exc))
    except TimeoutError as exc:
        return ToolResult(ok=False, code="provider_timeout", message=str(exc))
    except ConnectionError as exc:
        return ToolResult(ok=False, code="network_error", message=str(exc))
    except ValueError as exc:
        return ToolResult(ok=False, code="invalid_request", message=str(exc))
    except RuntimeError as exc:
        return ToolResult(ok=False, code="provider_error", message=str(exc))
    if isinstance(outcome, ToolResult):
        return outcome
    if isinstance(outcome, dict):
        return ToolResult(ok=True, code="ok", message="Done.", data=outcome)
    return ToolResult(ok=True, code="ok", message=str(outcome))
