"""Chat router for Muse Spark streaming with shared client."""

import os

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from typing import Literal

router = APIRouter()


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=8_000)


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(min_length=1, max_length=40)


@router.post("/api/chat/stream")
async def chat_stream(request: ChatRequest) -> StreamingResponse:
    """Stream Muse Spark response as SSE (reuses shared httpx client)."""
    from app.main import CHAT_MAX_TOTAL_CHARACTERS, CHAT_MODEL, _stream_muse_chat
    from app.settings import settings as _settings

    api_key = os.getenv("MODEL_API_KEY", "").strip() or _settings.model_api_key.strip()
    if not api_key:
        raise HTTPException(status_code=503, detail="Muse Spark is not configured on the server.")
    if not CHAT_MODEL:
        raise HTTPException(status_code=503, detail="Muse Spark model is not configured on the server.")
    total = sum(len(m.content) for m in request.messages)
    if total > CHAT_MAX_TOTAL_CHARACTERS:
        raise HTTPException(
            status_code=413,
            detail=f"Conversation context cannot exceed {CHAT_MAX_TOTAL_CHARACTERS} characters.",
        )
    messages = [
        {
            "role": "developer",
            "content": (
                "You are Voice-Pilot, a friendly voice assistant. Answer in clear, "
                "natural spoken language using short conversational paragraphs. "
                "Do not use Markdown, bullet points, numbered lists, or decorative "
                "symbols; say list items as connected sentences. Keep the answer "
                "concise unless the user asks for detail."
            ),
        },
        *[m.model_dump() for m in request.messages],
    ]
    return StreamingResponse(
        _stream_muse_chat(messages, api_key),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
