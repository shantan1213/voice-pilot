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

const STT_SAMPLE_RATE = 24000;
const STT_SESSION_LIMIT_MS = 10 * 60 * 1000;
const MAX_QUEUED_TURNS = 8;
const MAX_CONTEXT_TURNS = 3;
const sessionId = sessionStorage.getItem("voicePilotSessionId") || crypto.randomUUID();
sessionStorage.setItem("voicePilotSessionId", sessionId);
let sttSocket = null;
let microphoneStream = null;
let playbackContext = null;
let captureContext = null;
let audioProcessor = null;
let silenceGain = null;
let sessionLimitTimer = null;
let microphoneActive = false;
let sttHadError = false;
let generatedResponse = "";
const chatHistory = [];
let responseQueue = [];
let responseQueueRunning = false;
let activeResponseController = null;
let speechQueue = Promise.resolve();
let speechRunId = 0;
let activeAudioSource = null;
const activeStreamingSources = new Set();
let activeTtsController = null;
let responseInProgress = false;
let audioEnabled = false;
let sttReadyResolve = null;
let sttReadyReject = null;
let successTimer = null;
let historyTurns = [];
let assistantSpeaking = false;
let listeningRequested = false;
let sttCloseReason = null;
let sttReconnectTimer = null;
let sttReconnectAttempts = 0;
let firstAudioCapturedAt = null;
let activeTurnMetrics = null;
let welcomeIntroPlayed = false;
let welcomeIntroBuffer = null;
let welcomeIntroPromise = null;
const WELCOME_INTRO = "Hello there! I am Raala, your Voice-Pilot assistant. Tap the microphone and start speaking.";

function setAgentState(state, message) {
  avatar.dataset.state = state;
  avatar.setAttribute("aria-label", `Voice-Pilot is ${state}`);
  agentStatus.dataset.state = state;
  agentStatus.textContent = message;
  connectionDot.dataset.state = state === "error"
    ? "error"
    : microphoneActive || state === "connecting" ? "connected" : "idle";
  connectionLabel.textContent = state === "connecting"
    ? "Connecting"
    : microphoneActive
      ? state === "error" ? "Connection issue" : "Listening connection"
      : state === "error" ? "Disconnected" : "Ready";
}

function logTurnMetrics(metrics) {
  const duration = (start, end) => start != null && end != null ? Math.round(end - start) : null;
  console.info("Voice turn latency", {
    firstAudioCapturedMs: duration(metrics.firstAudioCapturedAt, metrics.finalTranscriptAt),
    transcriptToFirstTextMs: duration(metrics.finalTranscriptAt, metrics.firstTextChunkAt),
    firstTextToTtsMs: duration(metrics.firstTextChunkAt, metrics.ttsStartedAt),
    ttsToFirstAudioMs: duration(metrics.ttsStartedAt, metrics.firstAudioPlayableAt),
    playbackMs: duration(metrics.firstAudioPlayableAt, metrics.playbackCompletedAt),
  });
}

function showError(message) {
  errorMessage.textContent = message;
  errorMessage.hidden = false;
}

function clearError() {
  errorMessage.textContent = "";
  errorMessage.hidden = true;
}

function renderStoredTurn(turn) {
  chatHistory.push(
    { role: "user", content: turn.user },
    { role: "assistant", content: turn.assistant },
  );
}

async function loadConversationHistory() {
  try {
    const result = await fetch("/api/history");
    if (!result.ok) {
      throw new Error(`Conversation history could not be loaded (HTTP ${result.status}).`);
    }
    const payload = await result.json();
    if (!Array.isArray(payload.turns)) {
      throw new Error("The conversation history response was invalid.");
    }
    historyTurns = payload.turns;
    for (const turn of historyTurns) {
      renderStoredTurn(turn);
    }
  } catch (error) {
    showError(error instanceof Error ? error.message : "Conversation history could not be loaded.");
  }
}

const historyReady = loadConversationHistory();

