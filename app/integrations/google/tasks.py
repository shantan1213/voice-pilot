"""Google Tasks service: lists plus task CRUD-lite.

Reads run immediately; create/update/complete require explicit confirmation.
Task-list deletion and bulk operations are deliberately unsupported.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel

from app.agent import action_models as models
from app.agent.tool_executor import ToolResult
from app.agent.tool_registry import ToolRegistry
from app.integrations.google import oauth
from app.integrations.google import token_store as token_store_module

logger = logging.getLogger(__name__)

TASKS_API_BASE = "https://tasks.googleapis.com/tasks/v1"
TASKS_SCOPE = oauth.TASKS_SCOPE
REQUIRED_SCOPES = (TASKS_SCOPE,)

_MISSING_AUTH = (
    "Google Tasks is not authorized yet. "
    "Open /auth/google/login in your browser to grant Tasks access "
    "(your existing access is preserved)."
)


class TasksAPIError(RuntimeError):
    def __init__(self, code: str, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


def _check_scope(deps) -> None:
    granted = token_store_module.granted_scopes(deps.token_path)
    if granted and not any(scope in granted for scope in REQUIRED_SCOPES):
        raise PermissionError(_MISSING_AUTH)


async def _request(client, method: str, path: str, token: str, **kwargs) -> dict:
    headers = dict(kwargs.pop("headers", {}))
    headers["Authorization"] = f"Bearer {token}"
    response = await client.request(method, TASKS_API_BASE + path, headers=headers, **kwargs)
    if response.status_code == 401:
        raise TasksAPIError("auth_expired", "Google authorization expired.", status=401)
    if response.status_code == 403:
        raise PermissionError(_MISSING_AUTH)
    if response.status_code == 404:
        raise TasksAPIError("not_found", "The task or task list was not found.", status=404)
    if response.status_code == 429:
        raise TasksAPIError("rate_limited", "Tasks rate limit reached. Try again shortly.", status=429)
    if response.status_code < 200 or response.status_code >= 300:
        raise TasksAPIError(
            "provider_error", f"Google Tasks returned HTTP {response.status_code}.",
            status=response.status_code,
        )
    try:
        body = response.json()
    except ValueError as exc:
        raise TasksAPIError("provider_error", "Google Tasks returned invalid data.") from exc
    return body if isinstance(body, dict) else {}


async def call_tasks_api(client, method: str, path: str, ensure_token, **kwargs) -> dict:
    token = await ensure_token()
    try:
        return await _request(client, method, path, token, **kwargs)
    except TasksAPIError as exc:
        if exc.code != "auth_expired":
            raise
    token = await ensure_token(force_refresh=True)
    return await _request(client, method, path, token, **kwargs)


def _task_summary(task: dict) -> dict:
    return {
        "id": task.get("id", ""),
        "title": task.get("title", "(no title)"),
        "status": task.get("status", ""),
        "notes": task.get("notes", ""),
        "due": task.get("due", ""),
        "updated": task.get("updated", ""),
    }


async def list_task_lists(client, ensure_token) -> dict:
    body = await call_tasks_api(client, "GET", "/users/@me/lists", ensure_token)
    items = body.get("items", [])
    if not isinstance(items, list):
        items = []
    return {
        "count": len(items),
        "lists": [
            {"id": item.get("id", ""), "title": item.get("title", "")}
            for item in items if isinstance(item, dict)
        ],
    }


async def get_tasks(
    client,
    ensure_token,
    task_list_id: str,
    max_results: int,
    show_completed: bool,
    due_min=None,
    due_max=None,
) -> dict:
    params: dict[str, Any] = {
        "maxResults": str(max(1, min(max_results, 100))),
        "showCompleted": "true" if show_completed else "false",
    }
    if due_min is not None:
        params["dueMin"] = due_min.isoformat()
    if due_max is not None:
        params["dueMax"] = due_max.isoformat()
    body = await call_tasks_api(
        client, "GET", f"/users/@me/lists/{task_list_id}/tasks", ensure_token, params=params
    )
    items = body.get("items", [])
    if not isinstance(items, list):
        items = []
    summaries = [_task_summary(item) for item in items if isinstance(item, dict)]
    pending = [task for task in summaries if task["status"] != "completed"]
    return {
        "task_list_id": task_list_id,
        "count": len(summaries),
        "pending_count": len(pending),
        "tasks": summaries,
    }


async def get_task_details(client, ensure_token, task_list_id: str, task_id: str) -> dict:
    body = await call_tasks_api(
        client, "GET", f"/users/@me/lists/{task_list_id}/tasks/{task_id}", ensure_token
    )
    return _task_summary(body)


async def create_task(
    client, ensure_token, task_list_id: str, title: str, notes: str = "", due=None
) -> dict:
    payload: dict[str, Any] = {"title": title}
    if notes:
        payload["notes"] = notes
    if due is not None:
        payload["due"] = due.isoformat()
    body = await call_tasks_api(
        client, "POST", f"/users/@me/lists/{task_list_id}/tasks", ensure_token, json=payload
    )
    return _task_summary(body)


async def update_task(
    client,
    ensure_token,
    task_list_id: str,
    task_id: str,
    title=None,
    notes=None,
    due=None,
    status=None,
) -> dict:
    payload: dict[str, Any] = {}
    if title is not None:
        payload["title"] = title
    if notes is not None:
        payload["notes"] = notes
    if due is not None:
        payload["due"] = due.isoformat()
    if status is not None:
        payload["status"] = status
    body = await call_tasks_api(
        client,
        "PATCH",
        f"/users/@me/lists/{task_list_id}/tasks/{task_id}",
        ensure_token,
        json=payload,
    )
    return _task_summary(body)


async def complete_task(client, ensure_token, task_list_id: str, task_id: str) -> dict:
    return await update_task(
        client, ensure_token, task_list_id, task_id, status="completed"
    )


def _write_proposal(action: str, validated: BaseModel) -> tuple[str, dict]:
    if isinstance(validated, models.CreateTaskArgs):
        args: dict[str, Any] = {
            "task_list_id": validated.task_list_id,
            "title": validated.title,
            "notes": validated.notes,
            "due": validated.due.isoformat() if validated.due else "",
        }
        proposal = f"Should I add '{validated.title}'"
        if validated.due is not None:
            proposal += f" with a due date of {validated.due.strftime('%A, %Y-%m-%d').strip()}"
        return proposal + "? Reply with 'Confirm'. This request expires in 5 minutes.", args
    dumped = validated.model_dump(mode="json")
    if action == "update_task":
        return (
            f"Should I update that task? Reply with 'Confirm'. "
            f"This request expires in 5 minutes.",
            dumped,
        )
    return (
        f"Should I mark that task as completed? Reply with 'Confirm'. "
        f"This request expires in 5 minutes.",
        dumped,
    )


def register_tasks_tools(registry: ToolRegistry, deps) -> None:
    async def _lists(validated: BaseModel | dict, context: dict) -> dict:
        _check_scope(deps)
        async with deps.client_factory() as client:
            return await list_task_lists(client, deps.ensure_token)

    async def _tasks(validated: BaseModel | dict, context: dict) -> dict:
        assert isinstance(validated, models.ListTasksArgs)
        _check_scope(deps)
        async with deps.client_factory() as client:
            return await get_tasks(
                client,
                deps.ensure_token,
                validated.task_list_id,
                validated.max_results,
                validated.show_completed,
                validated.due_min,
                validated.due_max,
            )

    async def _details(validated: BaseModel | dict, context: dict) -> dict:
        assert isinstance(validated, models.TaskDetailsArgs)
        _check_scope(deps)
        async with deps.client_factory() as client:
            return await get_task_details(
                client, deps.ensure_token, validated.task_list_id, validated.task_id
            )

    async def _needs_confirmation(
        validated: BaseModel, action: str, context: dict
    ) -> ToolResult | dict:
        _check_scope(deps)
        if context.get("confirmed"):
            args = validated.model_dump(mode="json")
            async with deps.client_factory() as client:
                if action == "create_task":
                    assert isinstance(validated, models.CreateTaskArgs)
                    return await create_task(
                        client, deps.ensure_token, validated.task_list_id,
                        validated.title, validated.notes, validated.due,
                    )
                if action == "update_task":
                    assert isinstance(validated, models.UpdateTaskArgs)
                    return await update_task(
                        client, deps.ensure_token, validated.task_list_id,
                        validated.task_id, validated.title, validated.notes,
                        validated.due, validated.status,
                    )
                assert isinstance(validated, models.CompleteTaskArgs)
                return await complete_task(
                    client, deps.ensure_token, validated.task_list_id, validated.task_id
                )
        proposal, args = _write_proposal(action, validated)
        return ToolResult(
            ok=False,
            code="needs_confirmation",
            message=proposal,
            needs_confirmation=True,
            proposal=proposal,
            arguments=args,
        )

    async def _create(validated: BaseModel | dict, context: dict) -> ToolResult | dict:
        assert isinstance(validated, models.CreateTaskArgs)
        return await _needs_confirmation(validated, "create_task", context)

    async def _update(validated: BaseModel | dict, context: dict) -> ToolResult | dict:
        assert isinstance(validated, models.UpdateTaskArgs)
        return await _needs_confirmation(validated, "update_task", context)

    async def _complete(validated: BaseModel | dict, context: dict) -> ToolResult | dict:
        assert isinstance(validated, models.CompleteTaskArgs)
        return await _needs_confirmation(validated, "complete_task", context)

    registry.register(
        "list_task_lists", "List the user's Google task lists.",
        models.BaseModel, _lists,
    )
    registry.register(
        "get_tasks", "Retrieve tasks from a task list.",
        models.ListTasksArgs, _tasks,
    )
    registry.register(
        "get_task_details", "Get details of one task by ID.",
        models.TaskDetailsArgs, _details,
    )
    registry.register(
        "create_task", "Create a task. Requires confirmation.",
        models.CreateTaskArgs, _create, needs_confirmation=True,
    )
    registry.register(
        "update_task", "Update a task. Requires confirmation.",
        models.UpdateTaskArgs, _update, needs_confirmation=True,
    )
    registry.register(
        "complete_task", "Mark a task as completed. Requires confirmation.",
        models.CompleteTaskArgs, _complete, needs_confirmation=True,
    )
