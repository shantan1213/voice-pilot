const microphoneButton = document.getElementById("microphone-button");
const stopResponseButton = document.getElementById("stop-response");
const avatar = document.getElementById("avatar");
const agentStatus = document.getElementById("agent-status");
const connectionDot = document.getElementById("connection-dot");
const connectionLabel = document.getElementById("connection-label");
const audioDot = document.getElementById("audio-dot");
const audioLabel = document.getElementById("audio-label");
const enableAudioButton = document.getElementById("enable-audio");
const faceButtons = {
  desi: document.getElementById("face-desi"),
  robo: document.getElementById("face-robo"),
};
const errorMessage = document.getElementById("error-message"); const actionLine = document.getElementById("action-line");
const typeForm = document.getElementById("type-box");
const typeInput = document.getElementById("type-input");
const micSelect = document.getElementById("mic-select");
const micLevelBar = document.querySelector("#mic-level i");

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
const MIC_STORAGE_KEY = "raala-mic-id";
let micAnalyser = null;
let micLevelTimer = null;
let micPeakLevel = 0;
let micSilentHintShown = false;
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
let activeTtsController = null; const ttsPrefetchCache = new Map(); function prefetchTts(text, runId) { if (!text || text.length > 1000 || !audioEnabled || runId !== speechRunId || ttsPrefetchCache.has(text)) return; ttsPrefetchCache.set(text, fetch("/api/tts", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({text: text, model: "mms"})}).then((r) => { if (!r.ok) throw 0; return r.arrayBuffer(); }).then((buf) => (buf && buf.byteLength > 44 ? buf : null)).catch(() => null)); if (ttsPrefetchCache.size > 4) ttsPrefetchCache.delete(ttsPrefetchCache.keys().next().value); } function fetchTtsCached(url, options) { try { const key = JSON.parse(options.body).text; const hit = ttsPrefetchCache.get(key); if (hit) { ttsPrefetchCache.delete(key); return hit.then((buf) => { if (!buf) throw 0; return {ok: true, status: 200, headers: {get: () => "audio/wav"}, arrayBuffer: async () => buf}; }); } } catch (e) {} return fetch(url, options); }
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
const WELCOME_INTRO = "Hello! I am Raala, your receptionist. I am here to schedule meetings for you — say the word and consider it done. For instance, I can set up your meeting with Mark Zuckerberg tomorrow. Just tap the microphone and tell me when you would like to meet.";

function storedFace() {
  try {
    return localStorage.getItem("voicePilotFace") === "robo" ? "robo" : "desi";
  } catch {
    return "desi";
  }
}

function setFace(name) {
  const face = name === "robo" ? "robo" : "desi";
  avatar.dataset.face = face;
  faceButtons.desi.setAttribute("aria-pressed", String(face === "desi"));
  faceButtons.robo.setAttribute("aria-pressed", String(face === "robo"));
  try {
    localStorage.setItem("voicePilotFace", face);
  } catch {
    // Private mode: keep the choice for this tab only.
  }
}

