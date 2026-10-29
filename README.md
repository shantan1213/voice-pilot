# Voice-Pilot

Voice-Pilot is a browser-based voice assistant. Speak naturally, see your
transcript appear as you talk, and hear a Muse Spark answer spoken aloud using
a locally running MMS-TTS voice.

## Features

- Live microphone transcription through Meta Muse Voice Transcribe.
- Interim and finalized transcript events over a browser-to-FastAPI WebSocket.
- Muse Spark responses streamed from the backend using server-sent events
  (SSE) and spoken sentence by sentence; assistant text is not displayed as a
  second transcript.
- Complete assistant turns are kept in browser memory and sent as context on
  later turns.
- Completed sentences are synthesized locally with `facebook/mms-tts-eng` and
  played in order. Text streaming does not wait for speech synthesis.
- The main conversation view shows your transcript; the assistant answer is
  spoken rather than shown as a second transcript.
- Lightweight animated SVG face with idle, listening, thinking, and speaking
  states, plus subtle color and expression changes for broad response moods.
  The mouth animation follows audio playback rather than phonemes.
- A stop button and voice-interruption handling; no automatic retry of partial
  responses.
- Optional Pocket TTS remains available from the backend when separately
  installed; the main voice interface uses MMS English.

## Architecture

The browser captures mono 24 kHz PCM and sends small audio frames over WebSocket
to FastAPI. The backend relays them to Meta's realtime transcription API and
forwards interim/final events. Final speech turns are sent to Muse Spark over
Meta's OpenAI-compatible Chat Completions API (`stream=True`). FastAPI forwards
text deltas as SSE. The browser accumulates the complete answer for context and
independently sends sentence-sized text to the local MMS-TTS endpoint. WAV
responses are decoded and played one at a time with the Web Audio API.

```text
Microphone ──24 kHz PCM/WebSocket──> FastAPI ──realtime WebSocket──> Muse Voice Transcribe
     ^                                   │                             │
     └──────── interim/final events ─────┴─────────────────────────────┘
                                          │ final transcript
                                          v
Browser transcript <──SSE text── FastAPI ──stream=True──> Muse Spark
     │
     ├── conversation history (browser memory)
     └── sentence queue ──POST /api/tts──> local MMS-TTS ──WAV──> Web Audio playback
```

For a step-by-step explanation of the actual implementation, see
[PROJECT_EXPLAINED.md](PROJECT_EXPLAINED.md).

## Requirements

- Windows 10 or 11, PowerShell, and Git.
- Python 3.12 (64-bit).
- A current Chrome or Edge browser. The microphone needs a secure context;
  `localhost` and `127.0.0.1` count as secure for local development.
- A Meta Model API key with access to Muse Voice Transcribe and Muse Spark.
- Internet access for the Meta APIs and first-time Hugging Face model download.
- Optional: an NVIDIA GPU and a compatible PyTorch CUDA build. CPU inference
  also works but first-time model loading and speech generation can be slower.
  Leave enough GPU memory for other applications; the MMS model is loaded
  lazily on the first spoken response.

## Install on Windows

Run these commands in **PowerShell**. The first command can run from any
directory; the remaining commands run from the cloned repository root.

```powershell
# Clone the requested project repository.
git clone https://github.com/shantan1213/voice-pilot.git

# Enter the project root.
Set-Location .\voice-pilot

# Create an isolated Python environment.
py -3.12 -m venv .venv

# Activate it for this PowerShell window.
.\.venv\Scripts\Activate.ps1

# Optional: install CUDA-enabled PyTorch first for an NVIDIA GPU.
# Use the CUDA wheel index recommended by the official PyTorch selector
# for your driver; this example is for CUDA 12.8.
python -m pip install torch==2.11.0+cu128 --index-url https://download.pytorch.org/whl/cu128

# Install the remaining pinned project dependencies.
python -m pip install -r requirements.txt
```

If PowerShell says script execution is disabled, use the one-session bypass
below rather than changing the machine-wide execution policy:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

For a CPU-only install, skip the CUDA command. `requirements.txt` installs
PyTorch from the default package index. On a CUDA machine, verify the installed
build with:

```powershell
python -c "import torch; print(torch.__version__); print('CUDA available:', torch.cuda.is_available())"
```

## Configure the Meta API key

From the repository root, create a local `.env` from the placeholder template
only when one does not already exist. Never replace a file containing your
credentials.

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
notepad .env
```

Set the key in `.env`:

```text
MODEL_API_KEY=your_meta_model_api_key
```

Optional model override:

```text
MUSE_CHAT_MODEL=muse-spark-1.3
```

Voice-Pilot reads `.env` when the backend starts. Restart Uvicorn after editing
the file. Keep real credentials only in `.env`; `.gitignore` excludes it. Do
not paste credentials into the browser or commit them. The example file contains
placeholders only.

## Models and first run

The MMS-TTS checkpoint is `facebook/mms-tts-eng`. Voice-Pilot loads it lazily
when the first speech segment is synthesized; Transformers downloads the
checkpoint into the normal Hugging Face cache if it is not already present.
The first answer may take longer while weights download and the model loads.
Later synthesis reuses the loaded model.

Muse Voice Transcribe (`muse-voice-transcribe-1.0`) and Muse Spark
(`muse-spark-1.3` by default) are hosted services, not local downloads. Their
requests use `MODEL_API_KEY` from the backend environment. The main UI uses
English MMS TTS. The optional `pocket-indian` model path requires `pocket-tts`
and its Hugging Face voice checkpoint to be installed/downloaded separately;
it is not needed for the main application.

## Start Voice-Pilot

From the repository root, with `.venv` active:

```powershell
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000). Allow microphone access,
then select the large microphone button. Speak a short question and pause:
the interim transcript should appear first, followed by the finalized turn.
Muse Spark text streams to the browser and is synthesized by MMS one completed
sentence at a time; the assistant answer is spoken, not shown as a transcript.
Press **Stop response** or begin speaking while Voice-Pilot is speaking to
interrupt it. Select the microphone button again to finish the listening
session.

