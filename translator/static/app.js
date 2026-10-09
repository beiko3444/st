"use strict";

const $ = (id) => document.getElementById(id);
const TYPING_DELAY_MS = 700;
const LANG_STORAGE_KEY = "kr-translator.langs";

const el = {
  source: $("source"),
  style: $("style"),
  live: $("live"),
  counter: $("counter"),
  clear: $("clear"),
  jevChip: $("jev-chip"),
  styleChip: $("style-chip"),
  account: $("account"),
  login: $("login"),
  loginDesc: $("login-desc"),
  loginActions: $("login-actions"),
  loginBrowser: $("login-browser"),
  loginMsg: $("login-msg"),
  deviceBox: $("device-box"),
  deviceUrl: $("device-url"),
  deviceCode: $("device-code"),
  langs: $("langs"),
  langHint: $("lang-hint"),
  outputs: $("outputs"),
  outputTemplate: $("output-template"),
  // Filled per language by buildLanguages().
  checkbox: {},
  panel: {},
  out: {},
  back: {},
  meta: {},
};

const state = {
  controller: null,
  jobId: null,
  lastKey: "",
  typingTimer: null,
  loginPoll: null,
  status: null,
  // Language codes in server order, e.g. ["en", "zh"].
  langs: [],
};

// --------------------------------------------------------------- languages

function selectedLangs() {
  return state.langs.filter((code) => el.checkbox[code].checked);
}

function loadSavedLangs() {
  try {
    const saved = JSON.parse(localStorage.getItem(LANG_STORAGE_KEY) || "null");
    return Array.isArray(saved) ? saved : null;
  } catch (error) {
    return null;
  }
}

function saveLangs() {
  try {
    localStorage.setItem(LANG_STORAGE_KEY, JSON.stringify(selectedLangs()));
  } catch (error) {
    // Storage can be unavailable (private window); the choice then lasts for this visit.
  }
}

function buildLanguages(targets) {
  if (state.langs.length || !targets.length) return;
  const saved = loadSavedLangs();
  const savedValid = saved && saved.some((code) => targets.some((target) => target.code === code));
  for (const target of targets) {
    const code = target.code;
    state.langs.push(code);

    const label = document.createElement("label");
    label.className = "lang-option";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.value = code;
    checkbox.checked = savedValid ? saved.includes(code) : true;
    checkbox.addEventListener("change", () => onLangToggle(checkbox));
    label.append(checkbox, ` ${target.label}`);
    el.langs.appendChild(label);
    el.checkbox[code] = checkbox;

    const panel = el.outputTemplate.content.firstElementChild.cloneNode(true);
    panel.dataset.lang = code;
    panel.querySelector(".pane-title").textContent = target.native;
    panel.querySelector(".copy").dataset.copy = code;
    const output = panel.querySelector(".output");
    output.lang = target.htmlLang;
    el.outputs.appendChild(panel);
    el.panel[code] = panel;
    el.out[code] = output;
    el.back[code] = panel.querySelector(".back-output");
    panel.querySelector(".copy-back").dataset.copyBack = code;
    el.meta[code] = panel.querySelector(".meta");
  }
  applyLangSelection();
}

function applyLangSelection() {
  for (const code of state.langs) el.panel[code].hidden = !el.checkbox[code].checked;
}

function onLangToggle(checkbox) {
  if (!selectedLangs().length) {
    // Translating into nothing is never useful; keep the last language on.
    checkbox.checked = true;
    el.langHint.hidden = false;
    setTimeout(() => {
      el.langHint.hidden = true;
    }, 2000);
    return;
  }
  applyLangSelection();
  saveLangs();
  // Unchecking only hides the panel. Checking fetches the new language;
  // languages already translated come back from the server cache.
  if (checkbox.checked) translate();
}

// ------------------------------------------------------------ translation

function requestKey(text) {
  return `${el.style.value}\u0000${selectedLangs().join(",")}\u0000${text}`;
}

function scheduleTranslate() {
  clearTimeout(state.typingTimer);
  state.typingTimer = setTimeout(() => translate(), TYPING_DELAY_MS);
}

function resetOutputs(streaming) {
  const active = selectedLangs();
  for (const lang of state.langs) {
    const working = streaming && active.includes(lang);
    el.out[lang].textContent = "";
    el.back[lang].textContent = working ? "번역문이 완성되면 한국어로 다시 번역합니다." : "";
    el.back[lang].classList.remove("error");
    el.out[lang].classList.toggle("streaming", working);
    el.out[lang].classList.remove("error");
    el.meta[lang].textContent = working ? "번역 중…" : "";
  }
}

class SessionExpired extends Error {}

