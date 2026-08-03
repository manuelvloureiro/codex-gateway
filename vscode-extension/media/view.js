(function () {
  "use strict";

  const vscode = acquireVsCodeApi();
  const elements = {
    status: document.getElementById("status"),
    statusText: document.getElementById("status-text"),
    config: document.getElementById("config"),
    plan: document.getElementById("plan"),
    planItems: document.getElementById("plan-items"),
    transcript: document.getElementById("transcript"),
    empty: document.getElementById("empty"),
    error: document.getElementById("error"),
    prompt: document.getElementById("prompt"),
    send: document.getElementById("send"),
    cancel: document.getElementById("cancel"),
  };

  let currentState = null;
  let promptSequence = 0;
  let pendingPrompt = null;
  const saved = vscode.getState();
  if (saved && typeof saved.draft === "string") {
    elements.prompt.value = saved.draft;
  }

  function optionList(options) {
    const flattened = [];
    for (const option of Array.isArray(options) ? options : []) {
      if (option && Array.isArray(option.options)) {
        flattened.push(...option.options);
      } else if (option) {
        flattened.push(option);
      }
    }
    return flattened;
  }

  function renderConfig(configOptions, busy) {
    elements.config.replaceChildren();
    for (const config of Array.isArray(configOptions) ? configOptions : []) {
      if (!config || config.type !== "select" || typeof config.id !== "string") {
        continue;
      }
      const label = document.createElement("label");
      label.textContent = typeof config.name === "string" ? config.name : config.id;
      const select = document.createElement("select");
      select.dataset.configId = config.id;
      select.disabled = busy;
      select.title = typeof config.description === "string" ? config.description : "";
      for (const item of optionList(config.options)) {
        if (!item || typeof item.value !== "string") {
          continue;
        }
        const option = document.createElement("option");
        option.value = item.value;
        option.textContent = typeof item.name === "string" ? item.name : item.value;
        option.title = typeof item.description === "string" ? item.description : "";
        option.selected = item.value === config.currentValue;
        select.append(option);
      }
      select.addEventListener("change", () => {
        vscode.postMessage({
          type: "setConfigOption",
          configId: config.id,
          value: select.value,
        });
      });
      label.append(select);
      elements.config.append(label);
    }
  }

  function renderPlan(plan) {
    const entries = Array.isArray(plan) ? plan : [];
    elements.plan.hidden = entries.length === 0;
    elements.planItems.replaceChildren();
    for (const entry of entries) {
      const item = document.createElement("li");
      item.textContent = typeof entry.content === "string" ? entry.content : "";
      item.dataset.status = typeof entry.status === "string" ? entry.status : "pending";
      elements.planItems.append(item);
    }
  }

  function renderTranscript(messages, tools) {
    elements.transcript.replaceChildren();
    const items = [];
    for (const message of Array.isArray(messages) ? messages : []) {
      items.push({ ...message, itemType: "message" });
    }
    for (const tool of Array.isArray(tools) ? tools : []) {
      items.push({ ...tool, itemType: "tool" });
    }
    items.sort((left, right) => (left.sequence || 0) - (right.sequence || 0));
    elements.empty.hidden = items.length > 0;

    for (const item of items) {
      if (item.itemType === "message") {
        const article = document.createElement("article");
        const role = ["user", "agent", "thought"].includes(item.role)
          ? item.role
          : "agent";
        article.className = `message ${role}`;
        const heading = document.createElement("div");
        heading.className = "role";
        heading.textContent = role === "thought" ? "Reasoning" : role;
        const body = document.createElement("pre");
        body.textContent = typeof item.text === "string" ? item.text : "";
        article.append(heading, body);
        elements.transcript.append(article);
        continue;
      }

      const details = document.createElement("details");
      details.className = "tool";
      details.open = item.status === "failed";
      const summary = document.createElement("summary");
      const title = typeof item.title === "string" ? item.title : "Tool call";
      const status = typeof item.status === "string" ? item.status : "pending";
      summary.textContent = `${title} — ${status.replaceAll("_", " ")}`;
      const body = document.createElement("pre");
      body.textContent = typeof item.output === "string" ? item.output : "";
      details.append(summary);
      if (body.textContent) {
        details.append(body);
      }
      elements.transcript.append(details);
    }
  }

  function render(state) {
    currentState = state;
    const busy = Boolean(state.busy);
    elements.status.dataset.state = state.status || "idle";
    elements.statusText.textContent = state.statusText || "Not connected";
    elements.error.textContent = state.error || "";
    elements.prompt.disabled = !state.canPrompt;
    elements.send.disabled =
      !state.canPrompt || busy || pendingPrompt !== null || !elements.prompt.value.trim();
    elements.cancel.disabled = !state.canCancelTurn;
    renderConfig(state.configOptions, busy);
    renderPlan(state.plan);
    renderTranscript(state.messages, state.tools);
    requestAnimationFrame(() => {
      window.scrollTo({ top: document.body.scrollHeight, behavior: "auto" });
    });
  }

  function sendPrompt() {
    if (
      !currentState ||
      !currentState.canPrompt ||
      currentState.busy ||
      pendingPrompt !== null
    ) {
      return;
    }
    const text = elements.prompt.value.trim();
    if (!text) {
      return;
    }
    const requestId = `${Date.now()}-${++promptSequence}`;
    pendingPrompt = { requestId, input: elements.prompt.value };
    vscode.postMessage({ type: "prompt", requestId, text });
    elements.send.disabled = true;
  }

  elements.prompt.addEventListener("input", () => {
    vscode.setState({ draft: elements.prompt.value });
    elements.send.disabled =
      !currentState ||
      !currentState.canPrompt ||
      currentState.busy ||
      pendingPrompt !== null ||
      !elements.prompt.value.trim();
  });
  elements.prompt.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      sendPrompt();
    }
  });
  elements.send.addEventListener("click", sendPrompt);
  elements.cancel.addEventListener("click", () => {
    vscode.postMessage({ type: "cancel" });
  });

  window.addEventListener("message", (event) => {
    const message = event.data;
    if (message && message.type === "state" && message.state) {
      render(message.state);
    } else if (
      message &&
      (message.type === "promptAccepted" || message.type === "promptRejected") &&
      pendingPrompt &&
      message.requestId === pendingPrompt.requestId
    ) {
      if (
        message.type === "promptAccepted" &&
        elements.prompt.value === pendingPrompt.input
      ) {
        elements.prompt.value = "";
        vscode.setState({ draft: "" });
      }
      pendingPrompt = null;
      elements.send.disabled =
        !currentState ||
        !currentState.canPrompt ||
        currentState.busy ||
        !elements.prompt.value.trim();
    }
  });

  vscode.postMessage({ type: "ready" });
})();
