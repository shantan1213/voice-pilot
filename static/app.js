const microphoneButton = document.getElementById("microphone-button");
const stopResponseButton = document.getElementById("stop-response");
const avatar = document.getElementById("avatar");
const agentStatus = document.getElementById("agent-status");
const connectionDot = document.getElementById("connection-dot");
const connectionLabel = document.getElementById("connection-label");
const audioDot = document.getElementById("audio-dot");
const audioLabel = document.getElementById("audio-label");
const enableAudioButton = document.getElementById("enable-audio");
const errorMessage = document.getElementById("error-message");
const transcript = document.getElementById("transcript");
const transcriptState = document.getElementById("transcript-state");
const response = document.getElementById("response");
const responseState = document.getElementById("response-state");

const STT_SAMPLE_RATE = 24000;
const STT_SESSION_LIMIT_MS = 10 * 60 * 1000;
const MAX_QUEUED_TURNS = 8;
let sttSocket = null;
let microphoneStream = null;
let playbackContext = null;
let captureContext = null;
let audioProcessor = null;
let silenceGain = null;
let sessionLimitTimer = null;
let microphoneActive = false;
let sttHadError = false;
let finalTranscript = "";
const chatHistory = [];
let responseQueue = [];
let responseQueueRunning = false;
let activeResponseController = null;
let speechQueue = Promise.resolve();
let speechRunId = 0;
let activeAudioSource = null;
let activeTtsController = null;
let responseInProgress = false;
let audioEnabled = false;

function setAgentState(state, message) {
  avatar.dataset.state = state;
  avatar.setAttribute("aria-label", `Voice-Pilot is ${state}`);
  agentStatus.dataset.state = state;
  agentStatus.textContent = message;
  connectionDot.dataset.state = state === "error" ? "error" : microphoneActive ? "connected" : "idle";
  connectionLabel.textContent = microphoneActive
    ? state === "error" ? "Connection issue" : "Listening connection"
    : "Ready";
}

function showError(message) {
  errorMessage.textContent = message;
  errorMessage.hidden = false;
}

function clearError() {
  errorMessage.textContent = "";
  errorMessage.hidden = true;
}

function stopCurrentAudio() {
  speechRunId += 1;
  if (activeTtsController) {
    activeTtsController.abort();
    activeTtsController = null;
  }
  if (activeAudioSource) {
    const source = activeAudioSource;
    activeAudioSource = null;
    try {
      source.stop();
    } catch (error) {
      if (!(error instanceof DOMException && error.name === "InvalidStateError")) {
        throw error;
      }
    }
  }
  speechQueue = Promise.resolve();
  audioDot.dataset.state = audioEnabled ? "ready" : "off";
}

function interruptResponse(status = "Response interrupted by your voice.") {
  if (activeResponseController) {
    activeResponseController.abort();
    activeResponseController = null;
  }
  responseQueue = [];
  stopCurrentAudio();
  responseInProgress = false;
  stopResponseButton.disabled = true;
  responseState.textContent = "INTERRUPTED";
  setAgentState(microphoneActive ? "listening" : "idle", status);
}

async function ensureAudioContext() {
  const AudioContextClass = window.AudioContext || window.webkitAudioContext;
  if (!AudioContextClass) {
    throw new Error("This browser does not support audio playback.");
  }
  if (!playbackContext || playbackContext.state === "closed") {
    playbackContext = new AudioContextClass();
  }
  if (playbackContext.state !== "running") {
    await playbackContext.resume();
  }
  audioEnabled = playbackContext.state === "running";
  audioDot.dataset.state = audioEnabled ? "ready" : "error";
  audioLabel.textContent = audioEnabled
    ? "Voice replies on · MMS English"
    : "Tap the microphone to enable voice replies";
  enableAudioButton.hidden = audioEnabled;
  if (!audioEnabled) {
    throw new Error("Audio is blocked by the browser. Allow audio and tap the microphone again.");
  }
}

function stopMicrophone() {
  if (audioProcessor) {
    audioProcessor.disconnect();
    audioProcessor.onaudioprocess = null;
    audioProcessor = null;
  }
  if (silenceGain) {
    silenceGain.disconnect();
    silenceGain = null;
  }
  if (microphoneStream) {
    microphoneStream.getTracks().forEach((track) => track.stop());
    microphoneStream = null;
  }
  if (captureContext && captureContext.state !== "closed") {
    void captureContext.close();
    captureContext = null;
  }
}

