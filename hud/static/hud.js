"use strict";

const connection = document.getElementById("connection");
const signin = document.getElementById("signin");
const form = signin.querySelector("form");
const nonceInput = document.getElementById("nonce");
const submit = form.querySelector("button");
const signinMessage = document.getElementById("signin-message");
const dashboard = document.getElementById("dashboard");
const decisionAccess = document.getElementById("decision-access");
const stateMessage = document.getElementById("state-message");
const decisionMessage = document.getElementById("decision-message");
const approvals = document.getElementById("approvals");

const deciding = new Set();
const rendered = new Map();
const actionsByButton = new WeakMap();
let csrfToken = null;
let refreshRequest = null;
let stateAvailable = false;

const dateFormat = new Intl.DateTimeFormat("ko-KR", {
  dateStyle: "medium",
  timeStyle: "short",
});

function element(tag, value, className) {
  const node = document.createElement(tag);
  if (value !== undefined) node.textContent = value;
  if (className) node.className = className;
  return node;
}

function showStatus(label, message = "") {
  if (connection.textContent !== label) connection.textContent = label;
  if (stateMessage.textContent !== message) stateMessage.textContent = message;
}

function timeLabel(value) {
  const date = new Date(value);
  return element("span", Number.isNaN(date.getTime()) ? value : dateFormat.format(date), "entry-time");
}

function recordRow(record) {
  const row = element("li");
  const header = element("div", undefined, "entry-header");
  header.append(element("h3", `#${record.id} · ${record.kind}`), element("span", record.status, "badge"));
  row.append(header, timeLabel(record.created_at));
  if (record.question_preview) row.append(element("p", record.question_preview));
  return row;
}

function teamRow([name, team]) {
  const row = element("li");
  const header = element("div", undefined, "entry-header");
  const label = team.available ? "사용 가능" : "사용 불가";
  header.append(element("h3", name), element("span", label, team.available ? "badge available" : "badge"));
  row.append(header);
  if (team.reason) row.append(element("p", team.reason));
  return row;
}

function approvalRow(approval) {
  const row = element("li");
  const header = element("div", undefined, "entry-header");
  header.append(element("h3", approval.kind));
  if (approval.status === "executing") header.append(element("span", "실행 중단", "badge"));

  const expiry = element("p", "만료 ", "expiry");
  expiry.append(timeLabel(approval.expires_at));
  const actions = element("div", undefined, "actions");
  for (const [action, label] of [["approve", "승인"], ["reject", "거절"]]) {
    const button = element("button", label, action === "approve" ? "primary" : "secondary");
    button.type = "button";
    actionsByButton.set(button, { id: approval.id, action });
    actions.append(button);
  }
  row.append(header, element("p", approval.summary), expiry, actions);
  return row;
}

function renderList(list, rows, renderRow, emptyText) {
  const snapshot = JSON.stringify(rows);
  if (rendered.get(list) === snapshot) return;
  const fragment = document.createDocumentFragment();
  for (const row of rows) fragment.append(renderRow(row));
  if (!rows.length) fragment.append(element("li", emptyText, "empty"));
  list.replaceChildren(fragment);
  rendered.set(list, snapshot);
}

function updateDecisionButtons() {
  for (const button of approvals.querySelectorAll("button")) {
    button.disabled = !csrfToken || !stateAvailable || deciding.has(actionsByButton.get(button).id);
  }
}

function renderState(state) {
  const active = document.activeElement;
  const focusedAction = actionsByButton.get(active);
  const approvalCount = document.getElementById("approval-count");
  approvalCount.textContent = `${state.pending.length}건`;
  renderList(approvals, state.pending, approvalRow, "대기 중인 승인이 없습니다.");
  renderList(document.getElementById("teams"), Object.entries(state.teams), teamRow, "표시할 팀이 없습니다.");
  renderList(document.getElementById("records"), state.records, recordRow, "아직 일지가 없습니다.");
  updateDecisionButtons();
  if (focusedAction && !approvals.contains(active)) {
    const replacement = Array.from(approvals.querySelectorAll("button")).find((button) => {
      const { id, action } = actionsByButton.get(button);
      return id === focusedAction.id && action === focusedAction.action && !button.disabled;
    });
    (replacement || document.getElementById("approvals-title")).focus();
  }
}

