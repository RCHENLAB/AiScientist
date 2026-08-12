const state = {
  sessions: [],
  currentSession: null,
  loading: false,
  loggedIn: false,
};

const elements = {
  loginScreen: document.querySelector("#loginScreen"),
  workspaceScreen: document.querySelector("#workspaceScreen"),
  loginUcinetidInput: document.querySelector("#loginUcinetidInput"),
  loginPasswordInput: document.querySelector("#loginPasswordInput"),
  loginCampusNetworkCheck: document.querySelector("#loginCampusNetworkCheck"),
  loginDuoCheck: document.querySelector("#loginDuoCheck"),
  loginRequestBtn: document.querySelector("#loginRequestBtn"),
  loginConnectionRequest: document.querySelector("#loginConnectionRequest"),
  loginSshCommandBox: document.querySelector("#loginSshCommandBox"),
  loginConnectionSteps: document.querySelector("#loginConnectionSteps"),
  sessionList: document.querySelector("#sessionList"),
  newSessionBtn: document.querySelector("#newSessionBtn"),
  sessionMode: document.querySelector("#sessionMode"),
  sessionTitle: document.querySelector("#sessionTitle"),
  runStatus: document.querySelector("#runStatus"),
  hpcState: document.querySelector("#hpcState"),
  chatStream: document.querySelector("#chatStream"),
  messageForm: document.querySelector("#messageForm"),
  messageInput: document.querySelector("#messageInput"),
  workflowSelect: document.querySelector("#workflowSelect"),
  sendBtn: document.querySelector("#sendBtn"),
  workflowState: document.querySelector("#workflowState"),
  progressList: document.querySelector("#progressList"),
  reportTitle: document.querySelector("#reportTitle"),
  reportPreview: document.querySelector("#reportPreview"),
  downloadReport: document.querySelector("#downloadReport"),
  artifactList: document.querySelector("#artifactList"),
  connectHpcBtn: document.querySelector("#connectHpcBtn"),
  authModal: document.querySelector("#authModal"),
  closeAuthBtn: document.querySelector("#closeAuthBtn"),
  startAuthBtn: document.querySelector("#startAuthBtn"),
  completeAuthBtn: document.querySelector("#completeAuthBtn"),
  copySshBtn: document.querySelector("#copySshBtn"),
  authForm: document.querySelector("#authForm"),
  ucinetidInput: document.querySelector("#ucinetidInput"),
  authMethodSelect: document.querySelector("#authMethodSelect"),
  campusNetworkCheck: document.querySelector("#campusNetworkCheck"),
  duoCheck: document.querySelector("#duoCheck"),
  duoCheckRow: document.querySelector("#duoCheckRow"),
  keyPolicyCheck: document.querySelector("#keyPolicyCheck"),
  keyPolicyCheckRow: document.querySelector("#keyPolicyCheckRow"),
  connectionRequest: document.querySelector("#connectionRequest"),
  sshCommandBox: document.querySelector("#sshCommandBox"),
  connectionSteps: document.querySelector("#connectionSteps"),
  toast: document.querySelector("#toast"),
};

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function showToast(message) {
  elements.toast.textContent = message;
  elements.toast.classList.add("visible");
  window.setTimeout(() => elements.toast.classList.remove("visible"), 2400);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const contentType = response.headers.get("Content-Type") || "";
  const payload = contentType.includes("application/json") ? await response.json() : await response.text();
  if (!response.ok) {
    const message = typeof payload === "object" && payload.error ? payload.error : "Request failed";
    throw new Error(message);
  }
  return payload;
}

function statusLabel(status) {
  const labels = {
    idle: "Idle",
    running: "Running",
    completed: "Completed",
    needs_auth: "Needs authorization",
    failed: "Failed",
  };
  return labels[status] || status || "Idle";
}

function statusClass(status) {
  if (status === "completed") return "success";
  if (status === "needs_auth") return "warning";
  if (status === "failed") return "danger";
  if (status === "running") return "active";
  return "muted";
}

function renderSessionList() {
  if (!state.sessions.length) {
    elements.sessionList.innerHTML = '<div class="empty-state">No sessions yet.</div>';
    return;
  }
  elements.sessionList.innerHTML = state.sessions
    .map((session) => {
      const active = state.currentSession && state.currentSession.id === session.id ? "active" : "";
      const report = session.report_available ? "Report ready" : "No report";
      return `
        <button class="session-item ${active}" data-session-id="${session.id}" type="button">
          <strong>${escapeHtml(session.title || "AiScientist session")}</strong>
          <span>${escapeHtml(statusLabel(session.status))} · ${escapeHtml(report)}</span>
        </button>
      `;
    })
    .join("");
}