function setAgentState(state, message, keepCurrentText) {
  avatar.dataset.state = state;
  avatar.setAttribute("aria-label", `Voice-Pilot is ${state}`);
  agentStatus.dataset.state = state;
  agentStatus.textContent = keepCurrentText ? agentStatus.textContent : message;
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
    serverMs: metrics.serverMs || null, transcriptToFirstTextMs: duration(metrics.finalTranscriptAt, metrics.firstTextChunkAt),
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
  } else if (/\b(angry|mad|furious|annoyed|irritated|upset)\b/.test(content)) {
    emotion = "angry";
  } else if (/\b(excited|awesome|amazing|wonderful|fantastic|love it)\b/.test(content)) {
    emotion = "excited";
  } else if (/\b(shy|blush|embarrassed|awkward)\b/.test(content)) {
    emotion = "shy";
  } else if (/\b(confused|huh|what|puzzled|unsure|don.t understand)\b/.test(content)) {
    emotion = "confused";
  } else if (/\b(love|adore|darling|sweetheart|heart)\b/.test(content)) {
    emotion = "loving";
  } else if (/\b(focus|concentrate|careful|precisely|exactly)\b/.test(content)) {
    emotion = "focused";
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
  if (welcomeIntroPlayed || !audioEnabled || !playbackContext || !welcomeIntroBuffer || responseInProgress || responseQueueRunning) {
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
  stopMicLevelLoop();
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

function stopListeningForResponse() {
  listeningRequested = false;
  sttCloseReason = "turn"; listeningRequested = true;
  window.clearTimeout(sessionLimitTimer);
  stopMicrophone();
  microphoneActive = false;
  microphoneButton.classList.remove("active");
  microphoneButton.setAttribute("aria-pressed", "false");
  microphoneButton.setAttribute("aria-label", "Start listening");
  microphoneButton.title = "Start listening";
  if (sttSocket?.readyState === WebSocket.OPEN) {
    try {
      sttSocket.send(JSON.stringify({ type: "stop" }));
    } catch (error) {
      if (!(error instanceof DOMException)) {
        throw error;
      }
    }
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
  const gain = playbackContext.createGain();
  const fadeSeconds = Math.min(0.012, buffer.duration / 4);
  gain.gain.setValueAtTime(0, playbackContext.currentTime);
  gain.gain.linearRampToValueAtTime(1, playbackContext.currentTime + fadeSeconds);
  gain.gain.setValueAtTime(1, playbackContext.currentTime + buffer.duration - fadeSeconds);
  gain.gain.linearRampToValueAtTime(0, playbackContext.currentTime + buffer.duration);
  source.connect(gain);
  gain.connect(playbackContext.destination);
  if (activeAudioSource && activeAudioSource !== source) { try { activeAudioSource.stop(); } catch (e) {} } activeAudioSource = source;
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
        const ttsResponse = await fetchTtsCached("/api/tts", {
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
      appendAssistantDelta(runId, event.text);
      speechRemainder += event.text; { const speakingNow = cleanSpeechText(speechRemainder) || generatedText; agentStatus.textContent = speakingNow.length > 220 ? "..." + speakingNow.slice(-220) : speakingNow; }
      setEmotion(generatedText);
      const { sentences, remainder } = extractSentences(cleanSpeechText(speechRemainder));
      speechRemainder = remainder;
      sentences.forEach((sentence, i) => { void prefetchTts(sentences[i + 1], runId);
        void queueSpeech(sentence, runId);
      });
    } else if (event.type === "action") { showActionLink(event); } else if (event.type === "done") {
      completed = true; if (event.serverMs && activeTurnMetrics) activeTurnMetrics.serverMs = event.serverMs;
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

function enqueueTurn(text, opts) {
  hideActionLink();
  const useChat = (opts && opts.chat) || chatIsOpen();
  if (useChat && typeof text === "string" && text.trim()) {
    ensureChatMode();
    addChatBubble(text.trim(), "user");
    addThinkingBubble();
  }

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
    chat: useChat,
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
      const turnChatMode = !!turn.chat;
      const controller = new AbortController();
      const runId = speechRunId;
      activeResponseController = controller;
      responseInProgress = true;
      activeTurnMetrics = metrics;
      generatedResponse = "";
      microphoneButton.disabled = true;
      typeInput.disabled = true;
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
          body: JSON.stringify({ messages: responseMessages, session_id: sessionId, chat_mode: turnChatMode }),
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
            microphoneActive ? "Listening" : "Ready when you are", answer && answer.length <= 200,
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
          chatTurnFailed();
        }
      } finally {
        if (activeResponseController === controller) {
          activeResponseController = null;
        }
        responseInProgress = false;
        activeTurnMetrics = null;
        microphoneButton.disabled = false;
        typeInput.disabled = false;
        stopResponseButton.disabled = true;
      }
    }
  } finally {
    responseQueueRunning = false; if (responseQueue.length) { void processResponseQueue(); } else if (listeningRequested && !sttHadError && !microphoneActive && !hasPendingTurn() && (!sttSocket || sttSocket.readyState >= WebSocket.CLOSING)) { window.setTimeout(() => { if (listeningRequested && !microphoneActive && !hasPendingTurn() && !sttHadError) { void startMicrophone().catch(() => {}); } }, 400); }
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
      setAgentState(microphoneActive ? "listening" : "idle", "Voice replies are ready"); void preloadWelcomeIntro().then(() => playWelcomeIntro()).catch(() => {});
    }
  } catch (error) {
    showError(error instanceof Error ? error.message : "Audio could not be enabled.");
  }
});