function closeSttSocket() {
  if (sttSocket && sttSocket.readyState < WebSocket.CLOSING) {
    sttSocket.close();
  }
}

async function playAudioBuffer(buffer, runId) {
  if (!playbackContext || runId !== speechRunId) {
    return Promise.resolve();
  }
  if (playbackContext.state !== "running") {
    await playbackContext.resume();
  }
  if (playbackContext.state !== "running") {
    throw new Error("Browser audio is paused. Select Enable voice to resume playback.");
  }
  const source = playbackContext.createBufferSource();
  source.buffer = buffer;
  source.connect(playbackContext.destination);
  activeAudioSource = source;
  avatar.dataset.state = "speaking";
  agentStatus.textContent = "Speaking";
  responseState.textContent = "SPEAKING";
  audioDot.dataset.state = "speaking";

  return new Promise((resolve, reject) => {
    source.onended = () => {
      if (activeAudioSource === source) {
        activeAudioSource = null;
      }
      if (runId === speechRunId && responseInProgress) {
        setAgentState("thinking", "Thinking");
        responseState.textContent = "THINKING";
        audioDot.dataset.state = "ready";
      }
      resolve();
    };
    try {
      source.start();
    } catch (error) {
      activeAudioSource = null;
      reject(error);
    }
  });
}

function queueSpeech(sentence, runId) {
  const text = sentence.trim();
  if (!text || text.length > 1000) {
    return;
  }
  speechQueue = speechQueue
    .catch(() => {})
    .then(async () => {
      if (!audioEnabled || runId !== speechRunId) {
        return;
      }
      const controller = new AbortController();
      activeTtsController = controller;
      audioDot.dataset.state = "loading";
      agentStatus.textContent = "Preparing voice";
      responseState.textContent = "PREPARING AUDIO";

      try {
        const ttsResponse = await fetch("/api/tts", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ text, model: "mms" }),
          signal: controller.signal,
        });
        if (!ttsResponse.ok) {
          let message = `Speech synthesis failed (HTTP ${ttsResponse.status}).`;
          try {
            const body = await ttsResponse.json();
            if (typeof body.detail === "string") {
              message = body.detail;
            }
          } catch {
            // Keep the generic HTTP message when the server does not return JSON.
          }
          throw new Error(message);
        }
        const contentType = ttsResponse.headers.get("content-type") || "";
        if (!contentType.toLowerCase().includes("audio/wav")) {
          throw new Error("Speech synthesis returned an unsupported audio format.");
        }
        const audioData = await ttsResponse.arrayBuffer();
        if (audioData.byteLength < 44 || runId !== speechRunId) {
          if (runId === speechRunId) {
            throw new Error("Speech synthesis returned an empty WAV file.");
          }
          return;
        }
        const audioBuffer = await playbackContext.decodeAudioData(audioData);
        if (runId === speechRunId) {
          await playAudioBuffer(audioBuffer, runId);
        }
      } catch (error) {
        if (controller.signal.aborted) {
          return;
        }
        audioDot.dataset.state = "error";
        audioEnabled = false;
        enableAudioButton.hidden = false;
        audioLabel.textContent = "Voice playback error · text is still available";
        showError(error instanceof Error ? error.message : "Could not play the spoken response.");
        setAgentState("error", "Response text is available");
      } finally {
        if (activeTtsController === controller) {
          activeTtsController = null;
        }
      }
    });
  return speechQueue;
}