// JSON request helper; a 401 means the translator session ran out.
async function api(path, { method = "GET", body, signal } = {}) {
  const response = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
    signal,
  });
  const data = await response.json().catch(() => ({}));
  if (response.status === 401) {
    showSessionExpired(data.portalUrl);
    throw new SessionExpired("세션이 만료되었습니다.");
  }
  if (!response.ok) throw new Error(data.detail || data.error || `서버 오류 (HTTP ${response.status})`);
  return data;
}

function stopJob() {
  if (state.controller) state.controller.abort();
  state.controller = null;
  if (state.jobId) {
    fetch(`/api/jobs/${state.jobId}`, { method: "DELETE" }).catch(() => {});
    state.jobId = null;
  }
}

async function translate(force = false) {
  clearTimeout(state.typingTimer);
  if (!state.langs.length) await statusReady;
  if (!state.langs.length) return;
  const text = el.source.value.trim();
  if (!text) {
    stopJob();
    state.lastKey = "";
    resetOutputs(false);
    el.jevChip.hidden = true;
    el.styleChip.hidden = true;
    return;
  }
  const key = requestKey(text);
  if (!force && key === state.lastKey) return;
  state.lastKey = key;

  // A newer paste replaces the running job: the server cancels the old one,
  // which interrupts its unfinished GPT turns.
  const previousJob = state.jobId;
  state.jobId = null;
  if (state.controller) state.controller.abort();
  const controller = new AbortController();
  state.controller = controller;
  resetOutputs(true);
  el.jevChip.hidden = true;
  el.styleChip.hidden = true;

  try {
    // Not tied to the abort signal: if this request is superseded while
    // starting, the job id is still needed to cancel it.
    const job = await api("/api/jobs", {
      method: "POST",
      body: { text, style: el.style.value, targets: selectedLangs(), replaces: previousJob },
    });
    if (state.controller !== controller) {
      fetch(`/api/jobs/${job.id}`, { method: "DELETE" }).catch(() => {});
      return;
    }
    state.jobId = job.id;
    // Each poll returns as soon as new text exists, so output keeps streaming
    // even through tunnels that cannot carry Server-Sent Events.
    let after = 0;
    for (;;) {
      const page = await api(`/api/jobs/${job.id}?after=${after}`, { signal: controller.signal });
      if (state.controller !== controller) return;
      for (const event of page.events) handleEvent(event);
      after = page.next;
      if (page.done) break;
    }
    if (state.jobId === job.id) state.jobId = null;
  } catch (error) {
    if (error.name === "AbortError" || state.controller !== controller) return;
    const message = error instanceof SessionExpired ? "Beiko 사이트의 '번역기' 메뉴로 다시 들어오세요." : error.message || String(error);
    for (const lang of selectedLangs()) showError(lang, message);
    state.lastKey = "";
  } finally {
    if (state.controller === controller) {
      state.controller = null;
      for (const lang of state.langs) el.out[lang].classList.remove("streaming");
    }
  }
}

function handleEvent(event) {
  const lang = event.lang;
  if (lang && !el.out[lang]) return;
  switch (event.type) {
    case "jev":
      renderJev(event);
      break;
    case "style":
      el.styleChip.textContent = `스타일: ${event.label}`;
      el.styleChip.title = { jev: "Jev가 판단", manual: "직접 선택", default: "기본값" }[event.source] || "";
      el.styleChip.hidden = false;
      break;
    case "reset":
      el.out[lang].textContent = "";
      break;
    case "delta":
      el.out[lang].textContent += event.text;
      el.meta[lang].textContent = "번역 중…";
      break;
    case "translated":
      el.out[lang].textContent = event.text;
      el.out[lang].classList.remove("streaming");
      el.back[lang].textContent = "";
      el.meta[lang].textContent = "한국어 확인 번역 중…";
      break;
    case "back_reset":
      el.back[lang].textContent = "";
      break;
    case "back_delta":
      el.back[lang].textContent += event.text;
      break;
    case "done":
      el.out[lang].textContent = event.text;
      el.out[lang].classList.remove("streaming");
      el.back[lang].textContent = event.backText || event.backError || "";
      el.back[lang].classList.toggle("error", Boolean(event.backError));
      el.meta[lang].textContent = describeTiming(event);
      break;
    case "error":
      showError(lang, event.message);
      break;
    case "fatal":
      for (const code of selectedLangs()) showError(code, event.message);
      break;
    default:
      break;
  }
}

function describeTiming(event) {
  if (event.cached) return "캐시에서 즉시 표시";
  const parts = [];
  if (event.firstTokenMs != null) parts.push(`첫 글자 ${formatMs(event.firstTokenMs)}`);
  parts.push(`완료 ${formatMs(event.ms)}`);
  if (event.model) parts.push(event.model);
  return parts.join(" · ");
}

