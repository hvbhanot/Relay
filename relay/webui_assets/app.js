const $ = (id) => document.getElementById(id);

function chatMount() {
  return $("chatInner") || $("chatLog");
}

function scrollChat() {
  const log = $("chatLog");
  if (log) log.scrollTop = log.scrollHeight;
}

const LOCAL_PROVIDER_DEFAULTS = {
  ollama: { label: "Ollama URL", base: "http://localhost:11434", model: "llama3.1:8b" },
  lmstudio: { label: "LM Studio URL", base: "http://localhost:1234/v1", model: "local-model" },
  llamacpp: { label: "llama.cpp URL", base: "http://localhost:8080/v1", model: "local-model" },
  vllm: { label: "vLLM URL", base: "http://localhost:8000/v1", model: "local-model" },
};

const fields = [
  "local_provider",
  "ollama_base_url",
  "ollama_model",
  "request_timeout_seconds",
  "cloud_enabled",
  "cloud_provider",
  "openrouter_model",
  "openrouter_api_key",
  "openrouter_api_key_clear",
  "openrouter_base_url",
  "openrouter_site_url",
  "openrouter_app_name",
  "cloud_model_map",
  "openai_compat_base_url",
  "openai_compat_model",
  "openai_compat_api_key",
  "openai_compat_api_key_clear",
  "privacy_mode",
  "min_local_confidence",
  "max_subtasks",
  "planner_preview_enabled",
];

let currentConfig = null;
let currentSessionId = null;
let busy = false;
let chatHistory = [];
let lastTraceData = null;
let liveTracePlan = null;

const COMPOSER_MIN_HEIGHT = 24;
const COMPOSER_MAX_HEIGHT = 200;
const MAX_ATTACHMENTS = 8;
const MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024;

let pendingAttachments = [];

function resizeComposer() {
  const input = $("messageInput");
  if (!input) return;
  input.style.height = "auto";
  const height = Math.max(COMPOSER_MIN_HEIGHT, Math.min(input.scrollHeight, COMPOSER_MAX_HEIGHT));
  input.style.height = `${height}px`;
  input.style.overflowY = input.scrollHeight > COMPOSER_MAX_HEIGHT ? "auto" : "hidden";
}

function resetComposer() {
  const input = $("messageInput");
  if (!input) return;
  input.style.height = "";
  input.style.overflowY = "";
  resizeComposer();
}

function formatFileSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function readFileAsBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const result = String(reader.result || "");
      const comma = result.indexOf(",");
      resolve(comma >= 0 ? result.slice(comma + 1) : result);
    };
    reader.onerror = () => reject(new Error(`Could not read ${file.name}`));
    reader.readAsDataURL(file);
  });
}

function revokeAttachmentPreviews(items = pendingAttachments) {
  for (const item of items) {
    if (item.previewUrl) URL.revokeObjectURL(item.previewUrl);
  }
}

function clearAttachments() {
  revokeAttachmentPreviews();
  pendingAttachments = [];
  renderAttachmentTray();
  const input = $("fileInput");
  if (input) input.value = "";
}

function removeAttachment(index) {
  const [removed] = pendingAttachments.splice(index, 1);
  if (removed?.previewUrl) URL.revokeObjectURL(removed.previewUrl);
  renderAttachmentTray();
}

function serializeAttachments(items = pendingAttachments) {
  return items.map((item) => ({
    name: item.name,
    mime: item.mime,
    data: item.data,
  }));
}

function renderAttachmentTray() {
  const tray = $("attachmentTray");
  if (!tray) return;
  if (!pendingAttachments.length) {
    tray.hidden = true;
    tray.innerHTML = "";
    return;
  }
  tray.hidden = false;
  tray.innerHTML = "";
  pendingAttachments.forEach((item, index) => {
    const chip = document.createElement("div");
    chip.className = `attachment-chip${item.kind === "image" ? " image" : " text"}`;
    if (item.kind === "image" && item.previewUrl) {
      const thumb = document.createElement("img");
      thumb.className = "attachment-thumb";
      thumb.src = item.previewUrl;
      thumb.alt = "";
      chip.appendChild(thumb);
    } else {
      const icon = document.createElement("span");
      icon.className = "attachment-icon";
      icon.textContent = "TXT";
      chip.appendChild(icon);
    }
    const meta = document.createElement("div");
    meta.className = "attachment-meta";
    const name = document.createElement("span");
    name.className = "attachment-name";
    name.textContent = item.name;
    const size = document.createElement("span");
    size.className = "attachment-size";
    size.textContent = formatFileSize(item.size_bytes);
    meta.append(name, size);
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "attachment-remove";
    remove.setAttribute("aria-label", `Remove ${item.name}`);
    remove.textContent = "×";
    remove.addEventListener("click", () => removeAttachment(index));
    chip.append(meta, remove);
    tray.appendChild(chip);
  });
}

function localModelSupportsVision(model) {
  return /llava|moondream|bakllava|minicpm-v|gemma[34]|vision|qwen2(\.5)?-vl|qwen3-vl|llama3\.[23]-vision|pixtral|cogvlm/i.test(model || "");
}

function warnIfImageNeedsCloudSetup(files) {
  const hasImage = [...(files || [])].some((file) => file.type.startsWith("image/"));
  if (!hasImage || !currentConfig) return;
  const localModel = currentConfig.ollama_model || "";
  if (localModelSupportsVision(localModel)) return;
  toast(
    `Images need a vision model. Your local model (${localModel || "unset"}) cannot see images. Run: ollama pull llava — then set Local model to llava in Settings.`,
    "error",
  );
}

async function addAttachmentFiles(fileList) {
  const files = [...(fileList || [])];
  if (!files.length) return;
  warnIfImageNeedsCloudSetup(files);
  const slots = MAX_ATTACHMENTS - pendingAttachments.length;
  if (slots <= 0) {
    toast(`Maximum ${MAX_ATTACHMENTS} files per message.`, "error");
    return;
  }
  const accepted = files.slice(0, slots);
  if (files.length > slots) toast(`Only ${slots} more file(s) can be attached.`, "error");
  for (const file of accepted) {
    if (file.size > MAX_ATTACHMENT_BYTES) {
      toast(`${file.name} exceeds the 5MB limit.`, "error");
      continue;
    }
    try {
      const data = await readFileAsBase64(file);
      const kind = file.type.startsWith("image/") ? "image" : "text";
      pendingAttachments.push({
        name: file.name,
        mime: file.type || "application/octet-stream",
        kind,
        data,
        size_bytes: file.size,
        previewUrl: kind === "image" ? URL.createObjectURL(file) : null,
      });
    } catch (err) {
      toast(err.message, "error");
    }
  }
  renderAttachmentTray();
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "content-type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const text = await res.text();
  let data;
  try { data = text ? JSON.parse(text) : {}; } catch { data = { ok: false, error: { message: text } }; }
  if (!res.ok || data.ok === false) {
    const msg = data?.error?.message || `${res.status} ${res.statusText}`;
    throw new Error(msg);
  }
  return data;
}

function toast(message, kind = "") {
  const el = $("toast");
  el.textContent = message;
  el.className = `toast ${kind}`.trim();
  el.hidden = false;
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => { el.hidden = true; }, 4200);
}

function setPill(id, text, kind = "muted") {
  const el = $(id);
  el.textContent = text;
  el.className = `pill ${kind}`;
}

function setBusy(nextBusy) {
  busy = nextBusy;
  $("sendButton").disabled = nextBusy;
  $("messageInput").disabled = nextBusy;
  const attach = $("attachButton");
  if (attach) attach.disabled = nextBusy;
}

