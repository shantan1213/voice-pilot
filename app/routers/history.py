"""History router - snapshot read to avoid blocking voice path."""

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field
from uuid import UUID

router = APIRouter()


class ConversationTurnRequest(BaseModel):
    session_id: UUID
    turn_id: UUID
    user: str = Field(min_length=1, max_length=8_000)
    assistant: str = Field(min_length=1, max_length=8_000)


@router.get("/api/history")
async def get_conversation_history() -> dict[str, list[dict[str, str]]]:
    """Return persisted turns via snapshot (no queue.join)."""
    from app.main import conversation_logs

    return {"turns": await conversation_logs.list_turns_snapshot()}


@router.post("/api/history/turn", status_code=202)
async def log_conversation_turn(request: ConversationTurnRequest) -> dict[str, str]:
    from app.main import conversation_logs

    user = request.user.strip()
    assistant = request.assistant.strip()
    if not user or not assistant:
        raise HTTPException(status_code=422, detail="A conversation turn cannot be empty.")
    result = conversation_logs.enqueue(request.session_id, request.turn_id, user, assistant)
    if result == "full":
        raise HTTPException(status_code=503, detail="Conversation history is temporarily busy.")
    return {"status": result}


@router.delete("/api/history", status_code=204)
async def clear_conversation_history() -> Response:
    import logging

    from app.main import conversation_logs

    logger = logging.getLogger(__name__)
    try:
        await conversation_logs.clear()
    except OSError as exc:
        logger.warning("Could not clear conversation history (%s).", type(exc).__name__)
        raise HTTPException(status_code=500, detail="Conversation history could not be cleared.") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="Conversation history is unavailable.") from exc
    return Response(status_code=204)
