"""Calendar tool registration: the strict five-action surface for Raala.

Read actions run immediately. `create_calendar_event` requires an explicit,
exact, unexpired confirmation (see `app.agent.confirmation`), otherwise this
module only proposes and never calls the Google API.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import httpx
from pydantic import BaseModel

from app.agent import action_models as models
from app.agent.confirmation import matching_details
from app.agent.tool_executor import ToolResult
from app.agent.tool_registry import ToolRegistry
from app.integrations.google import calendar as calendar_api

logger = logging.getLogger(__name__)


def _client_factory() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=30, write=20, pool=10))


def _format_dt(value: datetime) -> str:
    return value.strftime("%A, %Y-%m-%d %H:%M %Z").strip()


def _proposal_text(args: models.CreateEventArgs) -> str:
    return (
        f"Please confirm: '{args.summary}' on {_format_dt(args.start)} "
        f"to {_format_dt(args.end)}. Reply with 'Confirm' to schedule it. "
        f"This request expires in 5 minutes."
    )


class GoogleDeps:
    """Injectable dependencies (settings values + factories) for tests."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        client_factory=_client_factory,
        token_path=None,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.client_factory = client_factory
        self.token_path = token_path
        self._access_token: str = ""
        self._access_expiry: float = 0.0

    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret and self.redirect_uri)

    async def ensure_token(self, force_refresh: bool = False) -> str:
        import time

        from app.integrations.google import oauth, token_store

        if not self.configured():
            raise PermissionError(
                "Google is not configured on the server (missing client ID/secret)."
            )
        stored = token_store.load_tokens(self.token_path)
        if stored is None:
            raise PermissionError(
                "Your Google account is not connected. "
                "Open /auth/google/login in your browser to connect it."
            )
        import time as _time

        if not force_refresh and self._access_token and _time.time() < self._access_expiry - 60:
            return self._access_token
        async with self.client_factory() as client:
            try:
                refreshed = await oauth.refresh_access_token(
                    client, self.client_id, self.client_secret, stored["refresh_token"]
                )
            except RuntimeError as exc:
                message = str(exc)
                if "invalid_grant" in message or "deleted" in message:
                    token_store.clear_tokens(self.token_path)
                    raise PermissionError(
                        "Your Google connection expired or was revoked. "
                        "Open /auth/google/login in your browser to reconnect."
                    ) from exc
                raise
        self._access_token = refreshed["access_token"]
        self._access_expiry = time.time() + int(refreshed.get("expires_in", 3600))
        return self._access_token


def register_calendar_tools(registry: ToolRegistry, deps: GoogleDeps) -> None:
    async def _today(validated: BaseModel | dict, context: dict) -> dict:
        from app.agent.timezone import local_tz

        async with deps.client_factory() as client:
            return await calendar_api.get_today_events(
                client, deps.ensure_token, datetime.now(local_tz())
            )

    async def _upcoming(validated: BaseModel | dict, context: dict) -> dict:
        assert isinstance(validated, models.UpcomingArgs)
        from app.agent.timezone import local_tz

        async with deps.client_factory() as client:
            return await calendar_api.get_upcoming_events(
                client, deps.ensure_token, datetime.now(local_tz()),
                validated.days, validated.limit,
            )

    async def _availability(validated: BaseModel | dict, context: dict) -> dict:
        assert isinstance(validated, models.AvailabilityArgs)
        async with deps.client_factory() as client:
            return await calendar_api.check_availability(
                client, deps.ensure_token, validated.start, validated.end
            )

    async def _details(validated: BaseModel | dict, context: dict) -> dict:
        assert isinstance(validated, models.EventDetailsArgs)
        async with deps.client_factory() as client:
            return await calendar_api.get_event_details(
                client, deps.ensure_token, validated.event_id
            )

    async def _create(validated: BaseModel | dict, context: dict) -> ToolResult | dict:
        assert isinstance(validated, models.CreateEventArgs)
        if not context.get("confirmed"):
            args_dict = {
                "summary": validated.summary,
                "start": validated.start.isoformat(),
                "end": validated.end.isoformat(),
                "description": validated.description,
                "time_zone": validated.time_zone,
            }
            proposal = _proposal_text(validated)
            return ToolResult(
                ok=False,
                code="needs_confirmation",
                message=proposal,
                needs_confirmation=True,
                proposal=proposal,
                arguments=args_dict,
            )
        async with deps.client_factory() as client:
            created = await calendar_api.create_event(
                client,
                deps.ensure_token,
                validated.summary,
                validated.start,
                validated.end,
                validated.description,
                validated.time_zone,
            )
        return created

    registry.register(
        "get_today_events",
        "List today's Google Calendar events.",
        BaseModel,
        _today,
    )
    registry.register(
        "get_upcoming_events",
        "List upcoming Google Calendar events within a bounded number of days.",
        models.UpcomingArgs,
        _upcoming,
    )
    registry.register(
        "check_calendar_availability",
        "Check whether a time range is free of calendar events.",
        models.AvailabilityArgs,
        _availability,
    )
    registry.register(
        "get_event_details",
        "Get details of one calendar event by ID.",
        models.EventDetailsArgs,
        _details,
    )
    registry.register(
        "create_calendar_event",
        "Create a calendar event. Requires explicit user confirmation first.",
        models.CreateEventArgs,
        _create,
        needs_confirmation=True,
    )


def coerce_create_args(raw: dict[str, Any]) -> models.CreateEventArgs:
    """Re-validate stored pending arguments at confirmation time."""
    return models.CreateEventArgs.model_validate(raw)