function applyConfig(config) {
  currentConfig = config;
  for (const id of fields) {
    const el = $(id);
    if (!el) continue;
    if (el.type === "checkbox") {
      if (id.endsWith("_clear")) el.checked = false;
      else if (id === "planner_preview_enabled") el.checked = config[id] !== false;
      else el.checked = Boolean(config[id]);
    } else if (id === "cloud_model_map") {
      el.value = JSON.stringify(config[id] ?? {}, null, 2);
    } else if (!id.endsWith("api_key")) {
      el.value = config[id] ?? "";
    } else {
      el.value = "";
      const hasKey =
        id === "openrouter_api_key"
          ? config.has_openrouter_api_key
          : config.has_openai_compat_api_key;
      const keyUsable =
        id === "openrouter_api_key"
          ? config.openrouter_api_key_usable
          : config.openai_compat_api_key_usable;
      if (hasKey && keyUsable === false) {
        el.placeholder = "Invalid key saved — paste sk-or-v1-… from openrouter.ai/keys";
      } else {
        el.placeholder = hasKey ? "Saved key present; leave blank to keep" : "Paste sk-or-v1-… key from openrouter.ai/keys";
      }
    }
  }
  $("configPath").textContent = `Saved to ${config.config_path}`;
  const secretsInfo = $("secretsInfo");
  if (secretsInfo) {
    const path = config.secrets_path ? ` (${config.secrets_path})` : "";
    secretsInfo.textContent = `API keys are stored in an encrypted local vault${path}, not relay.ui.json.`;
  }
  renderHeaderStatus(config);
  updateProviderBlocks();
  updateLocalProviderHints();
  updatePrivacyModeHelp();
  setPill("saveState", "saved", "good");
}

const PRIVACY_MODE_HELP = {
  balanced:
    "Default. High-sensitivity and secret-like prompts stay local; difficult work routes to cloud when enabled.",
  strict:
    "Stricter. Medium- and high-sensitivity subtasks stay local; only simple low-risk tasks may use cloud.",
  permissive:
    "Looser. Sensitivity alone does not block cloud — only explicit secret keywords force local execution.",
};

function updatePrivacyModeHelp() {
  const el = $("privacyModeHelp");
  const mode = $("privacy_mode")?.value || "balanced";
  if (el) el.textContent = PRIVACY_MODE_HELP[mode] || PRIVACY_MODE_HELP.balanced;
}

function cloudPoolSize(config) {
  // Distinct models actually reachable, not capability-map entries (many map to
  // the same model). OpenRouter pools the default model + each mapped model;
  // a single OpenAI-compatible endpoint is just its one model.
  if (config.cloud_provider !== "openrouter") return 1;
  const distinct = new Set(Object.values(config.cloud_model_map || {}));
  if (config.openrouter_model) distinct.add(config.openrouter_model);
  return distinct.size;
}

function truncateStatus(value, max = 14) {
  const text = String(value || "").trim();
  if (text.length <= max) return text;
  return `${text.slice(0, max - 1)}…`;
}

function renderHeaderStatus(config) {
  const el = $("headerStatus");
  if (!el) return;
  const cloudOn = Boolean(config.cloud_enabled);
  const poolSize = cloudOn ? cloudPoolSize(config) : 0;
  const model = config.ollama_model || "unset";
  const localDot = document.createElement("span");
  localDot.className = "status-dot";
  const localText = document.createElement("span");
  localText.textContent = truncateStatus(model, 22);
  localText.title = `Local: ${model}`;
  const sep1 = document.createElement("span");
  sep1.className = "status-sep";
  sep1.textContent = "·";
  const cloudDot = document.createElement("span");
  cloudDot.className = `status-dot${cloudOn ? " cloud" : " off"}`;
  const cloudText = document.createElement("span");
  cloudText.textContent = cloudOn ? `${poolSize} cloud` : "cloud off";
  cloudText.title = cloudOn ? `${poolSize} cloud model${poolSize === 1 ? "" : "s"}` : "Cloud fallback disabled";
  const sep2 = document.createElement("span");
  sep2.className = "status-sep";
  sep2.textContent = "·";
  const privacyText = document.createElement("span");
  privacyText.textContent = config.privacy_mode || "balanced";
  privacyText.title = `Privacy: ${config.privacy_mode || "balanced"}`;
  el.replaceChildren(localDot, localText, sep1, cloudDot, cloudText, sep2, privacyText);
}

function collectConfig() {
  const payload = {};
  for (const id of fields) {
    const el = $(id);
    if (!el) continue;
    if (el.type === "checkbox") payload[id] = el.checked;
    else if (el.type === "number") payload[id] = Number(el.value);
    else payload[id] = el.value;
  }
  return payload;
}

function updateLocalProviderHints() {
  const provider = $("local_provider")?.value || "ollama";
  const defaults = LOCAL_PROVIDER_DEFAULTS[provider] || LOCAL_PROVIDER_DEFAULTS.ollama;
  const label = $("local_base_url_label");
  const base = $("ollama_base_url");
  const model = $("ollama_model");
  if (label) label.textContent = defaults.label;
  if (base && !base.value) base.placeholder = defaults.base;
  if (model && !model.value) model.placeholder = defaults.model;
}

function updateProviderBlocks() {
  const selected = $("cloud_provider").value;
  document.querySelectorAll(".provider-block").forEach((block) => {
    block.hidden = block.dataset.provider !== selected;
  });
}

function openDrawer(name) {
  const drawer = $(`drawer-${name}`);
  if (!drawer) return;
  document.querySelectorAll(".drawer").forEach((d) => d.classList.toggle("open", d === drawer));
  $("scrim").classList.add("show");
  document.querySelectorAll(".tool-btn[data-view]").forEach((b) => b.classList.toggle("active", b.dataset.view === name));
  const toolBtn = document.querySelector(`.tool-btn[data-view="${name}"]`);
  if (toolBtn) toolBtn.classList.remove("notify");
  if (name === "trace" && lastTraceData) paintTrace(lastTraceData);
  if (name === "history") loadHistoryList().catch((err) => toast(err.message, "error"));
}

function closeDrawers() {
  document.querySelectorAll(".drawer").forEach((d) => d.classList.remove("open"));
  $("scrim").classList.remove("show");
  document.querySelectorAll(".tool-btn[data-view]").forEach((b) => b.classList.remove("active"));
}

function toggleDrawer(name) {
  const drawer = $(`drawer-${name}`);
  if (drawer && drawer.classList.contains("open")) closeDrawers();
  else openDrawer(name);
}

async function loadConfig() {
  const data = await api("/api/config");
  applyConfig(data.config);
}

async function saveSetup() {
  setPill("saveState", "saving", "warn");
  try {
    const data = await api("/api/config", { method: "POST", body: JSON.stringify(collectConfig()) });
    applyConfig(data.config);
    toast("Setup saved. Future chats will use this configuration.", "good");
  } catch (err) {
    setPill("saveState", "error", "bad");
    toast(err.message, "error");
  }
}

async function refreshModels() {
  try {
    const data = await api("/api/ollama/models", { method: "POST", body: JSON.stringify(collectConfig()) });
    const list = $("ollamaModels");
    list.innerHTML = "";
    for (const name of data.models || []) {
      const opt = document.createElement("option");
      opt.value = name;
      list.appendChild(opt);
    }
    const provider = data.provider || currentConfig?.local_provider || "local";
    toast(data.models?.length ? `Found ${data.models.length} ${provider} model(s).` : `${provider} is reachable, but no models were returned.`, "good");
  } catch (err) {
    toast(err.message, "error");
  }
}