function selectedMicId() {
  try { return window.localStorage.getItem(MIC_STORAGE_KEY) || ""; } catch (error) { return ""; }
}

async function refreshMicList() {
  if (!micSelect || !navigator.mediaDevices?.enumerateDevices) { return; }
  let devices = [];
  try { devices = await navigator.mediaDevices.enumerateDevices(); } catch (error) { return; }
  const inputs = devices.filter((device) => device.kind === "audioinput");
  const previous = micSelect.value || selectedMicId();
  micSelect.textContent = "";
  if (!inputs.length) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "No microphone found — check the headset connection";
    micSelect.appendChild(option);
    return;
  }
  inputs.forEach((device, index) => {
    const option = document.createElement("option");
    option.value = device.deviceId || "";
    option.textContent = device.label || `Microphone ${index + 1}`;
    micSelect.appendChild(option);
  });
  const stillThere = inputs.some((device) => (device.deviceId || "") === previous);
  const next = stillThere ? previous : (inputs[0]?.deviceId || "");
  micSelect.value = next;
  try { window.localStorage.setItem(MIC_STORAGE_KEY, next); } catch (error) {}
}

function stopMicLevelLoop() {
  if (micLevelTimer) { window.clearInterval(micLevelTimer); micLevelTimer = null; }
  micAnalyser = null;
  if (micLevelBar) { micLevelBar.style.transform = "scaleX(0)"; }
}

