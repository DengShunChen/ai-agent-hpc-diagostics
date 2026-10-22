"use strict";
// Review page (Traditional Chinese UI). All server data is rendered with textContent, never
// parsed as HTML. Fixed values (status, domain, risk) are shown through the label tables
// below; generated diagnostic text from the server is shown as-is.
// The operator token is kept in sessionStorage and sent as a bearer token.

const $ = (id) => document.getElementById(id);
let currentRun = null;

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
const label = (table, value) => (value in table ? `${table[value]}（${value}）` : value);

function token() {
  try { return sessionStorage.getItem("operatorToken") || ""; } catch { return ""; }
}

function setToken(value) {
  try {
    if (value) sessionStorage.setItem("operatorToken", value);
    else sessionStorage.removeItem("operatorToken");
  } catch { /* storage unavailable: token lives only for this page */ }
}

function message(text) { $("message").textContent = text || ""; }

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

function fillList(id, items) {
  const list = $(id);
  list.replaceChildren(...(items.length ? items.map((t) => el("li", t)) : [el("li", "無")]));
}

async function signIn() {
  const typed = $("token").value.trim();
  if (typed) setToken(typed);
  $("token").value = "";
  try {
    const who = await api("/api/whoami");
    $("who").textContent = `已登入：${who.operator}`;
    $("workspace").hidden = false;
    message("");
    await Promise.all([loadIncidents(), loadRuns()]);
  } catch (err) {
    setToken("");
    $("workspace").hidden = true;
    $("who").textContent = "";
    message(`登入失敗：${err.message}`);
  }
}

function signOut() {
  setToken("");
  $("workspace").hidden = true;
  $("who").textContent = "";
  message("已登出。");
}

async function loadIncidents() {
  const ids = await api("/api/incidents");
  $("incidents").replaceChildren(...ids.map((id) => {
    const item = el("li", `${id} `);
    const button = el("button", "診斷");
    button.type = "button";
    button.addEventListener("click", () => diagnose(id));
    item.append(button);
    return item;
  }));
}

async function loadRuns() {
  const runs = await api("/api/runs");
  $("runs").replaceChildren(...runs.map((r) => {
    const row = el("tr");
    row.append(el("td", r.run_id), el("td", r.incident_id), el("td", label(STATUS, r.status)),
               el("td", localTime(r.timestamp)));
    const cell = el("td");
    const button = el("button", "審查");
    button.type = "button";
    button.addEventListener("click", () => openReview(r.run_id));
    cell.append(button);
    row.append(cell);
    return row;
  }));
}

async function diagnose(incidentId) {
  try {
    message(`正在診斷 ${incidentId}…`);
    const result = await api("/api/runs", { method: "POST", body: { incident_id: incidentId } });
    message(`已建立 ${result.run_id}，狀態：${label(STATUS, result.status)}`);
    await loadRuns();
    await openReview(result.run_id);
  } catch (err) { message(err.message); }
}

function renderAction(action, index) {
  const box = el("div", undefined, "action");
  box.append(el("strong", `動作 ${index + 1}`));
  const dl = el("dl");
  const add = (title, value) => { dl.append(el("dt", title)); dl.append(value instanceof Node ? value : el("dd", value)); };
  add("建議動作", action.proposed_action);
  add("原因", action.reason);
  const evidence = el("dd");
  const ul = el("ul");
  action.supporting_evidence.forEach((e) => {
    const li = el("li", e.statement);
    li.append(el("div", e.source, "source"));
    ul.append(li);
  });
  evidence.append(ul);
  add("支持證據", evidence);
  add("風險", label(RISK, action.risk));
  add("影響元件", action.affected_component);
  add("信心度", action.confidence.toFixed(2));
  const missing = el("dd");
  const mul = el("ul");
  (action.missing_information.length ? action.missing_information : ["無"]).forEach((m) => mul.append(el("li", m)));
  missing.append(mul);
  add("缺少的資訊", missing);
  const verify = el("dd");
  const vul = el("ul");
  (action.verification.length ? action.verification : ["無"]).forEach((v) => vul.append(el("li", v)));
  verify.append(vul);
  add("執行前驗證", verify);
  add("回復方式", action.rollback || "無");
  add("參考 SOP", action.reference || "無");
  box.append(dl);
  return box;
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
  button.disabled = true;
  message("正在請本機 LLM 產生說明，CPU 推論可能需要一到數分鐘…");
  try {
    const result = await api(`/api/runs/${encodeURIComponent(currentRun)}/explanation`, { method: "POST", body: {} });
    renderExplanation(result);
    message(`AI 說明：${label(AI_STATUS, result.status)}`);
  } catch (err) { message(err.message); } finally { button.disabled = false; }
}

function historyLine(h) {
  const who = h.operator ? `，操作員 ${h.operator}` : "";
  const note = h.operator_comment ? `：${h.operator_comment}` : "";
  return `${localTime(h.timestamp)}　${label(STATUS, h.status)}${who}${note}`;
}

async function openReview(runId) {
  try {
    const [review, history] = await Promise.all([
      api(`/api/runs/${encodeURIComponent(runId)}/review`),
      api(`/api/runs/${encodeURIComponent(runId)}/history`),
    ]);
    currentRun = runId;
    $("review").hidden = false;
    $("review-run").textContent = `${review.run_id}（${review.incident_id}）`;
    $("review-status").textContent = label(STATUS, review.status);
    $("review-class").textContent = label(DOMAIN, review.classification);
    $("review-confidence").textContent = review.confidence.toFixed(2);
    $("review-summary").textContent = review.summary;
    fillList("review-hypotheses", review.hypotheses);
    $("review-actions").replaceChildren(
      ...(review.actions.length ? review.actions.map(renderAction) : [el("p", "沒有提出補救動作。")]));
    $("review-remediation").textContent = review.remediation_summary || "";
    fillList("review-deferred", review.deferred);
    fillList("review-agents", (review.agents || []).map(
      (a) => `${a.agent}：${label(REASONING, a.reasoning)}${a.note ? `（${a.note}）` : ""}`));
    fillList("review-discarded", review.discarded_proposals || []);
    fillList("review-missing", review.missing_information);
    fillList("review-checks", review.recommended_checks);
    $("review-note").textContent = reviewNote(review);
    fillList("history", history.map(historyLine));
    await loadExplanation(runId);
  } catch (err) { message(err.message); }
}

async function act(path, body, what) {
  if (!currentRun) return;
  try {
    const result = await api(`/api/runs/${encodeURIComponent(currentRun)}/${path}`, { method: "POST", body });
    message(`已記錄${what}。沒有執行任何動作。`);
    $("comment").value = "";
    await loadRuns();
    await openReview(result.run_id && path === "revise" ? result.run_id : currentRun);
  } catch (err) { message(err.message); }
}

document.addEventListener("DOMContentLoaded", () => {
  $("signin").addEventListener("click", signIn);
  $("signout").addEventListener("click", signOut);
  $("refresh").addEventListener("click", () => loadRuns().catch((err) => message(err.message)));
  $("decide").addEventListener("click", () =>
    act("decision", { decision: $("decision").value, comment: $("comment").value }, "決定"));
  $("close").addEventListener("click", () => act("close", { comment: $("comment").value }, "結案"));
  $("revise").addEventListener("click", () => act("revise", { comment: $("comment").value }, "重新診斷"));
  $("explain").addEventListener("click", generateExplanation);
  if (token()) signIn();
});