async function ping(path, label) {
  try {
    const data = await api(path, { method: "POST", body: JSON.stringify(collectConfig()) });
    toast(`${label} ok in ${data.latency_seconds}s: ${data.response}`, "good");
  } catch (err) {
    toast(err.message, "error");
  }
}

function renderMessageAttachments(attachments, target) {
  if (!attachments?.length) return;
  const wrap = document.createElement("div");
  wrap.className = "msg-attachments";
  for (const item of attachments) {
    const chip = document.createElement("div");
    chip.className = `msg-attachment${item.kind === "image" ? " image" : " text"}`;
    if (item.kind === "image" && item.data) {
      const mime = item.mime || "image/png";
      const img = document.createElement("img");
      img.className = "msg-attachment-img";
      img.src = `data:${mime};base64,${item.data}`;
      img.alt = item.name || "Attached image";
      img.loading = "lazy";
      chip.appendChild(img);
    } else {
      const label = document.createElement("span");
      label.className = "msg-attachment-label";
      label.textContent = item.name || "file";
      chip.appendChild(label);
    }
    wrap.appendChild(chip);
  }
  target.appendChild(wrap);
}

function rebuildChatHistory(messages) {
  chatHistory = (messages || [])
    .filter((message) => message.role === "user" || message.role === "assistant")
    .map((message) => ({ role: message.role, content: message.content || "" }));
}

function conversationPayload() {
  return chatHistory.map((message) => ({ role: message.role, content: message.content }));
}

function appendMessage(role, text, { markdown = false, attachments = null, trackHistory = true } = {}) {
  const log = chatMount();
  const article = document.createElement("article");
  article.className = `msg ${role}`;
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  if (role === "assistant" && markdown) {
    const answer = document.createElement("div");
    answer.className = "answer";
    answer.innerHTML = renderMarkdown(text);
    bubble.appendChild(answer);
    if (text) addAnswerToolbar(bubble);
    article.className = "msg assistant has-answer";
    article.append(makeAvatar(role), bubble);
  } else {
    if (attachments?.length) renderMessageAttachments(attachments, bubble);
    if (text) {
      const p = document.createElement("p");
      p.textContent = text;
      bubble.appendChild(p);
    }
    if (role === "user") article.append(bubble);
    else article.append(makeAvatar(role), bubble);
  }
  log.appendChild(article);
  if (trackHistory && text && (role === "user" || role === "assistant")) {
    chatHistory.push({ role, content: text });
  }
  scrollChat();
}

async function ensureSession() {
  if (currentSessionId) return currentSessionId;
  const data = await api("/api/history", { method: "POST", body: "{}" });
  currentSessionId = data.session.id;
  return currentSessionId;
}

// Pass an explicit sessionId to pin the write to a specific chat. Without it the
// global currentSessionId is used, which can change if the user opens another
// chat mid-stream — so a finished answer must always pin to the session it began in.
async function persistMessage(role, content, trace = null, sessionId = null) {
  const targetId = sessionId || (await ensureSession());
  await api(`/api/history/${targetId}/messages`, {
    method: "POST",
    body: JSON.stringify({ role, content, trace }),
  });
  return targetId;
}

async function loadHistoryList() {
  const data = await api("/api/history");
  const list = $("historyList");
  const path = $("historyPath");
  if (path) path.textContent = data.history_path ? `Saved to ${data.history_path}` : "";
  list.innerHTML = "";
  const sessions = data.sessions || [];
  if (!sessions.length) {
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = "No saved chats yet. Start a conversation to create one.";
    list.appendChild(empty);
    return;
  }
  for (const session of sessions) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = `history-item${session.id === currentSessionId ? " active" : ""}`;
    const title = document.createElement("span");
    title.className = "history-title";
    title.textContent = session.title || "Untitled chat";
    const meta = document.createElement("span");
    meta.className = "history-meta";
    meta.textContent = `${session.message_count || 0} msgs`;
    btn.append(title, meta);
    btn.addEventListener("click", () => openHistorySession(session.id));
    list.appendChild(btn);
  }
}

async function openHistorySession(sessionId) {
  const data = await api(`/api/history/${sessionId}`);
  currentSessionId = sessionId;
  await api(`/api/history/${sessionId}/activate`, { method: "POST", body: "{}" });
  renderSession(data.session);
  await loadHistoryList();
  closeDrawers();
}

function renderSession(session) {
  const log = chatMount();
  log.innerHTML = "";
  const messages = session?.messages || [];
  if (!messages.length) {
    rebuildChatHistory([]);
    renderWelcome();
    return;
  }
  rebuildChatHistory(messages);
  let lastTrace = null;
  for (const message of messages) {
    if (message.role === "user") appendMessage("user", message.content || "", { trackHistory: false });
    else if (message.role === "assistant") appendMessage("assistant", message.content || "", { markdown: true, trackHistory: false });
    if (message.trace) lastTrace = { trace: message.trace, routes: traceToRoutes(message.trace) };
  }
  if (lastTrace) renderTrace(lastTrace);
  else resetTracePanel();
  scrollChat();
}

function traceToRoutes(trace) {
  const results = trace?.results || [];
  return results.map((result) => ({
    id: result.subtask?.id,
    title: result.subtask?.title,
    route: result.route,
    model: result.model,
    confidence: result.confidence,
    reason: result.reason,
    error: result.error,
    duration_seconds: result.duration_seconds,
    usage: result.usage,
  }));
}

function formatUsage(usage) {
  if (!usage) return "";
  const parts = [];
  if (usage.total_tokens) parts.push(`${usage.total_tokens} tok`);
  else if (usage.prompt_tokens || usage.completion_tokens) {
    parts.push(`${usage.prompt_tokens || 0}+${usage.completion_tokens || 0} tok`);
  }
  if (usage.cost_usd != null) parts.push(`$${Number(usage.cost_usd).toFixed(4)}`);
  return parts.join(" · ");
}

function formatDuration(seconds) {
  if (seconds == null) return "";
  return `${Number(seconds).toFixed(1)}s`;
}

function resetTracePanel() {
  lastTraceData = null;
  liveTracePlan = null;
  $("routeList").innerHTML = "";
  $("traceSummary").textContent = "No run yet. Send a message to see planning, routes, and errors.";
  const usageBox = $("traceUsage");
  if (usageBox) {
    usageBox.hidden = true;
    usageBox.textContent = "";
  }
  $("traceJson").textContent = "{}";
  setPill("traceState", "idle", "muted");
  document.querySelector('.tool-btn[data-view="trace"]')?.classList.remove("notify");
}

function normalizeTraceData(data) {
  const trace = data?.trace || {};
  const routes = Array.isArray(data?.routes) && data.routes.length
    ? data.routes
    : traceToRoutes(trace);
  return {
    trace,
    routes,
    usage: data?.usage || trace?.metadata?.usage || null,
  };
}

function buildRouteItem(route) {
  const item = document.createElement("div");
  item.className = "route-item";
  if (route.id) item.dataset.routeId = route.id;
  const routeName = route.route || "pending";
  item.innerHTML = `
    <div class="route-head">
      <div class="route-title"></div>
      <span class="route-badge ${routeName}">${routeName}</span>
    </div>
    <div class="route-meta"></div>
    <div class="route-reason"></div>
  `;
  const metaParts = [];
  if (route.model) metaParts.push(route.model);
  if (route.duration_seconds != null) metaParts.push(formatDuration(route.duration_seconds));
  const usageText = formatUsage(route.usage);
  if (usageText) metaParts.push(usageText);
  if (route.confidence != null) metaParts.push(`confidence ${Number(route.confidence).toFixed(2)}`);
  item.querySelector(".route-meta").textContent = metaParts.join(" · ");
  item.querySelector(".route-title").textContent = route.title || route.id || "subtask";
  item.querySelector(".route-reason").textContent = route.error
    ? `Error: ${route.error}`
    : (route.reason || (routeName === "pending" ? "Waiting…" : ""));
  return item;
}