function renderMessages(session) {
  const messages = session?.messages || [];
  elements.chatStream.innerHTML = messages
    .map((message) => {
      if (message.kind === "auth_request") {
        return `
          <article class="message assistant auth-card">
            <div class="message-role">AiScientist</div>
            <p>${escapeHtml(message.content)}</p>
            <button class="primary-action auth-card-button" type="button">Open authorization</button>
          </article>
        `;
      }
      const role = message.role === "user" ? "user" : "assistant";
      return `
        <article class="message ${role}">
          <div class="message-role">${role === "user" ? "Researcher" : "AiScientist"}</div>
          <p>${escapeHtml(message.content)}</p>
        </article>
      `;
    })
    .join("");
  elements.chatStream.scrollTop = elements.chatStream.scrollHeight;
}

function renderArtifacts(session) {
  const artifacts = session?.artifacts || [];
  if (!artifacts.length) {
    elements.artifactList.innerHTML = '<div class="empty-state">Artifacts will appear after a run.</div>';
    return;
  }
  elements.artifactList.innerHTML = artifacts
    .map(
      (artifact) => `
        <a class="artifact-item" href="${artifact.download_url}">
          <strong>${escapeHtml(artifact.name)}</strong>
          <span>${escapeHtml(artifact.summary || artifact.kind)}</span>
        </a>
      `
    )
    .join("");
}

function renderProgress(session) {
  const status = session?.status || "idle";
  const hpc = session?.hpc_auth?.status || "not_connected";
  const steps = [
    ["Session", session ? "ready" : "waiting"],
    ["Authorization", hpc === "connected" ? "connected" : hpc],
    ["Workflow", status],
    ["Report", session?.report?.available ? "ready" : "pending"],
  ];
  elements.progressList.innerHTML = steps
    .map(
      ([label, value]) => `
        <div class="progress-row">
          <span>${escapeHtml(label)}</span>
          <strong>${escapeHtml(value)}</strong>
        </div>
      `
    )
    .join("");
}

function renderReport(session) {
  const report = session?.report || { available: false };
  if (report.available) {
    elements.reportTitle.textContent = "Final report ready";
    elements.reportPreview.textContent = report.preview || "Report is available for download.";
    elements.downloadReport.href = report.download_url;
    elements.downloadReport.classList.remove("disabled");
    elements.downloadReport.setAttribute("aria-disabled", "false");
  } else {
    elements.reportTitle.textContent = "No report yet";
    elements.reportPreview.textContent = "The final report will appear here after AiScientist completes a workflow.";
    elements.downloadReport.href = "#";
    elements.downloadReport.classList.add("disabled");
    elements.downloadReport.setAttribute("aria-disabled", "true");
  }
}

function renderCurrentSession() {
  const session = state.currentSession;
  elements.sessionTitle.textContent = session?.title || "New AiScientist session";
  elements.sessionMode.textContent = session?.llm_mode === "configured" ? "LLM configured" : "Offline local mode";
  elements.runStatus.textContent = statusLabel(session?.status);
  elements.runStatus.className = `status-pill ${statusClass(session?.status)}`;
  const hpcStatus = session?.hpc_auth?.status || "not_connected";
  elements.hpcState.textContent = hpcStatus === "connected" ? "HPC connected" : "HPC disconnected";
  elements.hpcState.className = `status-pill ${hpcStatus === "connected" ? "success" : "muted"}`;
  elements.workflowState.textContent = statusLabel(session?.status);
  renderMessages(session);
  renderArtifacts(session);
  renderProgress(session);
  renderReport(session);
  renderSessionList();
}

async function refreshSessions() {
  const payload = await api("/api/sessions");
  state.sessions = payload.sessions || [];
  renderSessionList();
}

async function createSession() {
  const session = await api("/api/sessions", { method: "POST", body: "{}" });
  state.currentSession = session;
  await refreshSessions();
  renderCurrentSession();
}

async function loadSession(sessionId) {
  state.currentSession = await api(`/api/sessions/${sessionId}`);
  renderCurrentSession();
}

function selectedWorkflowPrefix() {
  const value = elements.workflowSelect.value;
  if (value === "auto") return "";
  const label = elements.workflowSelect.options[elements.workflowSelect.selectedIndex].textContent;
  return `Use ${label} workflow. `;
}