function startMicLevelLoop() {
  stopMicLevelLoop();
  if (!micAnalyser || !micLevelBar) { return; }
  micPeakLevel = 0;
  micSilentHintShown = false;
  const samples = new Float32Array(micAnalyser.fftSize);
  const startedAt = performance.now();
  micLevelTimer = window.setInterval(() => {
    if (!microphoneActive || !micAnalyser) { stopMicLevelLoop(); return; }
    micAnalyser.getFloatTimeDomainData(samples);
    let sum = 0;
    for (let index = 0; index < samples.length; index += 1) { sum += samples[index] * samples[index]; }
    const rms = Math.sqrt(sum / samples.length);
    micPeakLevel = Math.max(micPeakLevel, rms);
    micLevelBar.style.transform = `scaleX(${Math.min(1, rms * 4).toFixed(3)})`;
    if (!micSilentHintShown && performance.now() - startedAt > 8000 && micPeakLevel < 0.005) {
      micSilentHintShown = true;
      setAgentState("listening", "Listening — I can't hear anything. Check the microphone above.");
    }
  }, 150);
}

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
  const chosenMicId = (micSelect?.value || selectedMicId()).trim();
  const baseAudio = { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true };
  const micConstraints = chosenMicId ? { ...baseAudio, deviceId: { exact: chosenMicId } } : { ...baseAudio };
  let micPromise = navigator.mediaDevices.getUserMedia({ audio: micConstraints });
  micPromise = micPromise.catch((micError) => {
    if (chosenMicId && (micError?.name === "OverconstrainedError" || micError?.name === "NotFoundError")) {
      showError("That microphone is no longer available. Using the default microphone instead.");
      return navigator.mediaDevices.getUserMedia({ audio: { ...baseAudio } });
    }
    throw micError;
  });
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
  socket.onclose = async (event) => {
    if (sttSocket !== socket) {
      return;
    }
    if (event && event.code !== 1000 && sttCloseReason !== "turn") {
      sttHadError = true;
      if (errorMessage.hidden) {
        showError(`Speech connection closed unexpectedly (code ${event.code}).`);
      }
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
      if (listeningRequested && !sttHadError && !microphoneActive && !hasPendingTurn() && (!sttSocket || sttSocket.readyState >= WebSocket.CLOSING)) { setAgentState("connecting", "Listening again"); window.setTimeout(() => { if (listeningRequested && !microphoneActive && !hasPendingTurn() && !sttHadError) { void startMicrophone().catch(() => {}); } }, 400); } else { setAgentState(responseInProgress ? "thinking" : "idle", responseInProgress ? "Thinking" : "Ready when you are"); }
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
  micAnalyser = captureContext.createAnalyser();
  micAnalyser.fftSize = 512;
  source.connect(micAnalyser);
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
  startMicLevelLoop();
  void refreshMicList();
  microphoneButton.setAttribute("aria-pressed", "true");
  microphoneButton.setAttribute("aria-label", "Stop listening");
  microphoneButton.title = "Stop listening";
  setAgentState("listening", "Listening");

  sessionLimitTimer = window.setTimeout(() => {
    listeningRequested = false; showError("The 10-minute listening limit was reached.");
    closeSttSocket();
  }, STT_SESSION_LIMIT_MS);
}

let meetingSlots = [];
let meetingDate = "";
let meetingTz = "";

function showActionLink(event) {
  if (!actionLine) return;
  if (event && event.action === "meeting_slots" && Array.isArray(event.slots)) {
    renderMeetingSlots(event);
    return;
  }
  if (event && event.action === "meeting_form") {
    if (!chatIsOpen()) openMeetingForm(event);
    return;
  }
  if (event && event.action === "meeting_confirmation") {
    showMeetingConfirmation(event);
    return;
  }
  actionLine.innerHTML = "";
  if (event && event.url) {
    const link = document.createElement("a");
    link.href = event.url;
    link.target = "_blank";
    link.rel = "noopener";
    link.textContent = event.label || "Connect Google";
    actionLine.append(document.createTextNode("Tap to continue: "), link);
    actionLine.hidden = false;
  }
}

function hideActionLink() {
  if (actionLine) { actionLine.hidden = true; actionLine.innerHTML = ""; }
}

function renderMeetingSlots(event) {
  meetingSlots = event.slots || [];
  meetingDate = event.date || "";
  meetingTz = event.timezone || "";
  const panel = document.getElementById("slot-panel");
  const cards = document.getElementById("slot-cards");
  const heading = document.getElementById("slot-heading");
  if (!panel || !cards) return;
  cards.innerHTML = "";
  if (heading && meetingDate) {
    heading.textContent = `Available times for ${spokenDate(meetingDate)}${meetingTz ? ` (${meetingTz})` : ""}`;
  }
  for (const slot of meetingSlots) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "slot-card";
    button.textContent = slot.label;
    button.setAttribute("aria-label", `Book the ${slot.label} slot`);
    button.addEventListener("click", () => chooseMeetingSlot(slot));
    cards.append(button);
  }
  panel.hidden = meetingSlots.length === 0;
  syncMeetingSlotOptions();
}

function openMeetingForm(detail) {
  const wrap = document.getElementById("meeting-form-wrap");
  if (!wrap) return;
  if (detail && detail.timezone) {
    meetingTz = detail.timezone;
    syncMeetingSlotOptions();
  }
  if (detail && detail.start && detail.end) {
    syncMeetingSlotOptions({ start: detail.start, end: detail.end });
  }
  if (detail && detail.prefill) {
    const fieldMap = { topic: "mf-topic", meeting_type: "mf-type", name: "mf-name", email: "mf-email" };
    for (const key of Object.keys(fieldMap)) {
      const el = document.getElementById(fieldMap[key]);
      if (el && !el.value && detail.prefill[key]) {
        el.value = detail.prefill[key];
      }
    }
  }
  const when = document.getElementById("mf-when");
  if (when && meetingDate) {
    when.textContent = `${spokenDate(meetingDate)}${meetingTz ? ` (${meetingTz})` : ""} — your time is selected below.`;
    when.hidden = false;
  }
  if (!wrap.hidden) return;
  document.querySelector(".voice-stage")?.classList.add("split");
  wrap.hidden = false;
  requestAnimationFrame(() => wrap.classList.add("open"));
  try {
    wrap.scrollIntoView({ block: "nearest" });
  } catch (e) {}
  const firstEmpty = ["mf-name", "mf-email", "mf-topic"].map((id) => document.getElementById(id)).find((el) => el && !el.value);
  const target = firstEmpty || document.getElementById("mf-name");
  if (target && document.activeElement?.tagName !== "INPUT" && document.activeElement?.tagName !== "TEXTAREA") {
    target.focus({ preventScroll: true });
  }
}