function paintTrace(data) {
  const normalized = normalizeTraceData(data);
  const trace = normalized.trace;
  const plan = trace.plan || liveTracePlan || {};
  $("traceSummary").textContent = plan.summary || normalized.routes.length
    ? `${normalized.routes.length} routed subtask${normalized.routes.length === 1 ? "" : "s"}`
    : "No plan summary returned.";
  const usageBox = $("traceUsage");
  const usage = normalized.usage;
  if (usageBox) {
    if (usage) {
      const parts = [
        `${usage.total_tokens || 0} tokens`,
        `${usage.prompt_tokens || 0} prompt`,
        `${usage.completion_tokens || 0} completion`,
      ];
      if (usage.cost_usd != null) parts.push(`$${Number(usage.cost_usd).toFixed(4)} estimated`);
      usageBox.hidden = false;
      usageBox.textContent = `Run usage: ${parts.join(" · ")}`;
    } else {
      usageBox.hidden = true;
      usageBox.textContent = "";
    }
  }
  const list = $("routeList");
  list.innerHTML = "";
  for (const route of normalized.routes) list.appendChild(buildRouteItem(route));
  $("traceJson").textContent = JSON.stringify(trace, null, 2);
}

function beginLiveTrace() {
  liveTracePlan = null;
  lastTraceData = { trace: {}, routes: [] };
  $("routeList").innerHTML = "";
  $("traceSummary").textContent = "Planning request…";
  $("traceJson").textContent = "{}";
  const usageBox = $("traceUsage");
  if (usageBox) {
    usageBox.hidden = true;
    usageBox.textContent = "";
  }
  setPill("traceState", "running", "warn");
}

function updateLiveTrace(event) {
  if (event.type === "planned") {
    liveTracePlan = { summary: `Running ${(event.subtasks || []).length} subtasks` };
    $("traceSummary").textContent = liveTracePlan.summary;
    const list = $("routeList");
    list.innerHTML = "";
    for (const task of event.subtasks || []) {
      list.appendChild(buildRouteItem({
        id: task.id,
        title: task.title,
        route: "pending",
        reason: "Queued",
      }));
    }
    lastTraceData = { trace: { plan: liveTracePlan }, routes: traceToRoutes({ results: [] }) };
    return;
  }
  if (event.type === "subtask_start") {
    const item = $("routeList").querySelector(`[data-route-id="${event.id}"]`);
    if (item) {
      item.querySelector(".route-badge").className = `route-badge ${event.route || "local"}`;
      item.querySelector(".route-badge").textContent = event.route || "running";
      item.querySelector(".route-reason").textContent = "Running…";
    }
    return;
  }
  if (event.type === "subtask_done" || event.type === "routed") {
    const route = {
      id: event.id,
      title: event.title,
      route: event.route,
      model: event.model,
      confidence: event.confidence,
      reason: event.reason,
      error: event.error,
      duration_seconds: event.duration_seconds,
      usage: event.usage,
    };
    const item = $("routeList").querySelector(`[data-route-id="${event.id}"]`);
    if (item) item.replaceWith(buildRouteItem(route));
    else $("routeList").appendChild(buildRouteItem(route));
    if (lastTraceData) {
      const existing = (lastTraceData.routes || []).filter((r) => r.id !== event.id);
      lastTraceData.routes = [...existing, route];
    }
  }
}

function resetChatView() {
  currentSessionId = null;
  chatHistory = [];
  clearAttachments();
  renderWelcome();
  resetTracePanel();
  closeDrawers();
}

async function beginFreshChat({ deactivate = true } = {}) {
  resetChatView();
  if (deactivate) {
    try {
      await api("/api/history/deactivate", { method: "POST", body: "{}" });
    } catch {
      // Older servers may not expose deactivate; the UI still starts fresh.
    }
  }
  await loadHistoryList().catch(() => {});
  $("messageInput")?.focus();
}

async function startNewChat() {
  await beginFreshChat({ deactivate: true });
}

function hidePlanPreview() {
  clearTimeout(showPlanCancelled._timer);
  const panel = $("planPreview");
  panel.hidden = true;
  panel.innerHTML = "";
}

function showPlanCancelled() {
  const panel = $("planPreview");
  panel.hidden = false;
  panel.innerHTML = `
    <div class="plan-preview-card plan-cancelled">
      <div class="plan-preview-head">
        <div>
          <h3>Cancelled</h3>
          <p class="plan-summary">The plan was not run. Edit your message and send again when you are ready.</p>
        </div>
        <span class="pill bad">cancelled</span>
      </div>
    </div>
  `;
  scrollChat();
  toast("Plan cancelled.");
  clearTimeout(showPlanCancelled._timer);
  showPlanCancelled._timer = setTimeout(() => hidePlanPreview(), 3000);
}

function showPlanningWait() {
  const panel = $("planPreview");
  panel.hidden = false;
  panel.innerHTML = `
    <div class="plan-preview-card planning-wait">
      <div class="plan-preview-head">
        <div>
          <h3>Planning</h3>
          <p class="plan-summary">Breaking your request into subtasks…</p>
        </div>
        <span class="pill warn">working</span>
      </div>
      <div class="status-phase"><span class="spinner"></span><span class="phase-label">Analyzing request</span></div>
    </div>
  `;
  scrollChat();
}

function cloudModelList(config) {
  if (!config?.cloud_enabled) return [];
  if (config.cloud_provider !== "openrouter") return [config.openai_compat_model].filter(Boolean);
  const distinct = new Set(Object.values(config.cloud_model_map || {}));
  if (config.openrouter_model) distinct.add(config.openrouter_model);
  return [...distinct];
}

function buildModelSelectOptions(models, selected, autoLabel) {
  const local = models?.local || [];
  const cloud = models?.cloud || [];
  const auto = autoLabel || "Auto (recommended)";
  let html = `<option value="">${escapeHtml(auto)}</option>`;
  if (cloud.length) {
    html += '<optgroup label="Cloud">';
    for (const name of cloud) {
      html += `<option value="${escapeHtml(name)}"${name === selected ? " selected" : ""}>${escapeHtml(name)}</option>`;
    }
    html += "</optgroup>";
  }
  if (local.length) {
    html += '<optgroup label="Local">';
    for (const name of local) {
      html += `<option value="${escapeHtml(name)}"${name === selected ? " selected" : ""}>${escapeHtml(name)}</option>`;
    }
    html += "</optgroup>";
  }
  return html;
}