Starting the microphone is a browser gesture that resumes the Web Audio context.
If the browser blocks playback, allow sound for the site and click the
microphone button again. The old standalone TTS form has been removed from the
main screen; the `POST /api/tts` WAV endpoint remains available.

## Verify the services

Run from the repository root:

```powershell
# Backend health.
Invoke-RestMethod http://127.0.0.1:8000/api/health

# Python unit tests, including SSE chunks, malformed/empty events, errors,
# timeout and cancellation.
python -m unittest discover -s tests -v
```

Use the browser for the end-to-end microphone → transcription → Muse Spark →
MMS speech test. The browser needs microphone permission and a working speaker
or headphones. The user transcript remains visible; Voice-Pilot speaks its
response without echoing that response into another transcript panel. To test
MMS WAV generation directly, run this while the app is running; it saves a
uniquely named WAV to the Windows temporary directory:

```powershell
$wav = Join-Path $env:TEMP ("voice-pilot-test-" + [guid]::NewGuid() + ".wav")
$body = @{ text = "Hello, this is Voice-Pilot speaking."; model = "mms" } | ConvertTo-Json
Invoke-WebRequest -Uri http://127.0.0.1:8000/api/tts -Method Post `
  -ContentType "application/json" -Body $body -OutFile $wav
Write-Output "WAV written to $wav"
```

Muse Spark can be tested independently with a streaming HTTP client such as
`curl -N`:

```powershell
$body = '{"messages":[{"role":"user","content":"Reply with one friendly greeting."}]}'
curl.exe -N http://127.0.0.1:8000/api/chat/stream `
  -H "Content-Type: application/json" -d $body
```

The response contains SSE `delta` events and a final `done` event. Use the
browser rather than this command to verify progressive rendering and playback.

## Troubleshooting

### Microphone permission or missing audio

- Use Chrome or Edge on `http://127.0.0.1:8000` and allow microphone access
  when prompted. Check the browser's site-permission indicator if it was denied.
- `navigator.mediaDevices.getUserMedia` is unavailable on insecure non-local
  origins. Use HTTPS when hosting outside localhost.
- If the page says audio is blocked or speech is silent, check the selected
  output device, browser/site sound permission, and system volume. Click the
  microphone control again to resume the audio context.
- Confirm the TTS response is WAV by calling `/api/tts`; the endpoint returns
  `audio/wav` and removes its temporary server-side file after delivery.

### Meta API or STT connection errors

- Confirm `.env` is in the repository root, `MODEL_API_KEY` is non-empty, and
  restart Uvicorn after changing it. Never print the key into a terminal log
  that may be shared.
- Check that the key/account has access to both Muse Voice Transcribe and Muse
  Spark, and that the machine can reach `api.meta.ai`.
- The STT route uses `wss://api.meta.ai/v1/asr/realtime`; an old backend process
  may still be serving an earlier environment. Stop that specific old Uvicorn
  session, restart the command above, and refresh the browser.
- Browser WebSocket errors are intentionally generic; inspect the Uvicorn
  terminal for session failures. The key is sent only by the backend.
- Chat streaming uses `https://api.meta.ai/v1/chat/completions`. A 401/403
  points to key/account access; timeout and network messages indicate
  connectivity or service latency.

### Slow or failed speech generation

- The first response can be delayed by the MMS checkpoint download and model
  initialization. Check internet access and available disk space for the
  Hugging Face cache.
- CPU inference is supported but slower. Verify CUDA with the command above;
  update NVIDIA drivers and install the PyTorch wheel matching your supported
  CUDA version if CUDA is unavailable.
- Close GPU-heavy applications if the 6 GB laptop GPU runs out of memory.
- Muse Spark text keeps streaming while MMS is synthesizing the sentence queue.
  Sentence audio is generated serially to prevent overlap. Interrupting playback
  cancels the browser request, but an already-running synchronous TTS inference
  on the server may finish in the background.

## Limitations and licensing

- Meta STT and Muse Spark require network access, a valid key, and service
  availability. Speech audio and conversation text are sent to those services.
- TTS audio is local after model download; the first download requires
  Hugging Face connectivity.
- Speech synthesis is sentence-by-sentence, not token-by-token audio streaming.
  Small audio fades reduce clicks between clips; the face mood and mouth
  animation are approximate and are not synchronized to phonemes.
- Conversation history is kept in browser memory and is lost on page reload.
  Older turns are trimmed from the context request to keep it bounded.
- Review Meta Model API terms/privacy policies and the Hugging Face model cards
  and licenses for MMS (and Pocket TTS, if enabled) before redistribution or
  commercial use. Model and API licenses/terms can change; this project does
  not grant rights to the models.