function hideMeetingUi() {
  const panel = document.getElementById("slot-panel");
  const wrap = document.getElementById("meeting-form-wrap");
  if (panel) panel.hidden = true;
  if (wrap) { wrap.classList.remove("open"); wrap.hidden = true; }
  if (!document.getElementById("slot-panel") || document.getElementById("slot-panel").hidden) {
    document.querySelector(".voice-stage")?.classList.remove("split");
  }
}

function chooseMeetingSlot(slot) {
  syncMeetingSlotOptions(slot);
  if (!chatIsOpen()) openMeetingForm();
  enqueueTurn(`Book the ${slot.label} slot on ${meetingDate}.`);
}

function spokenDate(isoDate) {
  const parsed = new Date(`${isoDate}T12:00:00`);
  if (Number.isNaN(parsed.getTime())) return isoDate;
  return parsed.toLocaleDateString("en-US", { weekday: "long", month: "long", day: "numeric" });
}

function spokenSlot(label) {
  const match = /^(\d{1,2}):(\d{2})\s*([AP]M)$/i.exec(label || "");
  if (!match) return label;
  const hour = parseInt(match[1], 10);
  const suffix = match[3].toUpperCase();
  return match[2] === "00" ? `${hour} ${suffix}` : `${hour}:${match[2]} ${suffix}`;
}

function syncMeetingSlotOptions(selected) {
  const select = document.getElementById("mf-slot");
  if (!select) return;
  const wanted = selected
    ? JSON.stringify({ start: selected.start, end: selected.end })
    : select.value;
  select.innerHTML = "";
  const list = meetingSlots.length ? meetingSlots : (selected ? [selected] : []);
  for (const slot of list) {
    const option = document.createElement("option");
    option.value = JSON.stringify({ start: slot.start, end: slot.end });
    option.textContent = `${slot.label}${meetingDate ? ` — ${spokenDate(meetingDate)}` : ""}`;
    select.append(option);
  }
  if (wanted) {
    const has = Array.from(select.options).some((option) => option.value === wanted);
    if (has) select.value = wanted;
  }
}

function renderConfirmation(box, message, booking) {
  box.innerHTML = "";
  const check = document.createElement("span");
  check.className = "confirm-check";
  check.setAttribute("aria-hidden", "true");
  check.textContent = "✓";
  const content = document.createElement("span");
  content.className = "confirm-content";
  const text = document.createElement("span");
  text.textContent = message;
  content.append(text);
  if (booking && booking.booking_id) {
    const id = document.createElement("span");
    id.className = "confirm-id";
    id.textContent = `Booking ID ${booking.booking_id}`;
    content.append(id);
  }
  box.append(check, content);
}