function buildPlanPreviewRow(preview, models) {
  const row = document.createElement("div");
  row.className = "plan-preview-item";
  row.dataset.taskId = preview.id;
  row.dataset.capabilities = JSON.stringify(preview.capabilities || ["general"]);
  row.dataset.sensitivity = preview.sensitivity || "medium";
  row.dataset.dependsOn = JSON.stringify(preview.depends_on || []);
  const selectedModel = preview.model_override || "";
  const autoLabel = preview.predicted_model
    ? `Auto (${preview.predicted_model})`
    : "Auto (recommended)";
  row.innerHTML = `
    <div class="plan-preview-item-head">
      <input class="plan-edit-title" />
      <span class="route-badge ${preview.predicted_route}"></span>
    </div>
    <textarea class="plan-edit-prompt" rows="3" spellcheck="false"></textarea>
    <div class="plan-preview-controls">
      <label class="plan-model-pick">
        <span>Model</span>
        <select class="plan-edit-model"></select>
      </label>
      <span class="plan-preview-reason"></span>
      <span class="plan-preview-caps"></span>
    </div>
    <div class="plan-item-actions">
      <button type="button" class="ghost plan-remove">Remove</button>
    </div>
  `;
  row.querySelector(".plan-edit-title").value = preview.title || preview.id;
  row.querySelector(".route-badge").textContent = preview.predicted_route || "auto";
  row.querySelector(".plan-edit-prompt").value = preview.prompt || "";
  row.querySelector(".plan-edit-model").innerHTML = buildModelSelectOptions(models, selectedModel, autoLabel);
  row.querySelector(".plan-preview-reason").textContent = preview.reason || "Auto-selected route.";
  row.querySelector(".plan-preview-caps").textContent = preview.capabilities?.length
    ? `Capabilities: ${preview.capabilities.join(", ")}`
    : "";
  return row;
}

function attachmentPlanNote(attachments) {
  if (!attachments?.length) return "";
  const images = attachments.filter((item) => item.kind === "image").length;
  const texts = attachments.filter((item) => item.kind === "text").length;
  const parts = [];
  if (images) parts.push(`${images} image${images === 1 ? "" : "s"} (vision model)`);
  if (texts) parts.push(`${texts} text file${texts === 1 ? "" : "s"}`);
  return parts.join(" · ");
}

function showPlanPreview(planData) {
  const panel = $("planPreview");
  const plan = planData.plan || {};
  const previews = planData.previews || [];
  const attachmentNote = attachmentPlanNote(planData.attachments);
  const subtaskLabel = `${previews.length} subtask${previews.length === 1 ? "" : "s"}`;
  panel.hidden = false;
  panel.innerHTML = `
    <div class="plan-preview-card">
      <div class="plan-preview-head">
        <div>
          <h3>Review plan</h3>
          <p class="hint">${subtaskLabel} · Edit below, then run. Independent subtasks execute in parallel.${attachmentNote ? ` Attachments: ${attachmentNote}.` : ""}</p>
        </div>
        <span class="pill warn">awaiting approval</span>
      </div>
      <textarea class="plan-edit-summary" rows="2" spellcheck="false"></textarea>
      <div class="plan-preview-toolbar">
        <button type="button" class="ghost" data-plan-add>Add subtask</button>
        <button type="button" class="ghost" data-plan-refresh>Refresh routes</button>
      </div>
      <div class="plan-preview-list"></div>
      <div class="plan-preview-actions">
        <button type="button" class="ghost" data-plan-cancel>Cancel</button>
        <button type="button" class="primary" data-plan-run>Run plan</button>
      </div>
    </div>
  `;
  panel.querySelector(".plan-edit-summary").value = plan.summary || "Planned subtasks";
  const list = panel.querySelector(".plan-preview-list");
  const models = planData.models || { local: [], cloud: [] };
  for (const preview of previews) list.appendChild(buildPlanPreviewRow(preview, models));
  panel.scrollIntoView({ block: "nearest", behavior: "smooth" });
}

function collectPlanFromPreview(planData) {
  const panel = $("planPreview");
  const summary = panel.querySelector(".plan-edit-summary")?.value?.trim() || planData.plan?.summary || "";
  const subtasks = [];
  panel.querySelectorAll(".plan-preview-item").forEach((row, index) => {
    const title = row.querySelector(".plan-edit-title")?.value?.trim() || `Task ${index + 1}`;
    const prompt = row.querySelector(".plan-edit-prompt")?.value?.trim() || "";
    if (!prompt) return;
    let capabilities = ["general"];
    let depends_on = [];
    try {
      capabilities = JSON.parse(row.dataset.capabilities || "[]");
      if (!Array.isArray(capabilities) || !capabilities.length) capabilities = ["general"];
    } catch (_err) {
      capabilities = ["general"];
    }
    try {
      depends_on = JSON.parse(row.dataset.dependsOn || "[]");
      if (!Array.isArray(depends_on)) depends_on = [];
    } catch (_err) {
      depends_on = [];
    }
    const modelOverride = row.querySelector(".plan-edit-model")?.value?.trim() || null;
    subtasks.push({
      id: row.dataset.taskId || `task_${index + 1}`,
      title,
      prompt,
      preferred_route: "auto",
      capabilities,
      depends_on,
      sensitivity: row.dataset.sensitivity || "medium",
      rationale: "Edited in plan preview.",
      model_override: modelOverride,
    });
  });
  return {
    summary,
    requires_online: Boolean(planData.plan?.requires_online),
    final_response_instructions: planData.plan?.final_response_instructions,
    subtasks,
  };
}

async function refreshPlanRoutes(planData) {
  const plan = collectPlanFromPreview(planData);
  if (!plan.subtasks.length) {
    toast("Add at least one subtask with a prompt.", "error");
    return planData;
  }
  const data = await api("/api/chat/preview-routes", {
    method: "POST",
    body: JSON.stringify({ plan, message: planData.message || "" }),
  });
  return { ...planData, plan: data.plan, previews: data.previews };
}

function waitForPlanApproval(planData) {
  showPlanPreview(planData);
  return new Promise((resolve) => {
    const panel = $("planPreview");
    let liveData = planData;

    const onCancel = () => {
      cleanup();
      showPlanCancelled();
      resolve(null);
    };
    const onRun = () => {
      const plan = collectPlanFromPreview(liveData);
      if (!plan.subtasks.length) {
        toast("Add at least one subtask with a prompt.", "error");
        return;
      }
      cleanup();
      hidePlanPreview();
      resolve(plan);
    };
    const onAdd = () => {
      const list = panel.querySelector(".plan-preview-list");
      const nextId = `task_${list.children.length + 1}`;
      list.appendChild(
        buildPlanPreviewRow(
          {
            id: nextId,
            title: `Subtask ${list.children.length + 1}`,
            prompt: "",
            predicted_route: "local",
            capabilities: ["general"],
            sensitivity: "medium",
            reason: "New subtask — refresh routes after editing.",
          },
          liveData.models || { local: [], cloud: [] }
        )
      );
    };
    const onRemove = (event) => {
      const button = event.target.closest(".plan-remove");
      if (!button) return;
      const row = button.closest(".plan-preview-item");
      if (row) row.remove();
    };
    const onRefresh = async () => {
      try {
        liveData = await refreshPlanRoutes(liveData);
        showPlanPreview(liveData);
        wirePlanPreviewHandlers();
      } catch (err) {
        toast(err.message, "error");
      }
    };
    const cleanup = () => {
      panel.querySelector("[data-plan-cancel]")?.removeEventListener("click", onCancel);
      panel.querySelector("[data-plan-run]")?.removeEventListener("click", onRun);
      panel.querySelector("[data-plan-add]")?.removeEventListener("click", onAdd);
      panel.querySelector("[data-plan-refresh]")?.removeEventListener("click", onRefresh);
      panel.removeEventListener("click", onRemove);
    };
    const wirePlanPreviewHandlers = () => {
      cleanup();
      panel.querySelector("[data-plan-cancel]")?.addEventListener("click", onCancel);
      panel.querySelector("[data-plan-run]")?.addEventListener("click", onRun);
      panel.querySelector("[data-plan-add]")?.addEventListener("click", onAdd);
      panel.querySelector("[data-plan-refresh]")?.addEventListener("click", onRefresh);
      panel.addEventListener("click", onRemove);
    };
    wirePlanPreviewHandlers();
  });
}