async function loadState() {
  try {
    const response = await fetch("/api/state");
    if (response.status === 401) {
      if (!dashboard.hidden || csrfToken) {
        signinMessage.textContent = "세션이 종료됐습니다. javis hud를 다시 시작하고 새 코드를 입력하세요.";
      }
      csrfToken = null;
      stateAvailable = false;
      dashboard.hidden = true;
      decisionAccess.hidden = true;
      signin.hidden = false;
      showStatus("로그인 필요");
      return;
    }
    if (!response.ok) throw new Error("state");
    const state = await response.json();
    stateAvailable = true;
    dashboard.hidden = false;
    signin.hidden = true;
    decisionAccess.hidden = Boolean(csrfToken);
    showStatus(csrfToken ? "연결됨" : "읽기 전용");
    renderState(state);
  } catch {
    stateAvailable = false;
    showStatus("상태 확인 지연", "HUD 상태를 가져오지 못했습니다. 잠시 후 다시 확인합니다.");
    updateDecisionButtons();
  }
}

function refresh() {
  if (!refreshRequest) {
    refreshRequest = loadState().finally(() => {
      refreshRequest = null;
    });
  }
  return refreshRequest;
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (submit.disabled) return;
  submit.disabled = true;
  const nonce = nonceInput.value.trim();
  nonceInput.value = "";
  signinMessage.textContent = "";
  try {
    await refreshRequest;
    const response = await fetch("/session", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ nonce }),
    });
    if (response.status === 403) {
      signinMessage.textContent = "코드를 확인하세요. 이미 사용했거나 입력 한도를 넘겼다면 HUD를 다시 시작하세요.";
      return;
    }
    if (!response.ok) throw new Error("session");
    const session = await response.json();
    csrfToken = session.csrf_token;
    await refreshRequest;
    await refresh();
    if (!dashboard.hidden) document.getElementById("approvals-title").focus();
  } catch {
    signinMessage.textContent = "세션을 시작하지 못했습니다. HUD 서버가 실행 중인지 확인하세요.";
  } finally {
    submit.disabled = false;
    if (!signin.hidden) nonceInput.focus();
  }
});

const decisionLabels = {
  done: "승인 완료",
  rejected: "거절 완료",
  already_decided: "이미 결정된 요청입니다.",
  expired: "만료된 요청입니다.",
  not_found: "요청을 찾을 수 없습니다.",
  failed: "실행에 실패했습니다.",
};

approvals.addEventListener("click", async (event) => {
  const button = event.target.closest("button");
  if (!button || button.disabled || !csrfToken || !stateAvailable) return;
  const { id, action } = actionsByButton.get(button);
  if (deciding.has(id)) return;
  deciding.add(id);
  decisionMessage.textContent = action === "approve" ? "승인 처리 중…" : "거절 처리 중…";
  decisionMessage.focus();
  updateDecisionButtons();
  try {
    const response = await fetch(`/api/approvals/${encodeURIComponent(id)}/${action}`, {
      method: "POST",
      headers: { "X-CSRF-Token": csrfToken },
    });
    const result = await response.json();
    const label = decisionLabels[result.status] || `결정을 처리하지 못했습니다. (${response.status})`;
    decisionMessage.textContent = result.message ? `${label} · ${result.message}` : label;
  } catch {
    decisionMessage.textContent = "결정 결과를 확인하지 못했습니다. 대기 목록을 다시 확인합니다.";
  } finally {
    /* A poll already underway can contain the state before the decision. */
    await refreshRequest;
    await refresh();
    deciding.delete(id);
    updateDecisionButtons();
  }
});

submit.disabled = false;
refresh();
setInterval(() => {
  if (!submit.disabled) refresh();
}, 10_000);