function extractSentences(text) {
  const sentences = [];
  let remainder = text;
  const boundary = /[.!?]["'’”)\]]*(?:\s+|$)/;
  let match = boundary.exec(remainder);
  while (match) {
    const end = match.index + match[0].length;
    sentences.push(remainder.slice(0, end).trim());
    remainder = remainder.slice(end);
    match = boundary.exec(remainder);
  }
  while (remainder.length > 900) {
    const splitAt = remainder.lastIndexOf(" ", 900);
    if (splitAt < 1) {
      break;
    }
    sentences.push(remainder.slice(0, splitAt).trim());
    remainder = remainder.slice(splitAt + 1);
  }
  return { sentences, remainder };
}

async function readMuseStream(streamResponse, runId) {
  if (!streamResponse.body) {
    throw new Error("This browser cannot read the streaming response.");
  }
  const reader = streamResponse.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let completed = false;
  let generatedText = "";
  let speechRemainder = "";

  function applyFrame(frame) {
    const data = frame
      .split("\n")
      .filter((line) => line.startsWith("data:"))
      .map((line) => line.slice(5).trimStart())
      .join("\n");
    if (!data) {
      return;
    }
    let event;
    try {
      event = JSON.parse(data);
    } catch {
      throw new Error("Received an invalid Muse Spark event.");
    }
    if (event.type === "error") {
      throw new Error(event.message || "Muse Spark could not complete the response.");
    }
    if (event.type === "delta" && typeof event.text === "string" && event.text) {
      generatedText += event.text;
      response.textContent = generatedText;
      speechRemainder += event.text;
      const { sentences, remainder } = extractSentences(speechRemainder);
      speechRemainder = remainder;
      sentences.forEach((sentence) => {
        void queueSpeech(sentence, runId);
      });
    } else if (event.type === "done") {
      completed = true;
    }
  }

  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) {
        break;
      }
      buffer = (buffer + decoder.decode(value, { stream: true })).replace(/\r\n/g, "\n");
      let frameEnd = buffer.indexOf("\n\n");
      while (frameEnd !== -1) {
        applyFrame(buffer.slice(0, frameEnd));
        buffer = buffer.slice(frameEnd + 2);
        frameEnd = buffer.indexOf("\n\n");
      }
    }
    buffer = (buffer + decoder.decode()).replace(/\r\n/g, "\n");
    if (buffer.trim()) {
      applyFrame(buffer);
    }
  } finally {
    reader.releaseLock();
  }

  if (!completed) {
    throw new Error("The response stream ended before Muse Spark finished.");
  }
  if (!generatedText.trim()) {
    throw new Error("Muse Spark returned an empty response.");
  }
  if (speechRemainder.trim()) {
    await queueSpeech(speechRemainder, runId);
  }
  return generatedText;
}

function enqueueTurn(text) {
  const question = typeof text === "string" ? text.trim() : "";
  if (!question) {
    return;
  }
  if (responseQueue.length >= MAX_QUEUED_TURNS) {
    showError("There are too many pending turns. Please wait for Voice-Pilot to catch up.");
    return;
  }
  clearError();
  transcript.textContent = question;
  transcriptState.textContent = "FINALIZED";
  responseQueue.push(question);
  void processResponseQueue();
}

async function processResponseQueue() {
  if (responseQueueRunning) {
    return;
  }
  responseQueueRunning = true;
  try {
    while (responseQueue.length) {
      const question = responseQueue.shift();
      const controller = new AbortController();
      const runId = speechRunId;
      activeResponseController = controller;
      responseInProgress = true;
      stopResponseButton.disabled = false;
      response.textContent = "";
      responseState.textContent = "THINKING";
      setAgentState("thinking", "Thinking");

      try {
        const responseMessages = [];
        let contextCharacters = question.length;
        for (let index = chatHistory.length - 2; index >= 0; index -= 2) {
          const pair = chatHistory.slice(index, index + 2);
          const pairCharacters = pair.reduce((total, message) => total + message.content.length, 0);
          if (responseMessages.length + pair.length >= 38
            || contextCharacters + pairCharacters > 20000) {
            break;
          }
          responseMessages.unshift(...pair);
          contextCharacters += pairCharacters;
        }
        responseMessages.push({ role: "user", content: question });
        const result = await fetch("/api/chat/stream", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ messages: responseMessages }),
          signal: controller.signal,
        });
        if (!result.ok) {
          let message = `Muse Spark request failed (HTTP ${result.status}).`;
          try {
            const body = await result.json();
            if (typeof body.detail === "string") {
              message = body.detail;
            }
          } catch {
            // Keep the generic HTTP message when the server does not return JSON.
          }
          throw new Error(message);
        }
        const answer = await readMuseStream(result, runId);
        chatHistory.push(
          { role: "user", content: question },
          { role: "assistant", content: answer },
        );
        await speechQueue;
        if (controller.signal.aborted) {
          responseState.textContent = "INTERRUPTED";
        } else {
          responseState.textContent = "COMPLETE";
          setAgentState(microphoneActive ? "listening" : "idle", "Ready for your next question");
        }
      } catch (error) {
        if (controller.signal.aborted) {
          responseState.textContent = response.textContent ? "INTERRUPTED" : "STOPPED";
        } else {
          responseState.textContent = "ERROR";
          showError(error instanceof Error ? error.message : "Voice-Pilot could not complete the response.");
          setAgentState("error", "Something went wrong");
        }
      } finally {
        if (activeResponseController === controller) {
          activeResponseController = null;
        }
        responseInProgress = false;
        stopResponseButton.disabled = true;
      }
    }
  } finally {
    responseQueueRunning = false;
  }
}