async function sendMessage(event) {
  event.preventDefault();
  const content = elements.messageInput.value.trim();
  if (!content || state.loading) return;
  if (!state.currentSession) {
    await createSession();
  }
  state.loading = true;
  elements.sendBtn.disabled = true;
  const visibleContent = `${selectedWorkflowPrefix()}${content}`;
  const optimistic = structuredClone(state.currentSession);
  optimistic.messages = [
    ...(optimistic.messages || []),
    { role: "user", kind: "text", content: visibleContent, created_at: new Date().toISOString() },
    { role: "assistant", kind: "text", content: "Running AiScientist workflow...", created_at: new Date().toISOString() },
  ];
  optimistic.status = "running";
  state.currentSession = optimistic;
  renderCurrentSession();
  elements.messageInput.value = "";
  try {
    state.currentSession = await api(`/api/sessions/${state.currentSession.id}/messages`, {
      method: "POST",
      body: JSON.stringify({ content: visibleContent }),
    });
    await refreshSessions();
    renderCurrentSession();
    if (state.currentSession.status === "needs_auth") {
      openAuthModal();
    }
  } catch (error) {
    showToast(error.message);
    await loadSession(state.currentSession.id);
  } finally {
    state.loading = false;
    elements.sendBtn.disabled = false;
  }
}

function openAuthModal() {
  if (!state.currentSession) {
    showToast("Create a session before connecting HPC.");
    return;
  }
  renderAuthRequest(state.currentSession.hpc_auth);
  elements.authModal.classList.remove("hidden");
}

function closeAuthModal() {
  elements.authModal.classList.add("hidden");
}

function renderAuthRequest(request) {
  if (!request || !request.request_id) {
    elements.connectionRequest.classList.add("hidden");
    elements.sshCommandBox.textContent = "";
    elements.connectionSteps.innerHTML = "";
    elements.copySshBtn.disabled = true;
    elements.completeAuthBtn.disabled = true;
    return;
  }
  elements.connectionRequest.classList.remove("hidden");
  elements.sshCommandBox.textContent = request.ssh_command || "";
  elements.connectionSteps.innerHTML = (request.instructions || [])
    .map((item) => `<li>${escapeHtml(item)}</li>`)
    .join("");
  elements.copySshBtn.disabled = !request.ssh_command;
  elements.completeAuthBtn.disabled = request.status !== "pending";
}

function renderLoginRequest(request) {
  if (!request || !request.request_id) {
    elements.loginConnectionRequest.classList.add("hidden");
    elements.loginSshCommandBox.textContent = "";
    elements.loginConnectionSteps.innerHTML = "";
    return;
  }
  elements.loginConnectionRequest.classList.remove("hidden");
  elements.loginSshCommandBox.textContent = request.gateway_status || request.ssh_command || "";
  elements.loginConnectionSteps.innerHTML = (request.instructions || [])
    .map((item) => `<li>${escapeHtml(item)}</li>`)
    .join("");
}

function syncAuthMethod() {
  const method = elements.authMethodSelect.value;
  const usingKey = method === "ssh_key";
  elements.duoCheckRow.classList.toggle("hidden", usingKey);
  elements.keyPolicyCheckRow.classList.toggle("hidden", !usingKey);
}

async function requestHpcConnection(sessionId, formValues) {
  return api("/api/auth/hpc/start", {
    method: "POST",
    body: JSON.stringify({
      session_id: sessionId,
      ucinetid: formValues.ucinetid,
      password: formValues.password,
      auth_method: "password_duo",
      campus_network_confirmed: formValues.campusNetworkConfirmed,
      duo_confirmed: formValues.duoConfirmed,
    }),
  });
}

function loginFormValues() {
  return {
    ucinetid: elements.loginUcinetidInput.value.trim(),
    password: elements.loginPasswordInput.value,
    campusNetworkConfirmed: elements.loginCampusNetworkCheck.checked,
    duoConfirmed: elements.loginDuoCheck.checked,
  };
}

