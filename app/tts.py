"""Local English text-to-speech using Meta MMS-TTS."""

from __future__ import annotations

import os
import io
import re
import tempfile
import threading
from pathlib import Path
from typing import Literal

MODEL_ID = "facebook/mms-tts-eng"
MAX_TEXT_LENGTH = 1000
SPEAKING_RATE = 0.98
NOISE_SCALE = 0.667
NOISE_SCALE_DURATION = 0.8
SENTENCE_PAUSE_SECONDS = 0.15

_model = None
_tokenizer = None
_device = None
_model_lock = threading.RLock()


def warm_mms_model() -> None:
    """Load MMS once during startup so the first spoken response is not cold."""
    _load_model()


def is_model_ready() -> bool:
    return _model is not None and _tokenizer is not None


def synthesize_to_bytes(text: str, model_name: Literal["mms"] = "mms") -> bytes:
    """Synthesize text to WAV bytes in memory without touching the filesystem."""
    if not isinstance(text, str):
        raise TypeError("text must be a string.")
    text = text.strip()
    if not text:
        raise ValueError("text must not be empty.")
    if len(text) > MAX_TEXT_LENGTH:
        raise ValueError(f"text must be at most {MAX_TEXT_LENGTH} characters.")

    with _model_lock:
        import torch
        from transformers import AutoTokenizer

        tokenizer, model, device = _load_model()
        sampling_rate = model.config.sampling_rate
        if not isinstance(sampling_rate, int) or sampling_rate <= 0:
            raise RuntimeError("The MMS model reported an invalid sampling rate.")

        chunks = [
            sentence.strip()
            for sentence in re.split(r"(?<=[.!?])\s+|\n+", text)
            if sentence.strip()
        ]
        audio_parts = []
        for sentence in chunks:
            inputs = tokenizer(sentence, return_tensors="pt").to(device)
            with torch.inference_mode():
                outputs = model(**inputs)
            waveform = outputs.waveform
            if (
                waveform is None
                or waveform.ndim != 2
                or waveform.shape[0] != 1
                or waveform.shape[1] == 0
            ):
                raise RuntimeError("The MMS model returned an invalid waveform.")
            audio_parts.append(waveform[0].detach().cpu().numpy())

        if not audio_parts:
            raise RuntimeError("No speech segments were generated.")

        import numpy as np
        import soundfile as sf

        silence = np.zeros(
            round(sampling_rate * SENTENCE_PAUSE_SECONDS),
            dtype=audio_parts[0].dtype,
        )
        audio_segments: list[np.ndarray] = []
        for index, part in enumerate(audio_parts):
            if index:
                audio_segments.append(silence)
            audio_segments.append(part)
        audio = np.concatenate(audio_segments)

        buffer = io.BytesIO()
        sf.write(
            buffer,
            audio,
            sampling_rate,
            format="WAV",
            subtype="PCM_16",
        )
        return buffer.getvalue()


def _load_model():
    """Load the tokenizer and VITS model lazily on the first synthesis request."""
    global _model, _tokenizer, _device

    if _model is not None and _tokenizer is not None:
        return _tokenizer, _model, _device

    with _model_lock:
        if _model is not None and _tokenizer is not None:
            return _tokenizer, _model, _device

        import torch
        from transformers import AutoTokenizer, VitsModel

        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

        try:
            tokenizer = AutoTokenizer.from_pretrained(MODEL_ID)
            model = VitsModel.from_pretrained(MODEL_ID)
        except Exception as exc:
            raise RuntimeError(
                f"Failed to load '{MODEL_ID}'. Check model cache and Hugging Face connectivity."
            ) from exc

        try:
            model.to(device)
        except RuntimeError as exc:
            raise RuntimeError(f"Failed to move model to {device}.") from exc

        model.speaking_rate = SPEAKING_RATE
        model.noise_scale = NOISE_SCALE
        model.noise_scale_duration = NOISE_SCALE_DURATION
        model.eval()
        _tokenizer = tokenizer
        _model = model
        _device = device
        return _tokenizer, _model, _device



def synthesize_to_wav(
    text: str,
    output_path: str | Path,
    model_name: Literal["mms"] = "mms",
) -> Path:
    """Generate a WAV with the selected local voice and sentence pauses."""
    if not isinstance(text, str):
        raise TypeError("text must be a string.")

    text = text.strip()
    if not text:
        raise ValueError("text must not be empty.")
    if len(text) > MAX_TEXT_LENGTH:
        raise ValueError(f"text must be at most {MAX_TEXT_LENGTH} characters.")

    if isinstance(output_path, str):
        target = Path(output_path).expanduser()
    else:
        target = output_path.expanduser()

    if target.suffix.lower() != ".wav":
        raise ValueError("output_path must end with '.wav'.")

    target = target.resolve(strict=False)
    if not target.parent.exists():
        raise FileNotFoundError(f"Output directory does not exist: {target.parent}")
    if target.exists() or target.is_symlink():
        raise FileExistsError(f"Refusing to overwrite existing file: {target}")

    wav_bytes = synthesize_to_bytes(text, model_name=model_name)
    target.write_bytes(wav_bytes)

    return target