stopResponseButton.addEventListener("click", () => {
  responseQueue = [];
  interruptResponse();
});

enableAudioButton.addEventListener("click", async () => {
  try {
    await ensureAudioContext();
    clearError();
    if (!responseInProgress) {
      setAgentState(microphoneActive ? "listening" : "idle", "Voice replies are ready");
    }
  } catch (error) {
    showError(error instanceof Error ? error.message : "Audio could not be enabled.");
  }
});

async function startMicrophone() {
  clearError();
  if (!navigator.mediaDevices?.getUserMedia || !window.WebSocket) {
    throw new Error("This browser does not support microphone streaming. Try a current Chrome or Edge browser.");
  }

  try {
    await ensureAudioContext();
  } catch (error) {
    audioEnabled = false;
    audioDot.dataset.state = "error";
    audioLabel.textContent = "Tap the microphone to enable voice replies";
    showError(error instanceof Error ? error.message : "Audio could not be enabled.");
  }

  microphoneButton.disabled = true;
  microphoneButton.dataset.busy = "true";
  setAgentState("connecting", "Connecting to speech recognition");
  transcript.textContent = "Waiting for speech…";
  transcriptState.textContent = "LIVE TRANSCRIPT";
  finalTranscript = "";
  sttHadError = false;

  const websocketProtocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  sttSocket = new WebSocket(`${websocketProtocol}//${window.location.host}/api/stt`);
  sttSocket.binaryType = "arraybuffer";

  sttSocket.onopen = () => {
    sttSocket.send(JSON.stringify({ type: "start" }));
  };
  sttSocket.onmessage = (message) => {
    try {
      handleSttEvent(JSON.parse(message.data));
    } catch (error) {
      sttHadError = true;
      showError(error instanceof Error ? error.message : "Received an invalid transcription response.");
      setAgentState("error", "Transcription error");
    }
  };
  sttSocket.onerror = () => {
    if (!sttHadError) {
      sttHadError = true;
      showError("Speech connection failed. Check the backend, API key, and network connection.");
      setAgentState("error", "Speech connection error");
    }
  };
  sttSocket.onclose = async () => {
    window.clearTimeout(sessionLimitTimer);
    microphoneActive = false;
    stopMicrophone();
    microphoneButton.disabled = false;
    microphoneButton.dataset.busy = "false";
    microphoneButton.classList.remove("active");
    microphoneButton.setAttribute("aria-pressed", "false");
    microphoneButton.setAttribute("aria-label", "Start listening");
    microphoneButton.title = "Start listening";
    if (sttHadError && !errorMessage.hidden) {
      setAgentState("error", "Speech connection error");
    } else if (!responseInProgress) {
      setAgentState("idle", "Ready when you are");
    }
    sttSocket = null;
  };

  microphoneStream = await navigator.mediaDevices.getUserMedia({
    audio: {
      channelCount: 1,
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true,
    },
  });
  if (!sttSocket || sttSocket.readyState !== WebSocket.OPEN) {
    stopMicrophone();
    throw new Error("The speech connection closed before the microphone was ready. Please try again.");
  }

  if (!captureContext || captureContext.state === "closed") {
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    captureContext = new AudioContextClass({ sampleRate: STT_SAMPLE_RATE });
  }
  if (captureContext.sampleRate !== STT_SAMPLE_RATE) {
    throw new Error("This browser cannot provide the required 24 kHz microphone audio.");
  }

  const source = captureContext.createMediaStreamSource(microphoneStream);
  audioProcessor = captureContext.createScriptProcessor(2048, 1, 1);
  silenceGain = captureContext.createGain();
  silenceGain.gain.value = 0;
  audioProcessor.onaudioprocess = (event) => {
    if (!sttSocket || sttSocket.readyState !== WebSocket.OPEN) {
      return;
    }
    if (sttSocket.bufferedAmount > 512 * 1024) {
      sttHadError = true;
      showError("Audio connection is too slow. Stop and reconnect.");
      setAgentState("error", "Audio connection is slow");
      stopMicrophone();
      closeSttSocket();
      return;
    }

    const samples = event.inputBuffer.getChannelData(0);
    const buffer = new ArrayBuffer(samples.length * 2);
    const view = new DataView(buffer);
    for (let index = 0; index < samples.length; index += 1) {
      const sample = Math.max(-1, Math.min(1, samples[index]));
      view.setInt16(index * 2, sample < 0 ? sample * 0x8000 : sample * 0x7fff, true);
    }
    sttSocket.send(buffer);
  };
  source.connect(audioProcessor);
  audioProcessor.connect(silenceGain);
  silenceGain.connect(captureContext.destination);
  microphoneActive = true;
  microphoneButton.disabled = false;
  microphoneButton.dataset.busy = "false";
  microphoneButton.classList.add("active");
  microphoneButton.setAttribute("aria-pressed", "true");
  microphoneButton.setAttribute("aria-label", "Stop listening");
  microphoneButton.title = "Stop listening";
  setAgentState("listening", "Listening");

  sessionLimitTimer = window.setTimeout(() => {
    showError("The 10-minute listening limit was reached.");
    closeSttSocket();
  }, STT_SESSION_LIMIT_MS);
}