function formatMs(ms) {
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)}초` : `${ms}ms`;
}

function renderJev(event) {
  const chip = el.jevChip;
  chip.hidden = false;
  if (event.status === "ok") {
    const styles = (state.status && state.status.styles) || [];
    const style = styles.find((item) => item.key === event.label);
    const name = style ? style.label : event.label;
    const confidence = Math.round((event.confidence || 0) * 100);
    chip.textContent = `Jev: ${name} ${confidence}% · ${formatMs(event.latency_ms)}`;
    chip.title = event.applied ? "Jev 판단을 번역 스타일에 적용" : "신뢰도가 낮거나 '기타'라서 기본 스타일 사용";
    if (!event.applied) chip.textContent += " (미적용)";
  } else if (event.status === "skipped") {
    chip.textContent = "Jev 건너뜀";
    chip.title = event.reason || "";
  } else {
    chip.textContent = "Jev 꺼짐";
    chip.title = "TYPESAFE_API_KEY 또는 Cloudflare 설정이 없어 기본 스타일로 번역합니다.";
  }
}

function showError(lang, message) {
  el.out[lang].classList.remove("streaming");
  el.out[lang].classList.add("error");
  el.out[lang].textContent = message;
  el.meta[lang].textContent = "";
  el.back[lang].textContent = "";
  if (/auth|login|401|로그인/i.test(message)) refreshStatus();
}

// ------------------------------------------------------------------- copy

async function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const helper = document.createElement("textarea");
  helper.value = text;
  helper.style.position = "fixed";
  helper.style.opacity = "0";
  document.body.appendChild(helper);
  helper.select();
  document.execCommand("copy");
  helper.remove();
}

el.outputs.addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-copy], button[data-copy-back]");
  if (!button) return;
  const output = button.dataset.copyBack ? el.back[button.dataset.copyBack] : el.out[button.dataset.copy];
  if (!output.textContent || output.classList.contains("error")) return;
  await copyText(output.textContent);
  button.textContent = "복사됨";
  button.classList.add("done");
  setTimeout(() => {
    button.textContent = "복사";
    button.classList.remove("done");
  }, 1200);
});

// ------------------------------------------------------------ account/login

async function refreshStatus() {
  let status;
  try {
    const response = await fetch("/api/status", { cache: "no-store" });
    status = await response.json();
    if (response.status === 401) {
      showSessionExpired(status.portalUrl);
      return null;
    }
  } catch (error) {
    el.account.textContent = "서버에 연결할 수 없습니다";
    return null;
  }
  state.status = status;
  fillStyles(status.styles || []);
  buildLanguages(status.targets || []);
  renderAccount(status);
  return status;
}

function fillStyles(styles) {
  if (el.style.options.length > 1) return;
  for (const style of styles) {
    const option = document.createElement("option");
    option.value = style.key;
    option.textContent = style.label;
    el.style.appendChild(option);
  }
}

function renderAccount(status) {
  el.account.replaceChildren();
  if (status.deployed) {
    el.account.textContent = status.ready ? "바로 번역할 수 있습니다" : "번역 서버 확인이 필요합니다";
    if (status.ready) hideLogin();
    else showLogin("번역 서버를 확인 중입니다. 잠시 후 다시 시도해 주세요.", false);
    return;
  }
  if (!status.codex || !status.codex.ok) {
    el.account.textContent = "Codex 연결 안 됨";
    showLogin((status.codex && status.codex.error) || "Codex CLI를 찾을 수 없습니다.", false);
    return;
  }
  const account = status.account;
  if (!account && status.requiresOpenaiAuth === false) {
    // Codex is configured for a non-OpenAI model provider; nothing to sign in to.
    el.account.textContent = "Codex 설정의 모델 제공자 사용";
    hideLogin();
    return;
  }
  if (!account) {
    el.account.textContent = "로그인 필요";
    showLogin(null, true);
    return;
  }
  hideLogin();
  const label = document.createElement("span");
  if (account.type === "chatgpt") {
    const plan = account.planType ? ` · ${String(account.planType).toUpperCase()}` : "";
    label.textContent = `ChatGPT ${account.email || ""}${plan}`;
    const usage = describeUsage(status.usage);
    if (usage) label.textContent += ` · ${usage}`;
  } else {
    label.className = "warn";
    label.textContent = "API 키로 로그인됨 — 구독이 아닌 API 요금으로 청구됩니다";
  }
  const logout = document.createElement("button");
  logout.type = "button";
  logout.className = "ghost";
  logout.textContent = "로그아웃";
  logout.addEventListener("click", async () => {
    await fetch("/api/logout", { method: "POST" });
    refreshStatus();
  });
  el.account.append(label, logout);
}

function describeUsage(usage) {
  if (!usage || !usage.windows || !usage.windows.length) return "";
  return usage.windows
    .map((window) => {
      const minutes = window.windowMinutes;
      let name = "사용량";
      if (minutes) name = minutes >= 1440 ? `${Math.round(minutes / 1440)}일` : `${Math.round(minutes / 60)}시간`;
      return `${name} ${Math.round(window.usedPercent)}%`;
    })
    .join(" / ");
}

function showLogin(errorMessage, canLogin) {
  const deployed = Boolean(state.status && state.status.deployed);
  el.login.hidden = false;
  el.loginActions.hidden = deployed || !canLogin;
  $("login-title").textContent = deployed ? "번역 서버 확인 중" : "ChatGPT 계정으로 로그인";
  // On the server, the browser flow's callback would go to the server's own
  // 127.0.0.1, so only the device-code flow can work there.
  el.loginBrowser.hidden = deployed;
  el.loginDesc.textContent = deployed
    ? "서버 연결을 확인 중입니다. 잠시 후 다시 시도해 주세요."
    : "번역은 로그인한 ChatGPT 구독(Codex 포함 플랜)의 사용량으로 처리됩니다.";
  if (errorMessage) setLoginMessage(errorMessage, true);
}

function showSessionExpired(portalUrl) {
  stopLoginPoll();
  el.login.hidden = false;
  el.loginActions.hidden = true;
  el.deviceBox.hidden = true;
  el.loginDesc.textContent = "번역기 접속 시간이 만료되었습니다. Beiko 사이트의 '번역기' 메뉴로 다시 들어오세요.";
  el.account.textContent = "세션 만료";
  if (portalUrl) {
    el.loginMsg.hidden = false;
    el.loginMsg.classList.remove("error");
    el.loginMsg.replaceChildren();
    const link = document.createElement("a");
    link.href = portalUrl;
    link.textContent = "번역기 다시 열기";
    el.loginMsg.appendChild(link);
  }
}

function hideLogin() {
  el.login.hidden = true;
  el.deviceBox.hidden = true;
  el.loginMsg.hidden = true;
  stopLoginPoll();
}

function setLoginMessage(text, isError) {
  el.loginMsg.hidden = false;
  el.loginMsg.textContent = text;
  el.loginMsg.classList.toggle("error", Boolean(isError));
}

function stopLoginPoll() {
  clearInterval(state.loginPoll);
  state.loginPoll = null;
}

function pollLogin() {
  stopLoginPoll();
  const started = Date.now();
  state.loginPoll = setInterval(async () => {
    const status = await refreshStatus();
    if (status && status.account) return;
    const login = status && status.login;
    if (login && login.success === false) {
      stopLoginPoll();
      setLoginMessage(`로그인 실패: ${login.error || "알 수 없는 오류"}`, true);
    } else if (Date.now() - started > 10 * 60 * 1000) {
      stopLoginPoll();
      setLoginMessage("로그인 대기 시간이 지났습니다. 다시 시도하세요.", true);
    }
  }, 2000);
}

async function startLogin(method) {
  // Open the tab inside the click handler so popup blockers allow it.
  const tab = method === "browser" ? window.open("about:blank", "_blank") : null;
  try {
    const response = await fetch("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ method }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
    if (method === "browser") {
      if (tab) {
        tab.opener = null;
        tab.location.href = data.authUrl;
      } else {
        window.location.assign(data.authUrl);
      }
      setLoginMessage("열린 탭에서 ChatGPT 로그인을 마치면 자동으로 이어집니다.", false);
    } else {
      el.deviceBox.hidden = false;
      el.deviceUrl.href = data.verificationUrl;
      el.deviceUrl.textContent = data.verificationUrl;
      el.deviceCode.textContent = data.userCode;
      setLoginMessage("코드 입력을 마치면 자동으로 이어집니다.", false);
    }
    pollLogin();
  } catch (error) {
    if (tab) tab.close();
    setLoginMessage(`로그인을 시작하지 못했습니다: ${error.message}`, true);
  }
}

// ----------------------------------------------------------------- wiring

el.source.addEventListener("paste", () => {
  // Let the pasted text land in the textarea first.
  setTimeout(() => translate(), 0);
});

el.source.addEventListener("input", (event) => {
  el.counter.textContent = `${el.source.value.length.toLocaleString()}자`;
  if (event.inputType === "insertFromPaste") return;
  if (!el.source.value.trim()) translate();
  else if (el.live.checked) scheduleTranslate();
});

el.source.addEventListener("keydown", (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
    event.preventDefault();
    translate(true);
  }
});

el.style.addEventListener("change", () => translate());
el.clear.addEventListener("click", () => {
  el.source.value = "";
  el.counter.textContent = "0자";
  translate();
  el.source.focus();
});
$("login-browser").addEventListener("click", () => startLogin("browser"));
$("login-device").addEventListener("click", () => startLogin("device"));

const statusReady = refreshStatus();
setInterval(() => {
  if (!state.loginPoll && document.visibilityState === "visible") refreshStatus();
}, 60000);
