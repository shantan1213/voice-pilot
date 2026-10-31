"""STT router for Meta Voice Transcribe relay."""

import asyncio
import json
import logging
import os
import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from websockets.asyncio.client import connect as websocket_connect
from websockets.exceptions import ConnectionClosed

router = APIRouter()
logger = logging.getLogger(__name__)


@router.websocket("/api/stt")
async def stt(websocket: WebSocket) -> None:
    """Relay 24kHz PCM to Meta Muse Voice Transcribe (delegates to main)."""
    # Delegate to main's implementation to share constants and avoid duplication.
    from app.main import stt as main_stt

    await main_stt(websocket)