function handleSttEvent(event) {
  if (!event || typeof event !== "object") {
    return;
  }
  switch (event.type) {
    case "ready":
      microphoneButton.disabled = false;
      microphoneButton.dataset.busy = "false";
      setAgentState("listening", "Listening");
      break;
    case "speechStart":
      if (responseInProgress && avatar.dataset.state === "speaking") {
        interruptResponse("Listening to your interruption");
      }
      if (!responseInProgress) {
        setAgentState("listening", "Listening");
      }
      break;
    case "speechEnd":
      transcriptState.textContent = "PROCESSING";
      break;
    case "transcript":
      if (event.transcript) {
        transcript.textContent = `${finalTranscript}${finalTranscript ? " " : ""}${event.transcript}`;
        transcriptState.textContent = "LIVE TRANSCRIPT";
      }
      break;
    case "speechComplete":
      if (typeof event.transcript === "string" && event.transcript.trim()) {
        finalTranscript = `${finalTranscript}${finalTranscript ? "\n" : ""}${event.transcript.trim()}`;
        transcript.textContent = finalTranscript;
        enqueueTurn(event.transcript);
      }
      transcriptState.textContent = "FINALIZED";
      break;
    case "sessionComplete":
      if (!sttHadError && !responseInProgress) {
        setAgentState("idle", "Ready when you are");
      }
      break;
    case "error":
      sttHadError = true;
      showError(event.message || "Speech transcription failed.");
      setAgentState("error", "Transcription error");
      break;
    default:
      break;
  }
}

microphoneButton.addEventListener("click", async () => {
  if (microphoneActive) {
    microphoneButton.disabled = true;
    microphoneButton.classList.remove("active");
    setAgentState("thinking", "Finishing your last words");
    if (sttSocket?.readyState === WebSocket.OPEN) {
      sttSocket.send(JSON.stringify({ type: "stop" }));
    } else {
      closeSttSocket();
    }
    return;
  }

  try {
    await startMicrophone();
  } catch (error) {
    sttHadError = true;
    showError(error instanceof Error ? error.message : "Could not start microphone.");
    stopMicrophone();
    closeSttSocket();
    microphoneActive = false;
    microphoneButton.disabled = false;
    microphoneButton.dataset.busy = "false";
    microphoneButton.classList.remove("active");
    microphoneButton.setAttribute("aria-pressed", "false");
    setAgentState("error", "Could not start listening");
  }
});

window.addEventListener("pagehide", () => {
  window.clearTimeout(sessionLimitTimer);
  stopCurrentAudio();
  stopMicrophone();
  if (playbackContext && playbackContext.state !== "closed") {
    void playbackContext.close();
    playbackContext = null;
  }
  closeSttSocket();
});

async function checkConnection() {
  try {
    const health = await fetch("/api/health");
    if (!health.ok) {
      throw new Error(`HTTP ${health.status}`);
    }
    connectionDot.dataset.state = "connected";
    connectionLabel.textContent = "Connected";
  } catch {
    connectionDot.dataset.state = "error";
    connectionLabel.textContent = "Backend unavailable";
  }
}

checkConnection();