async function requestLoginConnection() {
  const values = loginFormValues();
  if (!values.ucinetid) {
    showToast("Enter your UCInetID first.");
    elements.loginUcinetidInput.focus();
    return;
  }
  if (!values.password) {
    showToast("Enter your password.");
    elements.loginPasswordInput.focus();
    return;
  }
  try {
    elements.loginRequestBtn.disabled = true;
    elements.loginRequestBtn.textContent = "Logging in...";
    if (!state.currentSession) {
      state.currentSession = await api("/api/sessions", { method: "POST", body: "{}" });
    }
    const request = await requestHpcConnection(state.currentSession.id, values);
    state.currentSession = await api(`/api/sessions/${state.currentSession.id}`);
    renderLoginRequest(request);
    state.loggedIn = true;
    elements.loginScreen.classList.add("hidden");
    elements.workspaceScreen.classList.remove("hidden");
    await refreshSessions();
    renderCurrentSession();
    showToast(`Logged in to HPC3 gateway as ${request.ucinetid}.`);
  } catch (error) {
    showToast(error.message);
  } finally {
    elements.loginRequestBtn.disabled = false;
    elements.loginRequestBtn.textContent = "Log in to HPC3";
    elements.loginPasswordInput.value = "";
  }
}

async function startAuth() {
  if (!state.currentSession) return;
  const requestPayload = {
    session_id: state.currentSession.id,
    ucinetid: elements.ucinetidInput.value.trim(),
    auth_method: elements.authMethodSelect.value,
    campus_network_confirmed: elements.campusNetworkCheck.checked,
    duo_confirmed: elements.duoCheck.checked,
    key_policy_confirmed: elements.keyPolicyCheck.checked,
  };
  if (!requestPayload.ucinetid) {
    showToast("Enter your UCInetID first.");
    elements.ucinetidInput.focus();
    return;
  }
  try {
    const responsePayload = await api("/api/auth/hpc/start", {
      method: "POST",
      body: JSON.stringify(requestPayload),
    });
    renderAuthRequest(responsePayload);
    showToast(`HPC3 connection request created for ${responsePayload.ucinetid}.`);
    state.currentSession = await api(`/api/sessions/${state.currentSession.id}`);
    renderCurrentSession();
    renderAuthRequest(state.currentSession.hpc_auth);
  } catch (error) {
    showToast(error.message);
  }
}

async function completeAuth() {
  if (!state.currentSession) return;
  const requestId = state.currentSession.hpc_auth?.request_id;
  try {
    state.currentSession = await api("/api/auth/hpc/complete", {
      method: "POST",
      body: JSON.stringify({ session_id: state.currentSession.id, request_id: requestId }),
    });
    await refreshSessions();
    closeAuthModal();
    renderCurrentSession();
    showToast("HPC authorization connected for this session.");
  } catch (error) {
    showToast(error.message);
  }
}

async function copySshCommand() {
  const command = state.currentSession?.hpc_auth?.ssh_command || elements.sshCommandBox.textContent;
  if (!command) return;
  try {
    await navigator.clipboard.writeText(command);
    showToast("SSH command copied.");
  } catch {
    showToast("Clipboard unavailable.");
  }
}

function wireEvents() {
  elements.loginRequestBtn.addEventListener("click", requestLoginConnection);
  document.querySelector("#loginForm").addEventListener("submit", (event) => {
    event.preventDefault();
    requestLoginConnection();
  });
  elements.newSessionBtn.addEventListener("click", createSession);
  elements.messageForm.addEventListener("submit", sendMessage);
  elements.connectHpcBtn.addEventListener("click", openAuthModal);
  elements.closeAuthBtn.addEventListener("click", closeAuthModal);
  elements.startAuthBtn.addEventListener("click", startAuth);
  elements.completeAuthBtn.addEventListener("click", completeAuth);
  elements.copySshBtn.addEventListener("click", copySshCommand);
  elements.authMethodSelect.addEventListener("change", syncAuthMethod);
  elements.sessionList.addEventListener("click", (event) => {
    const button = event.target.closest("[data-session-id]");
    if (button) loadSession(button.dataset.sessionId);
  });
  document.querySelectorAll("[data-example]").forEach((button) => {
    button.addEventListener("click", () => {
      elements.messageInput.value = button.dataset.example;
      elements.messageInput.focus();
    });
  });
  elements.chatStream.addEventListener("click", (event) => {
    if (event.target.closest(".auth-card-button")) openAuthModal();
  });
  elements.downloadReport.addEventListener("click", (event) => {
    if (elements.downloadReport.classList.contains("disabled")) {
      event.preventDefault();
      showToast("No report is available yet.");
    }
  });
}

async function boot() {
  wireEvents();
  syncAuthMethod();
  renderLoginRequest(null);
  elements.workspaceScreen.classList.add("hidden");
  elements.loginScreen.classList.remove("hidden");
}

boot().catch((error) => {
  showToast(error.message);
});
