"""TTS router - in-memory synthesis with concurrency limit."""

import asyncio
import io

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from typing import Literal

router = APIRouter()

# Shared semaphore will be set by main lifespan; fallback to None for tests
_tts_semaphore: asyncio.Semaphore | None = None


class TTSRequest(BaseModel):
    text: str = Field(min_length=1, max_length=1000)
    model: Literal["mms"] = "mms"


@router.post("/api/tts")
async def tts(request: TTSRequest) -> StreamingResponse:
    """Synthesize text to WAV bytes in memory (no temp files)."""
    from app.tts import synthesize_to_bytes

    text = request.text.strip()
    if not text:
        raise HTTPException(status_code=422, detail="Text must not be empty.")
    try:
        # Use shared semaphore when available, else direct call
        from app.main import _tts_semaphore as shared_sem

        sem = shared_sem
    except Exception:
        sem = _tts_semaphore
    if sem is None:
        wav_bytes = await asyncio.to_thread(synthesize_to_bytes, text, request.model)
    else:
        async with sem:
            wav_bytes = await asyncio.to_thread(synthesize_to_bytes, text, request.model)
    return StreamingResponse(
        io.BytesIO(wav_bytes),
        media_type="audio/wav",
        headers={"Content-Disposition": 'attachment; filename="voice-pilot.wav"'},
    )