function persistTurnInBackground(turn, assistant) {
  void fetch("/api/history/turn", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      session_id: sessionId,
      turn_id: turn.turnId,
      user: turn.question,
      assistant,
    }),
  }).then(async (result) => {
    if (!result.ok) {
      const payload = await result.json().catch(() => ({}));
      throw new Error(
        typeof payload.detail === "string"
          ? payload.detail
          : `Conversation logging failed (HTTP ${result.status}).`,
      );
    }
  }).catch((error) => {
    showError(error instanceof Error ? error.message : "Conversation history could not be saved.");
  });
}

function stopCurrentAudio() {
  speechRunId += 1;
  assistantSpeaking = false;
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
  for (const source of activeStreamingSources) {
    try {
      source.stop();
    } catch (error) {
      if (!(error instanceof DOMException && error.name === "InvalidStateError")) {
        throw error;
      }
    }
  }
  activeStreamingSources.clear();
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
  setAgentState(microphoneActive ? "listening" : "idle", status);
}

function setEmotion(text) {
  const content = text.toLowerCase();
  let emotion = "calm";
  if (/\b(congratulations|congrats|wonderful|fantastic|excellent|exciting|glad|happy|great job)\b/.test(content)) {
    emotion = "joyful";
  } else if (/\b(sorry|understand|difficult|tough|worried|concern|support|loss|painful)\b/.test(content)) {
    emotion = "caring";
  } else if (/\b(joke|funny|haha|laugh|playful|game|fun)\b/.test(content)) {
    emotion = "playful";
  } else if (/\b(wow|amazing|incredible|unbelievable|whoa)\b/.test(content)) {
    emotion = "surprised";
  } else if (/\b(tired|sleepy|sleep|good night|exhausted|yawn)\b/.test(content)) {
    emotion = "sleepy";
  } else if (/\b(sad|crying|tears|lonely|heartbroken)\b/.test(content)) {
    emotion = "sad";
  } else if (/\?|\b(curiously|curious|wonder|what if)\b/.test(content)) {
    emotion = "curious";
  }
  avatar.dataset.emotion = emotion;
}

