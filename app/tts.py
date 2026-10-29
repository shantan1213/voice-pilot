"""Local English text-to-speech using Meta MMS-TTS."""

from __future__ import annotations

import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Literal

MODEL_ID = "facebook/mms-tts-eng"
POCKET_MODEL_ID = "prasadvittaldev/pocket-tts-indian-english-male-indictts"
POCKET_MODEL_CONFIG = f"hf://{POCKET_MODEL_ID}/config.yaml"
POCKET_SAMPLE_RATE = 24_000
MAX_TEXT_LENGTH = 1000
SPEAKING_RATE = 0.98
NOISE_SCALE = 0.667
NOISE_SCALE_DURATION = 0.8
SENTENCE_PAUSE_SECONDS = 0.4

_model = None
_tokenizer = None
_device = None
_pocket_model = None
_pocket_voice_state = None
_model_lock = threading.RLock()


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


def _load_pocket_model():
    """Load the optional Indian-English Pocket TTS model and voice lazily."""
    global _pocket_model, _pocket_voice_state

    if _pocket_model is not None and _pocket_voice_state is not None:
        return _pocket_model, _pocket_voice_state

    with _model_lock:
        if _pocket_model is not None and _pocket_voice_state is not None:
            return _pocket_model, _pocket_voice_state

        try:
            from huggingface_hub import hf_hub_download
            from pocket_tts import TTSModel

            model = TTSModel.load_model(config=POCKET_MODEL_CONFIG, quantize=True)
            model.to("cpu")
            voice_path = hf_hub_download(
                repo_id=POCKET_MODEL_ID,
                filename="voice_prompt.wav",
            )
            voice_state = model.get_state_for_audio_prompt(voice_path)
        except Exception as exc:
            raise RuntimeError(
                "Could not load the optional Indian-English Pocket TTS model. "
                "Install pocket-tts and check Hugging Face connectivity."
            ) from exc

        _pocket_model = model
        _pocket_voice_state = voice_state
        return _pocket_model, _pocket_voice_state


def _pocket_chunks(text: str) -> list[str]:
    """Split Pocket TTS input into sentence/comma chunks under its token limit."""
    chunks: list[str] = []
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", text):
        sentence = sentence.strip()
        if not sentence:
            continue
        if len(sentence) <= 150:
            chunks.append(sentence)
            continue

        parts = re.split(r"(?<=,)\s+|\s+", sentence)
        current = ""
        for part in parts:
            if len(current) + len(part) + bool(current) > 150 and current:
                chunks.append(current)
                current = part
            else:
                current = f"{current} {part}".strip()
        if current:
            chunks.append(current)
    return chunks


def synthesize_to_wav(
    text: str,
    output_path: str | Path,
    model_name: Literal["mms", "pocket-indian"] = "mms",
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

    import soundfile as sf
    import numpy as np

    with _model_lock:
        if model_name == "mms":
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
        elif model_name == "pocket-indian":
            pocket_model, voice_state = _load_pocket_model()
            sampling_rate = POCKET_SAMPLE_RATE
            audio_parts = []
            for chunk in _pocket_chunks(text):
                generated_chunks = pocket_model.generate_audio_stream(
                    model_state=voice_state,
                    text_to_generate=chunk,
                )
                chunk_audio = []
                for generated in generated_chunks:
                    if hasattr(generated, "detach"):
                        generated = generated.detach().cpu().numpy()
                    chunk_audio.append(np.asarray(generated).reshape(-1))
                if not chunk_audio:
                    raise RuntimeError("Pocket TTS returned no audio for a text chunk.")
                audio_parts.append(np.concatenate(chunk_audio))
        else:
            raise ValueError("model_name must be 'mms' or 'pocket-indian'.")

        if not audio_parts:
            raise RuntimeError("No speech segments were generated.")

        silence = np.zeros(
            round(sampling_rate * SENTENCE_PAUSE_SECONDS),
            dtype=audio_parts[0].dtype,
        )
        audio_segments = []
        for index, part in enumerate(audio_parts):
            if index:
                audio_segments.append(silence)
            audio_segments.append(part)
        audio = np.concatenate(audio_segments)

        fd, temp_name = tempfile.mkstemp(
            prefix=".voice-pilot-tts-",
            suffix=".wav",
            dir=str(target.parent),
        )
        temp_path = Path(temp_name)

        try:
            os.close(fd)
            sf.write(
                str(temp_path),
                audio,
                sampling_rate,
                format="WAV",
                subtype="PCM_16",
            )

            try:
                os.link(temp_path, target)
            except FileExistsError as exc:
                raise FileExistsError(f"Refusing to overwrite existing file: {target}") from exc
            except OSError as exc:
                raise OSError(f"Could not finalize WAV file at {target}.") from exc
        finally:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass

    return target