function makeAvatar(role) {
  const avatar = document.createElement("div");
  avatar.className = `avatar ${role === "user" ? "user" : "relay"}`;
  if (role === "user") {
    avatar.textContent = "U";
  } else {
    const img = document.createElement("img");
    img.src = "/assets/logo-icon.png";
    img.alt = "";
    avatar.appendChild(img);
  }
  return avatar;
}

function clearSubtaskProgress(ui) {
  const progress = ui.status?.querySelector(".subtask-progress");
  if (progress) progress.remove();
}

function finalizeStatus(ui) {
  clearSubtaskProgress(ui);
  const phase = ui.status?.querySelector(".status-phase");
  if (phase) phase.remove();
  if (ui.status && ui.status.childElementCount === 0) ui.status.remove();
}

const EXAMPLE_PROMPTS = [
  "Summarize the architecture of Relay, a local-first model router.",
  "Write a Python function that parses a CSV and returns the column with the highest sum.",
  "Research the latest cloud GPU rental prices and estimate the monthly cost at 8h/day.",
];

function renderWelcome() {
  const log = chatMount();
  log.innerHTML = "";
  const hero = document.createElement("div");
  hero.className = "welcome";
  hero.innerHTML = `
    <div class="welcome-hero">
      <img class="welcome-logo" src="/assets/logo.png" alt="Relay — Local &amp; Cloud Orchestration" width="460" height="110" />
      <p class="welcome-desc">Plan on your machine, route each subtask locally or to the cloud, then synthesize one answer.</p>
      <ol class="welcome-flow" aria-label="How Relay works">
        <li class="flow-step"><span class="flow-num">1</span><span class="flow-label">Plan</span></li>
        <li class="flow-arrow" aria-hidden="true"></li>
        <li class="flow-step"><span class="flow-num">2</span><span class="flow-label">Route</span></li>
        <li class="flow-arrow" aria-hidden="true"></li>
        <li class="flow-step"><span class="flow-num">3</span><span class="flow-label">Execute</span></li>
        <li class="flow-arrow" aria-hidden="true"></li>
        <li class="flow-step"><span class="flow-num">4</span><span class="flow-label">Synthesize</span></li>
      </ol>
      <p class="welcome-examples-label">Try an example</p>
      <div class="examples"></div>
    </div>
  `;
  const examples = hero.querySelector(".examples");
  for (const prompt of EXAMPLE_PROMPTS) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "example";
    btn.textContent = prompt;
    examples.appendChild(btn);
  }
  log.appendChild(hero);
}

function renderTrace(data) {
  lastTraceData = normalizeTraceData(data);
  liveTracePlan = null;
  paintTrace(lastTraceData);
  const traceBtn = document.querySelector('.tool-btn[data-view="trace"]');
  if (traceBtn && !$("drawer-trace").classList.contains("open")) traceBtn.classList.add("notify");
  const routeCount = lastTraceData.routes?.length || 0;
  setPill("traceState", routeCount ? `${routeCount} routes` : "done", routeCount ? "good" : "muted");
}

function makeThinkingBubble() {
  const log = chatMount();
  const article = document.createElement("article");
  article.className = "msg assistant";
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  const meta = document.createElement("div");
  meta.className = "meta";
  meta.textContent = "Relay";
  const status = document.createElement("div");
  status.className = "status";
  status.innerHTML = `
    <div class="status-phase">
      <span class="spinner"></span>
      <span class="phase-label"></span>
    </div>
    <div class="subtask-progress"></div>
  `;
  bubble.append(meta, status);
  article.append(makeAvatar("assistant"), bubble);
  log.appendChild(article);
  scrollChat();
  const ui = { article, bubble, status, subtaskCards: {} };
  setPhase(ui, "Planning");
  return ui;
}

function initSubtaskProgress(ui, subtasks) {
  const wrap = ui.status.querySelector(".subtask-progress");
  if (!wrap || !subtasks?.length) return;
  wrap.innerHTML = "";
  ui.subtaskCards = {};
  for (const task of subtasks) {
    const card = document.createElement("div");
    card.className = "subtask-card queued";
    card.dataset.taskId = task.id;
    card.innerHTML = `
      <div class="subtask-card-head">
        <span class="subtask-card-title"></span>
        <span class="subtask-card-status">queued</span>
      </div>
      <div class="subtask-card-meta"></div>
    `;
    card.querySelector(".subtask-card-title").textContent = task.title || task.id;
    wrap.appendChild(card);
    ui.subtaskCards[task.id] = card;
  }
}

function updateSubtaskCard(ui, event) {
  const card = ui.subtaskCards?.[event.id] || ui.status.querySelector(`.subtask-card[data-task-id="${event.id}"]`);
  if (!card) return;
  if (!ui.subtaskCards) ui.subtaskCards = {};
  ui.subtaskCards[event.id] = card;

  if (event.type === "subtask_start") {
    card.classList.remove("queued", "done", "error");
    card.classList.add("running");
    card.querySelector(".subtask-card-status").textContent = "running";
    return;
  }

  const done = event.type === "subtask_done" || event.type === "routed";
  if (!done) return;

  card.classList.remove("queued", "running");
  card.classList.add(event.error ? "error" : "done");
  card.querySelector(".subtask-card-status").textContent = event.error ? "failed" : "done";

  const metaParts = [];
  if (event.route) metaParts.push(event.route);
  if (event.model) metaParts.push(event.model);
  if (event.duration_seconds != null) metaParts.push(formatDuration(event.duration_seconds));
  const usageText = formatUsage(event.usage);
  if (usageText) metaParts.push(usageText);
  card.querySelector(".subtask-card-meta").textContent = metaParts.join(" · ");
  if (event.error) {
    card.title = event.error;
  }
  scrollChat();
}

function setPhase(ui, label) {
  const el = ui.status.querySelector(".phase-label");
  el.textContent = "";
  el.append(document.createTextNode(label));
  const ellipsis = document.createElement("span");
  ellipsis.className = "ellipsis";
  el.appendChild(ellipsis);
}

function addRouteChip(ui, event) {
  const wrap = ui.status.querySelector(".status-routes");
  const chip = document.createElement("div");
  chip.className = `route-chip appear ${event.error ? "error" : event.route}`;
  const badge = document.createElement("span");
  badge.className = `route-badge ${event.route}`;
  badge.textContent = event.route;
  const text = document.createElement("span");
  text.className = "route-chip-text";
  text.textContent = event.title + (event.model ? ` · ${event.model}` : "");
  chip.append(badge, text);
  wrap.appendChild(chip);
  scrollChat();
}

function handleStreamEvent(event, ui) {
  updateLiveTrace(event);
  if (event.type === "planning") setPhase(ui, "Planning");
  else if (event.type === "planned") {
    const n = (event.subtasks || []).length;
    setPhase(ui, `Running ${n} subtask${n === 1 ? "" : "s"}`);
    initSubtaskProgress(ui, event.subtasks || []);
  } else if (event.type === "subtask_start" || event.type === "subtask_done" || event.type === "routed") {
    updateSubtaskCard(ui, event);
  } else if (event.type === "synthesizing") {
    clearSubtaskProgress(ui);
    setPhase(ui, "Synthesizing");
  } else if (event.type === "answering") {
    clearSubtaskProgress(ui);
    setPhase(ui, "Answering");
  } else if (event.type === "token") appendToken(ui, event.text || "");
}

function appendToken(ui, text) {
  if (!ui.answerEl) {
    finalizeStatus(ui);
    ui.answerEl = document.createElement("div");
    ui.answerEl.className = "answer";
    ui.answerRaw = "";
    ui.bubble.appendChild(ui.answerEl);
    ui.article.classList.add("has-answer");
  }
  ui.answerRaw += text;
  ui.answerEl.innerHTML = renderMarkdown(ui.answerRaw);
  scrollChat();
}