function showMeetingConfirmation(event) {
  const box = document.getElementById("meeting-confirmation");
  if (!box) return;
  const booking = event.booking || {};
  renderConfirmation(box, event.message || "Booked.", booking);
  box.hidden = false;
  hideMeetingUi();
  try {
    box.scrollIntoView({ block: "nearest" });
  } catch (e) {}
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
        stopListeningForResponse();
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

faceButtons.desi.addEventListener("click", () => setFace("desi"));
faceButtons.robo.addEventListener("click", () => setFace("robo"));

typeForm.addEventListener("submit", (event) => {
  event.preventDefault();
  if (responseInProgress || responseQueueRunning) {
    return;
  }
  const value = typeInput.value.trim();
  if (!value) {
    return;
  }
  clearError();
  typeInput.value = "";
  enqueueTurn(value, { chat: true });
});
const meetingForm = document.getElementById("meeting-form");
if (meetingForm) {
  syncMeetingSlotOptions();
  meetingForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const errorBox = document.getElementById("mf-error");
    const showFormError = (message) => {
      if (errorBox) { errorBox.textContent = message; errorBox.hidden = false; }
      else { showError(message); }
    };
    if (errorBox) errorBox.hidden = true;
    let slot = null;
    try { slot = JSON.parse(document.getElementById("mf-slot").value || "null"); }
    catch (e) { slot = null; }
    if (!slot || !slot.start || !slot.end) { showFormError("Please pick one of the available times."); return; }
    const payload = {
      session_id: sessionId,
      name: document.getElementById("mf-name").value.trim(),
      email: document.getElementById("mf-email").value.trim(),
      phone: document.getElementById("mf-phone").value.trim(),
      topic: document.getElementById("mf-topic").value.trim(),
      meeting_type: document.getElementById("mf-type").value,
      notes: document.getElementById("mf-notes").value.trim(),
      timezone: meetingTz || Intl.DateTimeFormat().resolvedOptions().timeZone || "",
      start: slot.start,
      end: slot.end,
    };
    if (payload.name.length < 2) { showFormError("Please share your full name so I can book the meeting."); return; }
    if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(payload.email)) { showFormError("That email address does not look valid. Please check it."); return; }
    if (payload.topic.length < 2) { showFormError("Please share a short meeting topic."); return; }
    stopCurrentAudio();
    setBookingStatus("working", "Setting that up now — checking your slot, saving to calendar, and sending the confirmation.");
    setAgentState("thinking", "Setting that up now: you will get confirmation once it is ready.");
    try {
      const bar = document.getElementById("booking-status");
      if (bar) bar.scrollIntoView({ block: "nearest" });
    } catch (e) {}
    queueSpeech(
      `Booking your ${payload.meeting_type} about ${payload.topic} on ${spokenDate(meetingDate)} at ${spokenSlot(slot.label)} for ${payload.name}.`,
      speechRunId,
    ).catch(() => {});
    try {
      const result = await fetch("/api/meetings/book", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const body = await result.json().catch(() => ({}));
      if (!result.ok) {
        throw new Error(typeof body.detail === "string" ? body.detail : "Booking failed.");
      }
      const box = document.getElementById("meeting-confirmation");
      if (box) {
        renderConfirmation(box, body.message || "Booked.", body.booking || {});
        box.hidden = false;
      }
      hideMeetingUi();
      setBookingStatus("done", body.message || "Booked.", body.booking || {});
      try {
        const doneBox = document.getElementById("meeting-confirmation");
        if (doneBox) doneBox.scrollIntoView({ block: "nearest" });
      } catch (e) {}
      try {
        await queueSpeech("Your meeting is booked. " + "Booking ID " + ((body.booking || {}).booking_id || "") + ".", speechRunId);
      } catch (e) {}
    } catch (error) {
      setBookingStatus("error", error instanceof Error ? error.message : "Booking failed. Please try again.");
      showFormError(error instanceof Error ? error.message : "Booking failed. Please try again.");
    }
  });
  const cancelButton = document.getElementById("mf-cancel");
  if (cancelButton) {
    cancelButton.addEventListener("click", () => {
      const form = document.getElementById("meeting-form");
      if (form) form.reset();
      syncMeetingSlotOptions();
      hideMeetingUi();
      ensureChatMode();
      enqueueTurn("Cancel my meeting booking.", { chat: true });
    });
  }
}
initChatPane();
setFace(storedFace());

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

  if (responseInProgress || responseQueueRunning) {
    setAgentState("thinking", "Thinking — microphone is off");
    return;
  }

  listeningRequested = true;

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