function cleanSpeechText(text) {
  return text
    .replace(/```[\s\S]*?```/g, " ")
    .replace(/!\[([^\]]*)\]\([^)]+\)/g, "$1")
    .replace(/\[([^\]]+)\]\([^)]+\)/g, "$1")
    .replace(/^\s{0,3}#{1,6}\s*/gm, "")
    .replace(/^\s*(?:[-*+]\s+|\d+[.)](?:\s+|$))/gm, "")
    .replace(/^\s*>\s?/gm, "")
    .replace(/[`*_~]/g, "")
    .replace(/\s[-–—]\s/g, ", ")
    .replace(/[ \t]+/g, " ")
    .trim();
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
    ? "MMS English voice replies on"
    : "Tap the microphone to enable voice replies";
  enableAudioButton.hidden = audioEnabled;
  if (!audioEnabled) {
    throw new Error("Audio is blocked by the browser. Allow audio and tap the microphone again.");
  }
}

function preloadWelcomeIntro() {
  if (welcomeIntroPromise) {
    return welcomeIntroPromise;
  }
  welcomeIntroPromise = (async () => {
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    if (!AudioContextClass) {
      throw new Error("This browser does not support audio playback.");
    }
    if (!playbackContext || playbackContext.state === "closed") {
      playbackContext = new AudioContextClass();
    }
    const response = await fetch("/api/tts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: WELCOME_INTRO, model: "mms" }),
    });
    if (!response.ok) {
      throw new Error(`Welcome intro failed (HTTP ${response.status}).`);
    }
    welcomeIntroBuffer = await playbackContext.decodeAudioData(await response.arrayBuffer());
    return welcomeIntroBuffer;
  })().catch((error) => {
    welcomeIntroPromise = null;
    throw error;
  });
  return welcomeIntroPromise;
}

async function playWelcomeIntro() {
  if (welcomeIntroPlayed || !audioEnabled || !playbackContext || !welcomeIntroBuffer) {
    return;
  }
  welcomeIntroPlayed = true;
  setAgentState("speaking", "Hello there");
  audioDot.dataset.state = "loading";
  try {
    await playAudioBuffer(welcomeIntroBuffer, speechRunId);
  } catch (error) {
    welcomeIntroPlayed = false;
    audioDot.dataset.state = "error";
    showError(error instanceof Error ? error.message : "The welcome intro could not be played.");
  } finally {
    if (!responseInProgress) {
      audioDot.dataset.state = audioEnabled ? "ready" : "off";
    }
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

function hasPendingTurn() {
  return responseInProgress || responseQueueRunning || responseQueue.length > 0;
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
  const gain = playbackContext.createGain();
  const fadeSeconds = Math.min(0.012, buffer.duration / 4);
  gain.gain.setValueAtTime(0, playbackContext.currentTime);
  gain.gain.linearRampToValueAtTime(1, playbackContext.currentTime + fadeSeconds);
  gain.gain.setValueAtTime(1, playbackContext.currentTime + buffer.duration - fadeSeconds);
  gain.gain.linearRampToValueAtTime(0, playbackContext.currentTime + buffer.duration);
  source.connect(gain);
  gain.connect(playbackContext.destination);
  activeAudioSource = source;
  if (activeTurnMetrics && activeTurnMetrics.firstAudioPlayableAt == null) {
    activeTurnMetrics.firstAudioPlayableAt = performance.now();
  }
  assistantSpeaking = true;
  avatar.dataset.state = "speaking";
  agentStatus.textContent = "Speaking";
  audioDot.dataset.state = "speaking";

  return new Promise((resolve, reject) => {
    source.onended = () => {
      if (activeAudioSource === source) {
        activeAudioSource = null;
      }
      assistantSpeaking = false;
      if (runId === speechRunId && responseInProgress) {
        setAgentState("thinking", "Thinking");
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
      if (activeTurnMetrics && activeTurnMetrics.ttsStartedAt == null) {
        activeTurnMetrics.ttsStartedAt = performance.now();
      }
      audioDot.dataset.state = "loading";
      agentStatus.textContent = "Preparing voice";

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
        setAgentState("error", "Voice playback needs attention");
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
  const firstChunk = sentences.length === 0;
  if (remainder.length > (firstChunk ? 120 : 160)) {
    const comma = remainder.lastIndexOf(", ", 160);
    if (comma > 40) {
      sentences.push(remainder.slice(0, comma + 1).trim());
      remainder = remainder.slice(comma + 1).trimStart();
    }
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
      if (activeTurnMetrics && activeTurnMetrics.firstTextChunkAt == null) {
        activeTurnMetrics.firstTextChunkAt = performance.now();
      }
      generatedResponse = generatedText;
      speechRemainder += event.text;
      setEmotion(generatedText);
      const { sentences, remainder } = extractSentences(cleanSpeechText(speechRemainder));
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
    await queueSpeech(cleanSpeechText(speechRemainder), runId);
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
  responseQueue.push({
    question,
    turnId: crypto.randomUUID(),
    metrics: {
      firstAudioCapturedAt,
      finalTranscriptAt: performance.now(),
      firstTextChunkAt: null,
      ttsStartedAt: null,
      firstAudioPlayableAt: null,
      playbackCompletedAt: null,
    },
  });
  firstAudioCapturedAt = null;
  void processResponseQueue();
}

async function processResponseQueue() {
  if (responseQueueRunning) {
    return;
  }
  responseQueueRunning = true;
  try {
    await historyReady;
    while (responseQueue.length) {
      const turn = responseQueue.shift();
      const { question, metrics } = turn;
      const controller = new AbortController();
      const runId = speechRunId;
      activeResponseController = controller;
      responseInProgress = true;
      activeTurnMetrics = metrics;
      generatedResponse = "";
      stopResponseButton.disabled = false;
      setAgentState("thinking", "Thinking");

      try {
        const responseMessages = [];
        let contextCharacters = question.length;
        for (let index = chatHistory.length - 2; index >= 0; index -= 2) {
          const pair = chatHistory.slice(index, index + 2);
          const pairCharacters = pair.reduce((total, message) => total + message.content.length, 0);
          if (responseMessages.length / 2 >= MAX_CONTEXT_TURNS
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
        generatedResponse = answer;
        chatHistory.push(
          { role: "user", content: question },
          { role: "assistant", content: answer },
        );
        persistTurnInBackground(turn, answer);
        metrics.playbackCompletedAt = performance.now();
        logTurnMetrics(metrics);
        if (controller.signal.aborted) {
          setAgentState(microphoneActive ? "listening" : "idle", "Response interrupted");
        } else {
          setAgentState(
            microphoneActive ? "listening" : "idle",
            microphoneActive ? "Listening" : "Ready when you are",
          );
        }
      } catch (error) {
        if (controller.signal.aborted) {
          setAgentState(
            microphoneActive ? "listening" : "idle",
            generatedResponse ? "Response interrupted" : "Response stopped",
          );
        } else {
          showError(error instanceof Error ? error.message : "Voice-Pilot could not complete the response.");
          setAgentState("error", "Something went wrong");
        }
      } finally {
        if (activeResponseController === controller) {
          activeResponseController = null;
        }
        responseInProgress = false;
        activeTurnMetrics = null;
        stopResponseButton.disabled = true;
      }
    }
  } finally {
    responseQueueRunning = false;
    if (listeningRequested && !microphoneActive && !sttSocket && !hasPendingTurn()) {
      window.clearTimeout(sttReconnectTimer);
      sttReconnectTimer = window.setTimeout(() => {
        if (listeningRequested && !microphoneActive && !hasPendingTurn()) {
          void startMicrophone();
        }
      }, 0);
    }
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
    void preloadWelcomeIntro()
      .then(() => playWelcomeIntro())
      .catch((error) => {
        showError(error instanceof Error ? error.message : "The welcome intro could not be loaded.");
      });
  } catch (error) {
    audioEnabled = false;
    audioDot.dataset.state = "error";
    audioLabel.textContent = "Tap the microphone to enable voice replies";
    showError(error instanceof Error ? error.message : "Audio could not be enabled.");
  }

  microphoneButton.disabled = true;
  microphoneButton.dataset.busy = "true";
  setAgentState("connecting", "Connecting to speech recognition");
  sttHadError = false;
  sttCloseReason = null;
  firstAudioCapturedAt = null;

  // Parallel mic startup: permission plus device setup overlaps the STT handshake.
  const micPromise = navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
  micPromise.catch(() => {});
  micPromise.then((stream) => { if (sttSocket !== socket) { stream.getTracks().forEach((track) => track.stop()); } else { microphoneStream = stream; } }, () => {});
  const websocketProtocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const socket = new WebSocket(`${websocketProtocol}//${window.location.host}/api/stt`);
  sttSocket = socket;
  socket.binaryType = "arraybuffer";
  sttReadyResolve = null;
  sttReadyReject = null;
  const readyPromise = new Promise((resolve, reject) => {
    sttReadyResolve = resolve;
    sttReadyReject = reject;
  });
  let readyTimeout;

  socket.onopen = () => {
    try {
      socket.send(JSON.stringify({ type: "start" }));
    } catch (error) {
      sttReadyReject?.(error);
    }
  };
  socket.onmessage = (message) => {
    try {
      handleSttEvent(JSON.parse(message.data));
    } catch (error) {
      sttHadError = true;
      showError(error instanceof Error ? error.message : "Received an invalid transcription response.");
      setAgentState("error", "Transcription error");
      sttReadyReject?.(error);
    }
  };
  socket.onerror = () => {
    sttReadyReject?.(new Error("Speech connection failed. Check the backend, API key, and network connection."));
    if (!sttHadError) {
      sttHadError = true;
      showError("Speech connection failed. Check the backend, API key, and network connection.");
      setAgentState("error", "Speech connection error");
    }
  };
  socket.onclose = async () => {
    if (sttSocket !== socket) {
      return;
    }
    sttReadyReject?.(new Error("The speech connection closed before recognition became ready."));
    sttReadyResolve = null;
    sttReadyReject = null;
    window.clearTimeout(sessionLimitTimer);
    microphoneActive = false;
    stopMicrophone();
    microphoneButton.disabled = false;
    microphoneButton.dataset.busy = "false";
    microphoneButton.classList.remove("active");
    microphoneButton.setAttribute("aria-pressed", "false");
    microphoneButton.setAttribute("aria-label", "Start listening");
    microphoneButton.title = "Start listening";
    if (sttCloseReason === "turn") {
      setAgentState(responseInProgress ? "thinking" : "idle", responseInProgress ? "Thinking" : "Ready when you are");
    } else if (sttHadError && !errorMessage.hidden) {
      setAgentState("error", "Speech connection error");
    } else if (!responseInProgress) {
      setAgentState("idle", "Ready when you are");
    }
    if (sttHadError && sttCloseReason !== "turn" && listeningRequested && !hasPendingTurn()
      && sttReconnectAttempts < 3) {
      const delay = 500 * (2 ** sttReconnectAttempts);
      sttReconnectAttempts += 1;
      setAgentState("connecting", `Reconnecting to speech recognition (${sttReconnectAttempts}/3)`);
      window.clearTimeout(sttReconnectTimer);
      sttReconnectTimer = window.setTimeout(() => {
        if (listeningRequested && !microphoneActive && !hasPendingTurn()) {
          void startMicrophone();
        }
      }, delay);
    }
    sttSocket = null;
  };

  try {
    await Promise.race([
      readyPromise,
      new Promise((_, reject) => {
        readyTimeout = window.setTimeout(
          () => reject(new Error("Speech recognition did not become ready in time. Check the API connection and try again.")),
          20_000,
        );
      }),
    ]);
  } finally {
    window.clearTimeout(readyTimeout);
    sttReadyResolve = null;
    sttReadyReject = null;
  }

  try { microphoneStream = await micPromise; } catch (micError) { closeSttSocket(); throw micError instanceof Error ? micError : new Error("Could not start microphone."); }
  // Legacy inline mic request moved above for parallel startup; no-op.
  void ({ audio: {
      channelCount: 1,
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true,
    },
  });
  if (sttSocket !== socket || socket.readyState !== WebSocket.OPEN) {
    stopMicrophone();
    throw new Error("Speech recognition disconnected before microphone capture started. Please try again.");
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
    const sendingSilence = responseInProgress || assistantSpeaking;
    if (!sendingSilence && firstAudioCapturedAt == null) {
      firstAudioCapturedAt = performance.now();
    }
    const buffer = new ArrayBuffer(samples.length * 2);
    const view = new DataView(buffer);
    for (let index = 0; index < samples.length; index += 1) {
      const sample = sendingSilence ? 0 : Math.max(-1, Math.min(1, samples[index]));
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
      sttReconnectAttempts = 0;
      microphoneButton.disabled = false;
      microphoneButton.dataset.busy = "false";
      setAgentState("listening", "Listening");
      sttReadyResolve?.();
      sttReadyResolve = null;
      sttReadyReject = null;
      break;
    case "speechStart":
      if (!responseInProgress) {
        setAgentState("listening", "Listening");
      }
      break;
    case "speechEnd":
      if (!responseInProgress) {
        setAgentState("thinking", "Thinking");
      }
      break;
    case "transcript":
      if (event.transcript && !responseInProgress) {
        setAgentState("listening", "Listening");
      }
      break;
    case "speechComplete":
      if (!assistantSpeaking && !responseInProgress
        && typeof event.transcript === "string" && event.transcript.trim()) {
        enqueueTurn(event.transcript);
      }
      break;
    case "sessionComplete":
      if (!sttHadError && !responseInProgress) {
        setAgentState("idle", "Ready when you are");
      }
      break;
    case "error":
      sttHadError = true;
      {
        const message = event.message || "Speech transcription failed.";
        showError(message);
        setAgentState("error", "Transcription error");
        sttReadyReject?.(new Error(message));
      }
      break;
    default:
      break;
  }
}

microphoneButton.addEventListener("click", async () => {
  if (microphoneActive) {
    listeningRequested = false;
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

  listeningRequested = true;
  if (responseInProgress) {
    setAgentState("thinking", "Finishing response");
    return;
  }

  try {
    await startMicrophone();
  } catch (error) {
    listeningRequested = false;
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
void preloadWelcomeIntro().catch((error) => {
  console.warn("Welcome intro could not be preloaded.", error);
});
