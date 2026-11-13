"use strict";
// Review console (Traditional Chinese UI). All server data is rendered with textContent, never
// parsed as HTML. Fixed values (status, domain, risk) are shown through the label tables
// below; generated diagnostic text from the server is shown as-is.
// The operator token is kept in sessionStorage and sent as a bearer token.

const $ = (id) => document.getElementById(id);
let currentRun = null;
let currentStatus = null;
let toastTimer = null;

const STATUS = {
  DRAFT: "草稿",
  DIAGNOSIS_COMPLETE: "診斷完成",
  AWAITING_HUMAN_APPROVAL: "等待人工核准",
  APPROVED: "已核准",
  REJECTED: "已退回",
  REVISION_REQUESTED: "要求修改",
  REVISE: "修訂中",
  CLOSED: "已結案",
};
const DOMAIN = {
  storage: "儲存", scheduler: "排程", network: "網路", gpu: "GPU",
  "multi-domain": "跨領域", unknown: "未知",
};
const RISK = { low: "低", medium: "中", high: "高" };
const DECISION = { APPROVED: "核准", REJECTED: "退回", REVISION_REQUESTED: "要求修改" };
const REASONING = { llm: "AI 推理（已通過規則檢查）", rules: "規則", "rules-fallback": "規則（AI 無法使用，改用規則）" };
const AI_STATUS = { accepted: "已採用", rejected: "已拒絕（未通過證據檢查）", unavailable: "無法使用（LLM 未回應）" };
const HTTP_ERRORS = {
  401: "驗證失敗：token 無效或未提供",
  404: "找不到指定的資料",
  409: "目前狀態不允許這個操作",
  411: "請求缺少內容長度",
  413: "內容過大",
  422: "輸入不正確或不被接受",
  500: "伺服器內部錯誤",
  503: "伺服器尚未設定操作員驗證",
};
const TRACE_KIND = {
  prompt: "提示", model_reply: "模型回覆", tool_call: "工具呼叫", tool_result: "工具結果",
  checks: "規則檢查", fallback: "改用規則",
};
// Advisory copy of ALLOWED_TRANSITIONS (safety/approval.py): it only decides which buttons are
// enabled. The server checks every transition again.
const CAN_DECIDE = new Set(["AWAITING_HUMAN_APPROVAL"]);
const CAN_CLOSE = new Set(["APPROVED"]);
const CAN_REVISE = new Set(["REJECTED", "REVISION_REQUESTED"]);
const STATE_TEXT = {
  APPROVED: "已核准，可以結案。本平台不會執行任何動作。",
  REJECTED: "已退回。可以重新診斷，產生一筆新的診斷紀錄。",
  REVISION_REQUESTED: "已要求修改。可以重新診斷，產生一筆新的診斷紀錄。",
  CLOSED: "已結案，沒有其他可用的操作。",
};

const label = (table, value) => (value in table ? `${table[value]}（${value}）` : value);
const short = (table, value) => (value in table ? table[value] : value);

function token() {
  try { return sessionStorage.getItem("operatorToken") || ""; } catch { return ""; }
}

function setToken(value) {
  try {
    if (value) sessionStorage.setItem("operatorToken", value);
    else sessionStorage.removeItem("operatorToken");
  } catch { /* storage unavailable: token lives only for this page */ }
}

function message(text, kind = "info") {
  const box = $("message");
  box.dataset.kind = kind;
  box.textContent = text || "";
  clearTimeout(toastTimer);
  if (text && kind !== "error") toastTimer = setTimeout(() => { box.textContent = ""; }, 6000);
}

function localTime(iso) {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString("zh-TW", { hour12: false });
}