if (micSelect) {
  micSelect.addEventListener("change", () => {
    try { window.localStorage.setItem(MIC_STORAGE_KEY, micSelect.value || ""); } catch (error) {}
    if (microphoneActive) {
      listeningRequested = true;
      if (sttSocket?.readyState === WebSocket.OPEN) {
        try { sttSocket.send(JSON.stringify({ type: "stop" })); } catch (error) {}
      } else {
        closeSttSocket();
        stopMicrophone();
        microphoneActive = false;
      }
      window.setTimeout(() => {
        if (listeningRequested && !microphoneActive && !hasPendingTurn()) {
          void startMicrophone().catch(() => {});
        }
      }, 600);
    }
  });
  if (navigator.mediaDevices?.addEventListener) {
    navigator.mediaDevices.addEventListener("devicechange", () => { void refreshMicList(); });
  }
  void refreshMicList();
}

let bookingPhase = 0;
let bookingStepTimer = null;
let bookingStatusState = "idle";
let bookingResult = null;

const BOOKING_STEPS = [
  { label: "Slot", active: "Checking your slot against the calendar…", done: "Slot verified and held." },
  { label: "Save", active: "Saving to the calendar and queuing the confirmation mail…", done: "Saved to the calendar. Confirmation mail queued." },
  { label: "Done", active: "Finishing up…", done: "" },
];

function setBookingStatus(state, message, booking) {
  const bar = document.getElementById("booking-status");
  if (!bar) return;
  if (bookingStepTimer) { clearTimeout(bookingStepTimer); bookingStepTimer = null; }
  bookingStatusState = state;
  if (state === "working") {
    bookingPhase = 0;
    bookingResult = null;
    renderBookingSteps();
    bookingStepTimer = setTimeout(() => {
      if (bookingStatusState !== "working") return;
      bookingPhase = 1;
      renderBookingSteps();
    }, 1500);
  } else if (state === "done") {
    bookingPhase = 2;
    bookingResult = { message, booking: booking || {} };
    renderBookingSteps();
  } else if (state === "error") {
    bookingResult = { error: message };
    renderBookingSteps();
  }
  bar.hidden = false;
  bar.dataset.state = state;
}

function renderBookingSteps() {
  const bar = document.getElementById("booking-status");
  if (!bar) return;
  bar.innerHTML = "";
  const stepsEl = document.createElement("div");
  stepsEl.className = "steps";
  const detail = document.createElement("div");
  detail.className = "steps-detail";
  detail.hidden = true;
  const showDetail = (text) => { detail.textContent = text; detail.hidden = false; };
  BOOKING_STEPS.forEach((step, index) => {
    let stepState = "pending";
    if (bookingStatusState === "done") {
      stepState = "done";
    } else if (bookingStatusState === "error") {
      stepState = index < bookingPhase ? "done" : index === bookingPhase ? "error" : "pending";
    } else {
      stepState = index < bookingPhase ? "done" : index === bookingPhase ? "active" : "pending";
    }
    let message = stepState === "done" ? step.done : stepState === "error" ? (bookingResult && bookingResult.error) || "Booking failed." : stepState === "active" ? step.active : "Waiting…";
    if (index === 2 && stepState === "done" && bookingResult && bookingResult.booking) {
      const id = bookingResult.booking.booking_id || "";
      message = (bookingResult.message || "Booked.") + (id ? ` Booking ID ${id}.` : "");
      const sheets = bookingResult.booking.sheets;
      if (sheets && sheets !== "saved") message += ` Note: sheet ${sheets}.`;
    }
    const node = document.createElement("button");
    node.type = "button";
    node.className = "step";
    node.dataset.stepState = stepState;
    node.title = message;
    node.setAttribute("aria-label", `${step.label}: ${message}`);
    const icon = document.createElement("span");
    icon.className = "step-icon";
    icon.textContent = stepState === "done" ? "✓" : stepState === "error" ? "!" : String(index + 1);
    icon.setAttribute("aria-hidden", "true");
    const label = document.createElement("span");
    label.className = "step-label";
    label.textContent = step.label;
    node.append(icon, label);
    node.addEventListener("click", () => showDetail(`${step.label}: ${message}`));
    stepsEl.append(node);
  });
  bar.append(stepsEl, detail);
}