function finishThinking(ui, text, isError = false) {
  finalizeStatus(ui);
  if (ui.answerEl && !isError) {
    // Tokens already streamed in; reconcile with the authoritative final text.
    ui.answerRaw = text || ui.answerRaw || "";
    ui.answerEl.innerHTML = renderMarkdown(ui.answerRaw);
    addAnswerToolbar(ui.bubble);
  } else {
    if (ui.status) ui.status.remove();
    if (isError) {
      // Replace any partially-streamed answer with the error, rather than stacking
      // an "Error: …" below a half-finished response.
      ui.answerEl?.remove();
      ui.answerEl = null;
      ui.bubble.querySelector(".answer-toolbar")?.remove();
    }
    const div = document.createElement("div");
    div.className = isError ? "answer error-text" : "answer";
    if (isError) div.textContent = text;
    else div.innerHTML = renderMarkdown(text);
    ui.bubble.appendChild(div);
    ui.article.classList.add("has-answer");
    if (!isError) addAnswerToolbar(ui.bubble);
  }
  scrollChat();
}

function escapeHtml(s) {
  return s.replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
}

function escapeAttr(s) {
  return String(s).replace(/&/g, "&amp;").replace(/"/g, "&quot;");
}

function copyToClipboard(text) {
  if (navigator.clipboard?.writeText) return navigator.clipboard.writeText(text);
  // Fallback for non-secure contexts (e.g. http://127.0.0.1 without clipboard API).
  return new Promise((resolve, reject) => {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.top = "-9999px";
    document.body.appendChild(ta);
    ta.select();
    try {
      document.execCommand("copy") ? resolve() : reject(new Error("copy rejected"));
    } catch (err) {
      reject(err);
    } finally {
      ta.remove();
    }
  });
}

// Copies `text`, then flashes the button to "Copied" and back.
function flashCopy(button, text) {
  copyToClipboard(text)
    .then(() => {
      if (!button._copyLabel) button._copyLabel = button.textContent;
      button.textContent = "Copied";
      button.classList.add("copied");
      clearTimeout(button._copyTimer);
      button._copyTimer = setTimeout(() => {
        button.textContent = button._copyLabel || "Copy";
        button.classList.remove("copied");
      }, 1600);
    })
    .catch(() => toast("Could not copy to clipboard.", "error"));
}

// Adds a "Copy answer" toolbar under a finished assistant bubble (once).
function addAnswerToolbar(bubble) {
  if (!bubble || bubble.querySelector(".answer-toolbar")) return;
  const bar = document.createElement("div");
  bar.className = "answer-toolbar";
  const copyBtn = document.createElement("button");
  copyBtn.type = "button";
  copyBtn.className = "answer-copy";
  copyBtn.dataset.copyAnswer = "";
  copyBtn.textContent = "Copy answer";
  bar.appendChild(copyBtn);
  bubble.appendChild(bar);
}

// Minimal, dependency-free, XSS-safe markdown -> HTML. All text is HTML-escaped
// before any formatting, and the only links allowed are http(s). Tolerates the
// partial/unterminated markdown that arrives mid-stream.
function renderMarkdown(src) {
  const blocks = [];
  const stash = (html) => `@@B${blocks.push(html) - 1}@@`;
  // A fenced block becomes a titled card: the fence "info string" (language and/or
  // filename, e.g. ```js or ```python db.js) is shown as a header with a Copy
  // button so generated files are clearly labelled and one-click copyable.
  const codeCard = (info, code) => {
    const label = (info || "").trim() || "code";
    return stash(
      `<div class="md-codeblock">` +
        `<div class="md-codeblock-head">` +
          `<span class="md-codeblock-lang">${escapeHtml(label)}</span>` +
          `<button type="button" class="md-copy" data-copy-code>Copy</button>` +
        `</div>` +
        `<pre class="md-code"><code>${escapeHtml(code)}</code></pre>` +
      `</div>`
    );
  };
  // Fenced code blocks first, including an unterminated one while streaming.
  // Capture the whole info string (not just \w*) so filenames survive.
  src = src.replace(/```([^\n]*)\n?([\s\S]*?)```/g, (_m, info, code) =>
    codeCard(info, code.replace(/\n$/, ""))
  );
  src = src.replace(/```([^\n]*)\n?([\s\S]*)$/g, (_m, info, code) =>
    codeCard(info, code)
  );

  src = escapeHtml(src);
  // Models often emit literal <br> in table cells; restore just that one safe tag.
  src = src.replace(/&lt;br\s*\/?&gt;/gi, "<br>");
  src = src.replace(/`([^`]+)`/g, (_m, c) => `<code class="md-inline">${c}</code>`);
  src = src.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  src = src.replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
  src = src.replace(
    /\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
    '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>'
  );

  let html = "";
  let listType = null;
  let para = [];
  const flushPara = () => { if (para.length) { html += `<p>${para.join("<br>")}</p>`; para = []; } };
  const closeList = () => { if (listType) { html += `</${listType}>`; listType = null; } };
  const flush = () => { flushPara(); closeList(); };

  const splitRow = (row) => row.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
  const isSeparator = (row) => /\|/.test(row) && splitRow(row).every((c) => /^:?-+:?$/.test(c));

  const lines = src.split("\n");
  for (let idx = 0; idx < lines.length; idx++) {
    const line = lines[idx];
    const t = line.trim();

    // GFM table: a header row followed by a |---|---| separator row.
    if (t.includes("|") && idx + 1 < lines.length && isSeparator(lines[idx + 1])) {
      flush();
      const headers = splitRow(line);
      const aligns = splitRow(lines[idx + 1]).map((c) => {
        const l = c.startsWith(":"), r = c.endsWith(":");
        return l && r ? "center" : r ? "right" : l ? "left" : "";
      });
      const cellAttr = (i) => (aligns[i] ? ` style="text-align:${aligns[i]}"` : "");
      let table = '<table class="md-table"><thead><tr>';
      headers.forEach((c, i) => { table += `<th${cellAttr(i)}>${c}</th>`; });
      table += "</tr></thead><tbody>";
      let j = idx + 2;
      for (; j < lines.length && lines[j].includes("|") && lines[j].trim() !== ""; j++) {
        const cells = splitRow(lines[j]);
        table += "<tr>";
        for (let i = 0; i < headers.length; i++) table += `<td${cellAttr(i)}>${cells[i] ?? ""}</td>`;
        table += "</tr>";
      }
      table += "</tbody></table>";
      html += table;
      idx = j - 1;
      continue;
    }

    if (/^@@B\d+@@$/.test(t)) { flush(); html += t; continue; }
    const h = line.match(/^(#{1,6})\s+(.*)$/);
    if (h) { flush(); html += `<h${h[1].length} class="md-h">${h[2]}</h${h[1].length}>`; continue; }
    const ul = line.match(/^\s*[-*+]\s+(.*)$/);
    const ol = line.match(/^\s*\d+\.\s+(.*)$/);
    if (ul) { flushPara(); if (listType !== "ul") { closeList(); html += "<ul>"; listType = "ul"; } html += `<li>${ul[1]}</li>`; continue; }
    if (ol) { flushPara(); if (listType !== "ol") { closeList(); html += "<ol>"; listType = "ol"; } html += `<li>${ol[1]}</li>`; continue; }
    const bq = line.match(/^>\s?(.*)$/);
    if (bq) { flush(); html += `<blockquote>${bq[1]}</blockquote>`; continue; }
    if (t === "") { flush(); continue; }
    closeList();
    para.push(line.trim());
  }
  flush();

  return html.replace(/@@B(\d+)@@/g, (_m, i) => blocks[Number(i)]);
}

async function streamChat(message, ui, plan = null, attachments = null, history = null) {
  const payload = { message, plan };
  if (attachments?.length) payload.attachments = attachments;
  if (history?.length) payload.history = history;
  const res = await fetch("/api/chat/stream", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!res.ok || !res.body) throw new Error(`${res.status} ${res.statusText}`);
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let finalData = null;
  let streamDone = false;
  // Stop as soon as the "done" event arrives. The server may hold the SSE
  // connection open (keep-alive) after the final event; if we waited for the
  // socket to close, this read loop would hang forever and the code after
  // streamChat (rendering the final answer, persisting it to history) would
  // never run — which is why completed chats were not being saved.
  while (!streamDone) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const frames = buffer.split("\n\n");
    buffer = frames.pop();
    for (const frame of frames) {
      const line = frame.split("\n").find((l) => l.startsWith("data:"));
      if (!line) continue;
      const event = JSON.parse(line.slice(5).trim());
      if (event.type === "error") throw new Error(event.error?.message || "stream error");
      if (event.type === "done") { finalData = event; streamDone = true; }
      else handleStreamEvent(event, ui);
    }
  }
  try { await reader.cancel(); } catch { /* reader already closed */ }
  return finalData;
}

async function sendMessage() {
  if (busy) return;
  const input = $("messageInput");
  const message = input.value.trim();
  const attachments = serializeAttachments();
  if (!message && !attachments.length) return;
  const sentAttachments = [...pendingAttachments];
  input.value = "";
  clearAttachments();
  resetComposer();
  const welcome = chatMount().querySelector(".welcome");
  if (welcome) welcome.remove();
  const priorHistory = conversationPayload();
  const displayText = message || (sentAttachments.length ? `[${sentAttachments.length} attached file(s)]` : "");
  appendMessage("user", displayText, { attachments: sentAttachments });
  setBusy(true);
  let ui = null;
  try {
    const historyText = displayText;
    // Pin this exchange to the session it starts in, so the answer is saved here
    // even if the user opens a different chat while the response is streaming.
    const sessionId = await persistMessage("user", historyText);
    let plan = null;
    if (currentConfig?.planner_preview_enabled !== false) {
      setPill("traceState", "planning", "warn");
      showPlanningWait();
      const planPayload = { message: message || "Review the attached files and answer the user's request." };
      if (attachments.length) planPayload.attachments = attachments;
      if (priorHistory.length) planPayload.history = priorHistory;
      let planData = await api("/api/chat/plan", { method: "POST", body: JSON.stringify(planPayload) });
      if (!planData.models) {
        planData.models = {
          local: [currentConfig?.ollama_model].filter(Boolean),
          cloud: cloudModelList(currentConfig),
        };
      }
      hidePlanPreview();
      setBusy(false);
      $("messageInput").disabled = true;
      plan = await waitForPlanApproval(planData);
      $("messageInput").disabled = false;
      if (!plan) {
        setPill("traceState", "cancelled", "bad");
        $("traceSummary").textContent = "Plan cancelled. No subtasks were run.";
        $("traceJson").textContent = "{}";
        input.focus();
        return;
      }
      setBusy(true);
    }
    ui = makeThinkingBubble();
    beginLiveTrace();
    const runMessage = message || "Review the attached files and answer the user's request.";
    const data = await streamChat(runMessage, ui, plan, attachments, priorHistory);
    const answer = (data && data.answer) || "(empty response)";
    finishThinking(ui, answer);
    if (data) {
      // Save the answer first, so a later UI hiccup (e.g. trace rendering) can
      // never skip persistence. Surface a failure instead of letting it vanish.
      try {
        await persistMessage("assistant", answer, data.trace || null, sessionId);
        await loadHistoryList();
      } catch (persistErr) {
        toast(`Answer shown but not saved to history: ${persistErr.message}`, "error");
      }
      renderTrace(data);
    }
  } catch (err) {
    if (ui) finishThinking(ui, `Error: ${err.message}`, true);
    else appendMessage("assistant", `Error: ${err.message}`);
    $("traceSummary").textContent = `Error: ${err.message}`;
    setPill("traceState", "error", "bad");
    document.querySelector('.tool-btn[data-view="trace"]')?.classList.add("notify");
    toast(err.message, "error");
  } finally {
    setBusy(false);
    input.focus();
  }
}

function markDirty() {
  setPill("saveState", "unsaved", "warn");
}

function wireEvents() {
  document.querySelector(".brand")?.addEventListener("click", (e) => {
    e.preventDefault();
    closeDrawers();
    $("messageInput")?.focus();
  });
  document.querySelectorAll(".tool-btn[data-view]").forEach((btn) => {
    btn.addEventListener("click", () => toggleDrawer(btn.dataset.view));
  });
  $("scrim").addEventListener("click", closeDrawers);
  document.querySelectorAll("[data-close]").forEach((b) => b.addEventListener("click", closeDrawers));
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawers(); });
  $("saveSetup").addEventListener("click", saveSetup);
  $("refreshModels").addEventListener("click", refreshModels);
  $("testLocal").addEventListener("click", () => ping("/api/test-local", "Local model"));
  $("testCloud").addEventListener("click", () => ping("/api/test-cloud", "Cloud model"));
  $("cloud_provider").addEventListener("change", updateProviderBlocks);
  $("local_provider")?.addEventListener("change", updateLocalProviderHints);
  $("privacy_mode")?.addEventListener("change", updatePrivacyModeHelp);
  $("newChat").addEventListener("click", () => startNewChat().catch((err) => toast(err.message, "error")));
  $("historyNew").addEventListener("click", () => startNewChat().catch((err) => toast(err.message, "error")));
  // Delegated clicks inside the chat: example prompts and copy buttons.
  chatMount().addEventListener("click", (event) => {
    const codeBtn = event.target.closest("[data-copy-code]");
    if (codeBtn) {
      const code = codeBtn.closest(".md-codeblock")?.querySelector("code");
      if (code) flashCopy(codeBtn, code.textContent);
      return;
    }
    const answerBtn = event.target.closest("[data-copy-answer]");
    if (answerBtn) {
      const answer = answerBtn.closest(".bubble")?.querySelector(".answer");
      if (answer) flashCopy(answerBtn, answer.innerText);
      return;
    }
    const example = event.target.closest(".example");
    if (!example) return;
    const input = $("messageInput");
    input.value = example.textContent.trim();
    resizeComposer();
    input.focus();
    input.setSelectionRange(input.value.length, input.value.length);
  });
  $("chatForm").addEventListener("submit", (event) => {
    event.preventDefault();
    sendMessage();
  });
  $("attachButton")?.addEventListener("click", () => $("fileInput")?.click());
  $("fileInput")?.addEventListener("change", (event) => {
    addAttachmentFiles(event.target.files).catch((err) => toast(err.message, "error"));
    event.target.value = "";
  });
  $("messageInput").addEventListener("input", resizeComposer);
  $("messageInput").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      sendMessage();
    }
  });
  for (const id of fields) {
    const el = $(id);
    if (el) el.addEventListener("input", markDirty);
  }
}

async function bootstrap() {
  wireEvents();
  resizeComposer();
  await loadConfig().catch((err) => toast(err.message, "error"));
  // Always open the main chat surface on load. Past conversations stay in the
  // History drawer; clearing the active pointer keeps them out of the way.
  await beginFreshChat({ deactivate: true });
}

bootstrap();