async function api(path, options = {}) {
  const headers = { Authorization: `Bearer ${token()}` };
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetch(path, {
    method: options.method || "GET",
    headers,
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = data.error || data.detail || response.statusText;
    const text = typeof detail === "string" ? detail : JSON.stringify(detail);
    throw new Error(`${HTTP_ERRORS[response.status] || `錯誤 ${response.status}`}（${text}）`);
  }
  return data;
}

function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function setBusy(button, busy) {
  button.disabled = busy;
  if (busy) button.setAttribute("aria-busy", "true");
  else button.removeAttribute("aria-busy");
}

function statusPill(node, status, small = false) {
  node.className = `pill st-${status}${small ? " pill-sm" : ""}`;
  node.textContent = short(STATUS, status);
  node.title = status;
  return node;
}

function confidence(value) {
  const wrap = el("span", undefined, "conf");
  const meter = el("meter");
  meter.min = 0; meter.max = 1; meter.value = value;
  meter.setAttribute("aria-label", `信心度 ${value.toFixed(2)}`);
  wrap.append(meter, el("span", value.toFixed(2), "num"));
  return wrap;
}

// "(gpu, 0.50) text" or "(0.30) text": the coordinator's fixed prefix. Anything else is shown as-is.
function parseRanked(text) {
  const m = /^\((?:([a-z][a-z-]*), )?(\d(?:\.\d+)?)\)\s*([\s\S]*)$/.exec(text);
  return m ? { domain: m[1] || null, confidence: Number(m[2]), text: m[3] } : { domain: null, confidence: null, text };
}

function noneItem(text = "無") { return el("li", text, "none"); }

function fillList(id, items, render = (t) => el("li", t)) {
  $(id).replaceChildren(...(items.length ? items.map(render) : [noneItem()]));
}

// "get_tool(): note" -> the call in monospace, the note as text.
function checkItem(text) {
  const li = el("li");
  const m = /^([a-z_]+\(\))(.*)$/s.exec(text);
  if (m) li.append(el("code", m[1]), document.createTextNode(m[2]));
  else li.textContent = text;
  return li;
}

function traceLine(e) {
  const { seq, at, agent, kind, ...detail } = e;
  const text = JSON.stringify(detail);
  const li = el("li");
  li.append(el("span", agent, "trace-agent"), el("span", TRACE_KIND[kind] || kind, "trace-kind"),
            el("span", text.length > 400 ? text.slice(0, 400) + "…" : text, "trace-detail"));
  return li;
}

// --- session -----------------------------------------------------------------------------

function showSignedIn(operator) {
  $("who").textContent = operator;
  $("session").hidden = false;
  $("login").hidden = true;
  $("workspace").hidden = false;
}

function showSignedOut() {
  $("session").hidden = true;
  $("workspace").hidden = true;
  $("login").hidden = false;
  $("who").textContent = "";
  currentRun = null;
  currentStatus = null;
  $("review").hidden = true;
  $("rail").hidden = true;
  $("empty").hidden = false;
}

async function signIn() {
  const typed = $("token").value.trim();
  if (typed) setToken(typed);
  $("token").value = "";
  const button = $("signin");
  setBusy(button, true);
  try {
    const who = await api("/api/whoami");
    showSignedIn(who.operator);
    message("");
    await Promise.all([loadIncidents(), loadRuns()]);
  } catch (err) {
    setToken("");
    showSignedOut();
    message(`登入失敗：${err.message}`, "error");
  } finally { setBusy(button, false); }
}

function signOut() {
  setToken("");
  showSignedOut();
  message("已登出。");
}

// --- lists -------------------------------------------------------------------------------

async function loadIncidents() {
  const ids = await api("/api/incidents");
  $("incident-count").textContent = String(ids.length);
  $("incidents").replaceChildren(...(ids.length ? ids.map((id) => {
    const item = el("li");
    const button = el("button", "開始診斷", "btn btn-secondary btn-small");
    button.type = "button";
    button.setAttribute("aria-label", `開始診斷 ${id}`);
    button.addEventListener("click", () => diagnose(id, button));
    item.append(el("span", id, "incident-id"), button);
    return item;
  }) : [el("li", "沒有事件。", "list-empty")]));
}

async function loadRuns() {
  const runs = await api("/api/runs");
  $("run-count").textContent = String(runs.length);
  $("runs").replaceChildren(...(runs.length ? runs.map((r) => {
    const item = el("li");
    const button = el("button", undefined, "run-item");
    button.type = "button";
    button.dataset.run = r.run_id;
    if (r.run_id === currentRun) button.setAttribute("aria-current", "true");
    button.append(el("span", r.incident_id, "run-incident"), statusPill(el("span"), r.status, true),
                  el("span", r.run_id, "run-id"), el("span", localTime(r.timestamp), "run-time"));
    button.addEventListener("click", () => openReview(r.run_id));
    item.append(button);
    return item;
  }) : [el("li", "還沒有診斷紀錄。", "list-empty")]));
}

function markCurrent() {
  document.querySelectorAll("#runs .run-item").forEach((b) => {
    if (b.dataset.run === currentRun) b.setAttribute("aria-current", "true");
    else b.removeAttribute("aria-current");
  });
}

async function diagnose(incidentId, button) {
  setBusy(button, true);
  try {
    message(`正在診斷 ${incidentId}…`);
    const result = await api("/api/runs", { method: "POST", body: { incident_id: incidentId } });
    message(`已建立 ${result.run_id}，狀態：${short(STATUS, result.status)}`, "success");
    await loadRuns();
    await openReview(result.run_id);
  } catch (err) { message(err.message, "error"); } finally { setBusy(button, false); }
}

// --- review --------------------------------------------------------------------------------

function field(name, value, wide = false) {
  const box = el("div", undefined, `action-field${wide ? " wide" : ""}`);
  box.append(el("div", name, "field-name"));
  box.append(value instanceof Node ? value : el("div", value, "field-value"));
  return box;
}

function bulletList(items, className = "bullets") {
  const ul = el("ul", undefined, className);
  (items.length ? items : ["無"]).forEach((t) => ul.append(items.length ? el("li", t) : noneItem(t)));
  return ul;
}

function renderAction(action, index) {
  const box = el("section", undefined, "action");
  const head = el("div", undefined, "action-head");
  const badges = el("div", undefined, "action-badges");
  const risk = el("span", `風險：${short(RISK, action.risk)}`, `chip risk-${action.risk}`);
  risk.title = action.risk;
  badges.append(risk, confidence(action.confidence));
  head.append(el("span", `建議 ${index + 1}`, "action-index"), el("h4", action.proposed_action, "action-title"), badges);

  const evidence = el("ul", undefined, "evidence");
  action.supporting_evidence.forEach((e) => {
    const li = el("li");
    li.append(el("div", e.statement, "evidence-text"), el("span", e.source, "source"));
    evidence.append(li);
  });
  if (!action.supporting_evidence.length) evidence.append(noneItem());

  // The action's missing information also appears in the review's 證據缺口 card, so it starts folded.
  const missing = el("details", undefined, "action-missing");
  missing.append(el("summary", `缺少的資訊（${action.missing_information.length} 項）`),
                 bulletList(action.missing_information));

  const grid = el("div", undefined, "action-grid");
  grid.append(
    field("原因", action.reason),
    field("影響元件", action.affected_component),
    field("支持證據", evidence, true),
    field("執行前驗證", bulletList(action.verification, "bullets bullets-check"), true),
    field("回復方式", action.rollback || "無"),
    field("參考 SOP", action.reference || "無"),
  );
  if (action.missing_information.length) grid.append(field("證據缺口", missing, true));
  box.append(head, grid, el("div", "僅為建議：需要人工核准，本平台不會執行。", "action-foot"));
  return box;
}

function hypothesisItem(text, index) {
  const h = parseRanked(text);
  const li = el("li", undefined, "hyp");
  const body = el("div", undefined, "hyp-body");
  if (h.domain) {
    const chip = el("span", short(DOMAIN, h.domain), "chip chip-domain");
    chip.title = h.domain;
    const top = el("div");
    top.append(chip);
    body.append(top);
  }
  body.append(el("p", h.text, "hyp-text"));
  li.append(el("span", String(index + 1), "hyp-rank"), body);
  if (h.confidence !== null) li.append(confidence(h.confidence));
  return li;
}

function deferredItem(text) {
  const h = parseRanked(text);
  const li = el("li");
  const m = /^([\s\S]*?):\s*(confidence [\s\S]*)$/.exec(h.text);
  if (h.confidence !== null) li.append(confidence(h.confidence));
  else li.append(el("span"));
  li.append(el("span", m ? m[1] : h.text, "text"));
  if (m) li.append(el("span", m[2], "why"));
  return li;
}

function agentItem(a) {
  const li = el("li", undefined, "agent");
  li.append(el("span", a.agent, "agent-name"),
            el("span", short(REASONING, a.reasoning), `agent-mode${a.reasoning === "llm" ? " llm" : ""}`));
  if (a.note) li.append(el("span", a.note, "agent-note"));
  return li;
}

function reviewNote(review) {
  const base = "核准只記錄人的決定，本平台不會執行任何動作。";
  return review.actions.length ? base : `${base}本次沒有提出補救動作：決定代表接受或退回這份評估及其建議檢查。`;
}

function renderExplanation(x) {
  $("ai-reasons").replaceChildren();
  if (!x) {
    $("ai-meta").textContent = "尚未產生。";
    $("ai-text").textContent = "";
    $("ai-disclaimer").textContent = "";
    return;
  }
  $("ai-meta").textContent = `${label(AI_STATUS, x.status)} · ${x.provider}:${x.model} · ${localTime(x.generated_at)} · ${(x.duration_ms / 1000).toFixed(1)} 秒`;
  $("ai-text").textContent = x.text || "";
  if (x.status !== "accepted") fillList("ai-reasons", x.rejection_reasons);
  $("ai-disclaimer").textContent = x.disclaimer;
}

async function loadExplanation(runId) {
  const result = await api(`/api/runs/${encodeURIComponent(runId)}/explanation`);
  renderExplanation(result.explanation);
}

async function generateExplanation() {
  if (!currentRun) return;
  const button = $("explain");
  setBusy(button, true);
  message("正在請本機 LLM 產生說明，CPU 推論可能需要一到數分鐘…");
  try {
    const result = await api(`/api/runs/${encodeURIComponent(currentRun)}/explanation`, { method: "POST", body: {} });
    renderExplanation(result);
    message(`AI 說明：${label(AI_STATUS, result.status)}`, result.status === "accepted" ? "success" : "info");
  } catch (err) { message(err.message, "error"); } finally { setBusy(button, false); }
}

function historyItem(h) {
  const li = el("li");
  const top = el("div");
  top.append(statusPill(el("span"), h.status, true));
  li.append(top, el("span", localTime(h.timestamp), "tl-time"));
  if (h.operator) li.append(el("span", `操作員 ${h.operator}`, "tl-who"));
  if (h.operator_comment) li.append(el("p", h.operator_comment, "tl-comment"));
  return li;
}

function selectedDecision() {
  const checked = document.querySelector('input[name="decision"]:checked');
  return checked ? checked.value : "APPROVED";
}

function updateCommentHints() {
  const deciding = CAN_DECIDE.has(currentStatus);
  const needs = deciding && selectedDecision() !== "APPROVED";
  $("comment-required").hidden = !needs;
  $("comment-optional").hidden = deciding;
  $("comment").placeholder = deciding ? "說明理由（退回或要求修改時必填）" : "說明原因（選填）";
  $("comment-count").textContent = `${$("comment").value.length} / 1000`;
}

// Shows only the operations the current status allows; the server enforces them regardless.
function applyAllowed(status) {
  const decide = CAN_DECIDE.has(status);
  const close = CAN_CLOSE.has(status);
  const revise = CAN_REVISE.has(status);
  $("decision-group").hidden = !decide;
  $("decide").hidden = !decide;
  $("decide").disabled = !decide;
  document.querySelectorAll('input[name="decision"]').forEach((r) => { r.disabled = !decide; });
  $("close").hidden = !close;
  $("close").disabled = !close;
  $("revise").hidden = !revise;
  $("revise").disabled = !revise;
  $("comment-box").hidden = !(decide || close || revise);
  $("decision-state").hidden = decide;
  $("decision-state").textContent = decide ? "" : (STATE_TEXT[status] || "目前狀態沒有可用的決定。");
  $("close").classList.toggle("btn-primary", close);
  $("close").classList.toggle("btn-secondary", !close);
  $("revise").classList.toggle("btn-primary", revise);
  $("revise").classList.toggle("btn-secondary", !revise);
}

async function openReview(runId) {
  $("main").setAttribute("aria-busy", "true");
  try {
    const [review, history, trace] = await Promise.all([
      api(`/api/runs/${encodeURIComponent(runId)}/review`),
      api(`/api/runs/${encodeURIComponent(runId)}/history`),
      api(`/api/runs/${encodeURIComponent(runId)}/trace`),
    ]);
    currentRun = runId;
    currentStatus = review.status;
    markCurrent();
    $("empty").hidden = true;
    $("review").hidden = false;
    $("rail").hidden = false;
    $("review-run").textContent = review.run_id;
    $("review-title").textContent = review.incident_id;
    statusPill($("review-status"), review.status);
    statusPill($("rail-status"), review.status);
    $("review-class").textContent = short(DOMAIN, review.classification);
    $("review-class").title = review.classification;
    $("review-meter").value = review.confidence;
    $("review-confidence").textContent = review.confidence.toFixed(2);
    $("review-summary").textContent = review.summary;
    fillList("review-hypotheses", review.hypotheses, hypothesisItem);
    $("review-remediation").textContent = review.remediation_summary || "";
    $("review-actions").replaceChildren(
      ...(review.actions.length ? review.actions.map(renderAction) : [el("p", "沒有提出補救動作。", "no-actions")]));
    fillList("review-deferred", review.deferred, deferredItem);
    fillList("review-missing", review.missing_information);
    fillList("review-checks", review.recommended_checks, checkItem);
    fillList("review-agents", review.agents || [], agentItem);
    fillList("review-discarded", review.discarded_proposals || []);
    $("review-trace").replaceChildren(...(trace.length ? trace.map(traceLine)
      : [noneItem("沒有 AI 執行紀錄（規則式診斷）")]));
    $("review-note").textContent = reviewNote(review);
    $("history").replaceChildren(...(history.length ? history.map(historyItem) : [noneItem()]));
    applyAllowed(review.status);
    updateCommentHints();
    await loadExplanation(runId);
  } catch (err) { message(err.message, "error"); } finally { $("main").removeAttribute("aria-busy"); }
}

async function act(path, body, what, button) {
  if (!currentRun) return;
  setBusy(button, true);
  try {
    const result = await api(`/api/runs/${encodeURIComponent(currentRun)}/${path}`, { method: "POST", body });
    message(`已記錄${what}。沒有執行任何動作。`, "success");
    $("comment").value = "";
    await loadRuns();
    await openReview(result.run_id && path === "revise" ? result.run_id : currentRun);
  } catch (err) { message(err.message, "error"); } finally {
    button.removeAttribute("aria-busy");
    applyAllowed(currentStatus);
    updateCommentHints();
  }
}

function requestDecision() {
  const decision = selectedDecision();
  const comment = $("comment").value.trim();
  if (decision !== "APPROVED" && !comment) {
    message(`${DECISION[decision]}時必須填寫說明。`, "error");
    $("comment").focus();
    return;
  }
  $("confirm-text").textContent = `將對 ${currentRun} 記錄「${DECISION[decision]}」。`;
  const dialog = $("confirm");
  if (typeof dialog.showModal === "function") dialog.showModal();
  else confirmDecision();
}

function confirmDecision() {
  const dialog = $("confirm");
  if (dialog.open) dialog.close();
  act("decision", { decision: selectedDecision(), comment: $("comment").value }, "決定", $("decide"));
}

document.addEventListener("DOMContentLoaded", () => {
  $("signin").addEventListener("click", signIn);
  $("token").addEventListener("keydown", (e) => { if (e.key === "Enter") signIn(); });
  $("signout").addEventListener("click", signOut);
  $("refresh").addEventListener("click", () => loadRuns().catch((err) => message(err.message, "error")));
  $("decide").addEventListener("click", requestDecision);
  $("confirm-ok").addEventListener("click", confirmDecision);
  $("confirm-cancel").addEventListener("click", () => $("confirm").close());
  $("close").addEventListener("click", () => act("close", { comment: $("comment").value }, "結案", $("close")));
  $("revise").addEventListener("click", () => act("revise", { comment: $("comment").value }, "重新診斷", $("revise")));
  $("explain").addEventListener("click", generateExplanation);
  $("comment").addEventListener("input", updateCommentHints);
  document.querySelectorAll('input[name="decision"]').forEach((r) => r.addEventListener("change", updateCommentHints));
  if (token()) signIn();
});