function ensureChatMode() {
  document.querySelector(".voice-stage")?.classList.add("split");
  const pane = document.getElementById("chat-pane");
  if (pane) pane.hidden = false;
  const wrap = document.getElementById("meeting-form-wrap");
  if (wrap && !wrap.hidden) {
    wrap.classList.remove("open");
    wrap.hidden = true;
  }
}

function chatIsOpen() {
  const pane = document.getElementById("chat-pane");
  return !!pane && !pane.hidden;
}

function scrollThread() {
  const thread = document.getElementById("chat-thread");
  if (thread) thread.scrollTop = thread.scrollHeight;
}

function addChatBubble(text, kind) {
  const thread = document.getElementById("chat-thread");
  if (!thread || !text) return null;
  const bubble = document.createElement("div");
  bubble.className = `chat-msg ${kind}`;
  bubble.textContent = text;
  thread.append(bubble);
  scrollThread();
  return bubble;
}

let chatRunId = null;
let assistantBubble = null;
let thinkingBubbleEl = null;

function addThinkingBubble() {
  const thread = document.getElementById("chat-thread");
  if (!thread) return;
  if (thinkingBubbleEl) thinkingBubbleEl.remove();
  thinkingBubbleEl = document.createElement("div");
  thinkingBubbleEl.className = "chat-msg assistant thinking";
  thinkingBubbleEl.textContent = "Raala is thinking…";
  thread.append(thinkingBubbleEl);
  scrollThread();
  assistantBubble = null;
  chatRunId = null;
}

function appendAssistantDelta(runId, text) {
  const thread = document.getElementById("chat-thread");
  if (!thread || !chatIsOpen()) return;
  if (chatRunId !== runId || !assistantBubble) {
    if (thinkingBubbleEl) {
      thinkingBubbleEl.remove();
      thinkingBubbleEl = null;
    }
    assistantBubble = document.createElement("div");
    assistantBubble.className = "chat-msg assistant";
    thread.append(assistantBubble);
    chatRunId = runId;
  }
  assistantBubble.textContent += text;
  scrollThread();
}

function chatTurnFailed() {
  const thread = document.getElementById("chat-thread");
  if (!thread || !chatIsOpen()) return;
  if (thinkingBubbleEl) {
    thinkingBubbleEl.remove();
    thinkingBubbleEl = null;
  }
  if (!assistantBubble) {
    assistantBubble = document.createElement("div");
    assistantBubble.className = "chat-msg assistant";
    thread.append(assistantBubble);
  }
  if (!assistantBubble.textContent.trim()) {
    assistantBubble.textContent = "Sorry, that did not go through. Please try again.";
  }
  scrollThread();
  chatRunId = null;
  assistantBubble = null;
}

function initChatPane() {
  const stage = document.querySelector(".voice-stage");
  const anchor = document.querySelector(".howto");
  if (!stage || !anchor || document.getElementById("chat-pane")) return;
  const pane = document.createElement("section");
  pane.id = "chat-pane";
  pane.hidden = true;
  pane.setAttribute("aria-label", "Chat");
  pane.innerHTML = `<div class="chat-mode-tag">Chat mode</div><div id="chat-thread" class="chat-thread" aria-live="polite"></div>`;
  stage.insertBefore(pane, anchor);
  if (typeInput) {
    typeInput.placeholder = "Chat mode — type a message…";
    typeInput.setAttribute("aria-label", "Chat mode — type a message");
  }
}

const startButton = document.getElementById("start-button");
if (startButton) {
  startButton.addEventListener("click", async () => {
    startButton.disabled = true;
    clearError();
    setAgentState("connecting", "Getting things ready");
    try {
      await ensureAudioContext();
      await preloadWelcomeIntro();
      try {
        await fetch("/api/meetings/availability");
      } catch (e) {}
      startButton.hidden = true;
      await playWelcomeIntro();
      if (!responseInProgress) {
        setAgentState(microphoneActive ? "listening" : "idle", "Ready — tap the microphone and tell me when to meet");
      }
    } catch (error) {
      startButton.disabled = false;
      showError(error instanceof Error ? error.message : "Could not start. Please try again.");
      setAgentState("error", "Could not start");
    }
  });
}

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
