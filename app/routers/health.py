"""Health router - extracted for modular FastAPI routing."""

from fastapi import APIRouter

router = APIRouter()


@router.get("/api/health")
def health() -> dict:
    """Liveness check with model readiness for UI warm-up state."""
    try:
        from app.tts import is_model_ready

        model_ready = is_model_ready()
    except Exception:
        model_ready = False
    return {"status": "ok", "phase": 1, "app": "Voice-Pilot", "model_ready": model_ready}
