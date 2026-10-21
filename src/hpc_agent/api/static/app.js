"use strict";
// Review page. All server data is rendered with textContent, never parsed as HTML.
// The operator token is kept in sessionStorage and sent as a bearer token.

const $ = (id) => document.getElementById(id);
let currentRun = null;

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
    throw new Error(`${response.status}: ${typeof detail === "string" ? detail : JSON.stringify(detail)}`);
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
  list.replaceChildren(...(items.length ? items.map((t) => el("li", t)) : [el("li", "none")]));
}

async function signIn() {
  const typed = $("token").value.trim();
  if (typed) setToken(typed);
  $("token").value = "";
  try {
    const who = await api("/api/whoami");
    $("who").textContent = `Signed in as ${who.operator}`;
    $("workspace").hidden = false;
    message("");
    await Promise.all([loadIncidents(), loadRuns()]);
  } catch (err) {
    setToken("");
    $("workspace").hidden = true;
    $("who").textContent = "";
    message(`Sign-in failed: ${err.message}`);
  }
}

function signOut() {
  setToken("");
  $("workspace").hidden = true;
  $("who").textContent = "";
  message("Signed out.");
}

async function loadIncidents() {
  const ids = await api("/api/incidents");
  $("incidents").replaceChildren(...ids.map((id) => {
    const item = el("li", `${id} `);
    const button = el("button", "Diagnose");
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
    row.append(el("td", r.run_id), el("td", r.incident_id), el("td", r.status), el("td", r.timestamp));
    const cell = el("td");
    const button = el("button", "Review");
    button.type = "button";
    button.addEventListener("click", () => openReview(r.run_id));
    cell.append(button);
    row.append(cell);
    return row;
  }));
}

async function diagnose(incidentId) {
  try {
    message(`Diagnosing ${incidentId}...`);
    const result = await api("/api/runs", { method: "POST", body: { incident_id: incidentId } });
    message(`Run ${result.run_id} recorded: ${result.status}`);
    await loadRuns();
    await openReview(result.run_id);
  } catch (err) { message(err.message); }
}

function renderAction(action, index) {
  const box = el("div", undefined, "action");
  box.append(el("strong", `Action ${index + 1}`));
  const dl = el("dl");
  const add = (label, value) => { dl.append(el("dt", label)); dl.append(value instanceof Node ? value : el("dd", value)); };
  add("Proposed Action", action.proposed_action);
  add("Reason", action.reason);
  const evidence = el("dd");
  const ul = el("ul");
  action.supporting_evidence.forEach((e) => {
    const li = el("li", e.statement);
    li.append(el("div", e.source, "source"));
    ul.append(li);
  });
  evidence.append(ul);
  add("Supporting Evidence", evidence);
  add("Risk", action.risk);
  add("Affected Component", action.affected_component);
  add("Confidence", action.confidence.toFixed(2));
  const missing = el("dd");
  const mul = el("ul");
  (action.missing_information.length ? action.missing_information : ["none"]).forEach((m) => mul.append(el("li", m)));
  missing.append(mul);
  add("Missing Information", missing);
  box.append(dl);
  return box;
}

async function openReview(runId) {
  try {
    const [review, history] = await Promise.all([
      api(`/api/runs/${encodeURIComponent(runId)}/review`),
      api(`/api/runs/${encodeURIComponent(runId)}/history`),
    ]);
    currentRun = runId;
    $("review").hidden = false;
    $("review-run").textContent = `${review.run_id} (${review.incident_id})`;
    $("review-status").textContent = review.status;
    $("review-class").textContent = review.classification;
    $("review-confidence").textContent = review.confidence.toFixed(2);
    $("review-summary").textContent = review.summary;
    fillList("review-hypotheses", review.hypotheses);
    $("review-actions").replaceChildren(
      ...(review.actions.length ? review.actions.map(renderAction) : [el("p", "No remediation action proposed.")]));
    fillList("review-missing", review.missing_information);
    fillList("review-checks", review.recommended_checks);
    $("review-note").textContent = review.note;
    fillList("history", history.map((h) =>
      `${h.timestamp}  ${h.status}${h.operator ? ` by ${h.operator}` : ""}${h.operator_comment ? ` - ${h.operator_comment}` : ""}`));
  } catch (err) { message(err.message); }
}

async function act(path, body, label) {
  if (!currentRun) return;
  try {
    const result = await api(`/api/runs/${encodeURIComponent(currentRun)}/${path}`, { method: "POST", body });
    message(`${label} recorded. Nothing has been executed.`);
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
    act("decision", { decision: $("decision").value, comment: $("comment").value }, "Decision"));
  $("close").addEventListener("click", () => act("close", { comment: $("comment").value }, "Close"));
  $("revise").addEventListener("click", () => act("revise", { comment: $("comment").value }, "Revision"));
  if (token()) signIn();
});
