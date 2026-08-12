/* Orbit Runtime UI.
 *
 * Two rules shape everything below:
 *
 * 1. The server is the only authority on state. Nothing here advances a run,
 *    predicts a transition, or optimistically marks anything done — after a
 *    command the view refetches and renders whatever the server now says.
 * 2. Actions come from `allowed_commands[]`. There is no status-to-button map
 *    and no hardcoded mutation endpoint, so the UI cannot offer an action the
 *    actor is not allowed to take.
 */

import { Api, ApiError } from "./api.js";
import { I18n, LOCALES, preferredLocale } from "./i18n.js";
import { Router } from "./router.js";
import {
  acceptUnknownResultDialog, budgetDialog, cancelRunDialog, humanSubmitDialog, recoveryDialog,
  retryNodeDialog,
} from "./components/command-dialog.js";
import { dataState } from "./components/data-state.js";
import { installCustomSelects, syncCustomSelect } from "./components/custom-select.js";
import { el, svgEl } from "./components/dom.js";
import { createWorkflowDefinitionViews } from "./workflow/definition-views.js";
import { createViews } from "./views/index.js";

const api = new Api();
let i18n;
let router;
let route = { view: "home", runId: null };
let mayStartRun = false;
let shellFacts = null;
let rendering = false;
let renderQueued = false;
const TERMINAL_RUN_STATUSES = new Set(["succeeded", "failed", "cancelled"]);
// How much of one recorded value is rendered. Inline values are capped at
// 256 KB server-side; a page that pastes all of that stops responding.
const DATA_TEXT_LIMIT = 20_000;
const runtimeState = { stopped: false };
let views;

// Which Agents this Runtime can write DSL with. The server decides; an empty
// list means there is exactly one way and the choice is not worth offering.
function generationAgents() {
  const capability = shellFacts?.capabilities?.workflow_generation;
  return capability?.available ? capability.agents || [] : [];
}

/** The Agent this Runtime writes with when a request names none.
 *
 * The list is sorted for display, so its first entry is not the fallback the
 * server would actually use; the server names that one for us. */
function defaultGenerationAgent() {
  const agents = generationAgents();
  // A connected Agent App is already running in the user's current session,
  // so prefer it over spawning the Runtime's fallback CLI. App registrations
  // are live capabilities and disappear from this list when they disconnect.
  const connectedApp = agents.find((name) => name.startsWith("app:"));
  if (connectedApp) return connectedApp;
  const preferred = shellFacts?.capabilities?.workflow_generation?.default_agent;
  return agents.includes(preferred) ? preferred : agents[0] || "";
}


function generationAgentField(id, selected, onchange) {
  const agents = generationAgents();
  if (!agents.length) return null;
  if (agents.length === 1) return el("div", { class: "field" }, [
    el("span", { class: "field-label", text: i18n.t("generate.writtenBy") }),
    el("div", { class: "agent-choice-static mono", text: agents[0] }),
  ]);
  // No helper line: the field opens on the Agent the Runtime would use anyway,
  // so there is no "leave it unset" case left to explain.
  return el("div", { class: "field" }, [
    el("label", { for: id, text: i18n.t("generate.writtenBy") }),
    el("select", { id, onchange: (event) => onchange(event.target.value) },
      agents.map((name) => el("option", {
        value: name, text: name,
        ...(name === selected ? { selected: "selected" } : {}),
      }))),
  ]);
}

/* The step's name as the server gives it.
 *
 * The label is a definition field the generation contract requires, so the
 * client does not decide which node ids are "internal" and paper over them —
 * a definition without labels shows a neutral placeholder, which is a visible
 * problem rather than a hidden one. */
const pill = (status) =>
  el("span", { class: `pill ${status}`, text: i18n.status(status) });

/* The prototype's status language next to the pill: the dot scans, the pill
   spells the state out so meaning never rides on colour alone. */
const statusDot = (status) =>
  el("span", { class: `status-dot ${status}`, "aria-hidden": "true" });

function announce(message, kind = "info") {
  const region = document.getElementById("liveRegion");
  region.className = `banner ${kind}`;
  region.textContent = message;
  region.hidden = !message;
}

function reportError(error) {
  if (!(error instanceof ApiError)) throw error;
  announce(i18n.t(error.messageKey, { message: error.message }), "error");
  return error;
}

function workflowViews() {
  return createWorkflowDefinitionViews({ api, i18n, reportError });
}

/* ---------------------------------------------------------------- commands */

/** Render the buttons a responsibility advertises — and nothing else.
 *
 * `human.token` is advertised but deliberately not rendered: it exists for
 * the submit dialog, which uses it to fill its token field. A bare "Get
 * token" button on the row would hand out a credential with nowhere to put
 * it.
 */
function commandButtons(commands, onDone) {
  return commands
    .filter((allowed) => allowed.command !== "human.token")
    .map((allowed) =>
      el("button", {
        class: allowed.command === "run.cancel" ? "button danger" : "button",
        text: i18n.command(allowed),
        onclick: () => promptAndExecute(allowed, onDone, commands),
      }),
    );
}

/** Collect whatever the command's payload schema needs, then send it once. */
async function promptAndExecute(allowed, onDone, siblings = []) {
  const context = {
    api, el, i18n, reportError,
    retryAgents: shellFacts?.capabilities?.agent_handlers?.agents || [],
    // Server-stated, never inferred: on a single-operator Runtime the
    // approval token is minted and spent server-side.
    tokenRequired: shellFacts?.permissions?.human_token_required !== false,
  };
  let payload = {};
  if (allowed.payload_schema.startsWith("human-submit")) {
    payload = await humanSubmitDialog(context, allowed, siblings);
  } else if (allowed.payload_schema.startsWith("budget-add")) {
    payload = await budgetDialog(context);
  } else if (allowed.payload_schema.startsWith("run-cancel")) {
    payload = await cancelRunDialog(context);
  } else if (allowed.payload_schema.startsWith("recovery-apply")) {
    payload = await recoveryDialog(context, allowed);
  } else if (allowed.payload_schema.startsWith("node-retry")) {
    payload = await retryNodeDialog(context, allowed);
  } else if (allowed.payload_schema.startsWith("unknown-accept")) {
    payload = await acceptUnknownResultDialog(context, allowed);
  } else {
    announce(i18n.t("command.schemaUnsupported", { schema: allowed.payload_schema }), "error");
    return;
  }
  if (payload === null) return;

  announce(i18n.t("state.pending"), "info");
  try {
    const response = await api.execute(allowed, payload);
    const outcomes = response?.data?.results || [];
    const failed = outcomes.filter((item) => item.outcome !== "applied");
    await onDone();
    if (failed.length) {
      announce(i18n.t("command.partial", {
        failed: i18n.number(failed.length), total: i18n.number(outcomes.length),
      }), "error");
    } else {
      announce(i18n.t("command.accepted", { command: i18n.command(allowed) }));
    }
  } catch (error) {
    const failure = reportError(error);
    // A conflict is not a dead end: reload so the operator sees the state that
    // beat them, with fresh expected_versions to act on.
    if (failure.requiresRefresh) await onDone();
    return;
  }
}

/* ------------------------------------------------------------------- views */

/* ------------------------------------------------------------------- shell */

function navigate(next) {
  if (views && !views.canLeave()) return;
  route = next;
  router.navigate(next);
}

async function render() {
  // Old bookmarks to retired views land on the workspace instead of a 404.
  if (["ops", "settings", "inbox", "artifacts"].includes(route.view)) {
    navigate({ view: "home", runId: null });
    return;
  }
  // A render requested mid-flight is coalesced, not dropped: the state (or
  // locale) it reacted to is not in the in-flight paint.
  if (rendering) {
    renderQueued = true;
    return;
  }
  rendering = true;
  views?.cleanup();
  const root = document.getElementById("content");
  root.replaceChildren();
  root.append(dataState(el, i18n, "loading"));

  for (const button of document.querySelectorAll(".nav-button")) {
    const section = route.view === "run" || route.view === "goal" ? "home"
        : route.view === "workflow" || route.view === "workflowEdit"
          ? "workflows" : route.view;
    const active = button.dataset.view === section;
    if (active) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  }
  document.getElementById("viewTitle").textContent = i18n.t(
    route.view === "run" || route.view === "home" ? "simplified.title"
      : route.view === "goal" || route.view === "goals" ? "history.title"
        : route.view === "agents" ? "agents.title"
          : route.view === "workflow" || route.view === "workflowEdit"
            ? "workflows.title"
            : `${route.view}.title`,
  );

  try {
    // Agent App registrations are live rather than startup configuration.
    // Refresh capabilities with the view so a newly connected app can become
    // the initial writer without requiring a full browser reload.
    shellFacts = (await api.capabilities()).data;
    mayStartRun = Boolean(shellFacts.permissions && shellFacts.permissions.start_run);
    const singleAgentUi = shellFacts.product_mode?.workflow_ui_mode === "single-agent";
    const agentsNav = document.querySelector('[data-view="agents"]');
    if (agentsNav) agentsNav.hidden = singleAgentUi;
    const workflowsNav = document.querySelector('[data-view="workflows"]');
    if (workflowsNav) workflowsNav.hidden = singleAgentUi;
    if (singleAgentUi && ["agents", "workflows", "workflow"].includes(route.view)) {
      navigate({ view: "home", runId: null });
      return;
    }
    views.update({ i18n, shellFacts, mayStartRun });
    const fresh = el("div", { class: "content" });
    if (route.view === "home") await views.renderSimplifiedWorkspace(fresh);
    else if (route.view === "goal") await views.renderHistory(fresh, route.runId);
    else if (route.view === "goals") await views.renderHistory(fresh);
    else if (route.view === "workflows") await views.renderWorkflows(fresh);
    else if (route.view === "workflow") {
      // The catalog stays rendered (dimmed) behind the centred detail modal.
      await views.renderWorkflows(fresh);
      await views.openWorkflowModal(route.workflowId);
    }
    else if (route.view === "workflowEdit") {
      await views.renderWorkflowEdit(fresh, route.workflowId);
    }
    else if (route.view === "run") await views.renderSimplifiedWorkspace(fresh, route.runId);
    else if (route.view === "agents") await views.renderAgents(fresh);
    // The workspace is the one place runs are browsed.
    else await views.renderSimplifiedWorkspace(fresh);
    root.replaceChildren(...fresh.childNodes);
    views.refreshRuntimeCard();
  } catch (error) {
    // The failure lives where the data would have been, with a retry —
    // not only in the transient banner (plan P1 error state).
    root.replaceChildren(
      dataState(el, i18n, "error", {
        message: error instanceof ApiError
          ? i18n.t(error.messageKey, { message: error.message })
          : null,
        onRetry: () => render(),
      }),
    );
    reportError(error);
  } finally {
    rendering = false;
    if (renderQueued) {
      renderQueued = false;
      await render();
    }
  }
}

function applyStaticText() {
  document.documentElement.lang = i18n.locale;
  document.title = i18n.t("app.title");
  for (const node of document.querySelectorAll("[data-i18n]")) {
    node.textContent = i18n.t(node.dataset.i18n);
  }
  for (const node of document.querySelectorAll("[data-i18n-label]")) {
    node.setAttribute("aria-label", i18n.t(node.dataset.i18nLabel));
  }
  for (const node of document.querySelectorAll("[data-i18n-placeholder]")) {
    node.setAttribute("placeholder", i18n.t(node.dataset.i18nPlaceholder));
  }
  document.querySelectorAll("select[data-custom-select='true']").forEach(syncCustomSelect);
}


async function setLocale(locale) {
  i18n = await I18n.load(locale);
  i18n.persist();
  document.getElementById("localeSelect").value = locale;
  syncCustomSelect(document.getElementById("localeSelect"));
  applyStaticText();
  views.update({ i18n, shellFacts, mayStartRun });
  views.syncRefreshIntervalSelect(document.getElementById("refreshInterval"));
  await render();
  await views.refreshRuntimeCard();
}

function installMoreMenu() {
  const trigger = document.getElementById("moreButton");
  const menu = document.getElementById("moreMenu");
  if (!trigger || !menu) return;
  const setOpen = (open) => {
    menu.hidden = !open;
    trigger.setAttribute("aria-expanded", String(open));
  };
  trigger.addEventListener("click", (event) => {
    event.stopPropagation();
    setOpen(menu.hidden);
  });
  // A click inside the popover must not dismiss it — changing theme, language
  // or interval keeps the menu open. A click anywhere else closes it, and so
  // does Escape (which returns focus to the trigger).
  menu.addEventListener("click", (event) => event.stopPropagation());
  document.addEventListener("click", () => setOpen(false));
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !menu.hidden) {
      setOpen(false);
      trigger.focus();
    }
  });
}

function runtimeShutdownCommand() {
  return shellFacts?.runtime?.allowed_commands?.find(
    (allowed) => allowed.command === "runtime.shutdown",
  ) || null;
}

function openRuntimeShutdownDialog() {
  const allowed = runtimeShutdownCommand();
  if (!allowed) return;
  const titleId = `runtime-shutdown-${Date.now()}`;
  const dialog = el("dialog", {
    class: "workflow-delete-dialog", "aria-labelledby": titleId,
  });
  const cancel = el("button", {
    class: "button", type: "button", text: i18n.t("action.cancel"),
  });
  const stop = el("button", {
    class: "button danger", type: "submit",
    text: i18n.t("runtime.shutdown.action"),
  });
  cancel.addEventListener("click", () => dialog.close());
  const form = el("form", { method: "dialog" }, [
    el("h2", { id: titleId, text: i18n.t("runtime.shutdown.title") }),
    el("p", { text: i18n.t("runtime.shutdown.confirm") }),
    el("div", { class: "actions" }, [cancel, stop]),
  ]);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    cancel.disabled = true;
    stop.disabled = true;
    try {
      await api.execute(allowed, {}, "runtime.shutdown");
      runtimeState.stopped = true;
      views.stopPolling();
      dialog.close();
      document.getElementById("runtimeDot").classList.add("degraded");
      document.getElementById("runtimeCard").setAttribute(
        "aria-label", i18n.t("shell.runtime.stopped"),
      );
      document.getElementById("refresh").disabled = true;
      document.getElementById("shutdownRuntime").disabled = true;
      announce(i18n.t("runtime.shutdown.complete"));
    } catch (error) {
      cancel.disabled = false;
      stop.disabled = false;
      reportError(error);
    }
  });
  dialog.append(form);
  dialog.addEventListener("close", () => dialog.remove(), { once: true });
  document.body.append(dialog);
  dialog.showModal();
}

async function boot() {
  i18n = await I18n.load(preferredLocale());
  views = createViews({
    api, i18n, shellFacts, mayStartRun, runtimeState,
    render: (...args) => render(...args), navigate: (...args) => navigate(...args),
    announce, reportError, commandButtons, promptAndExecute, pill, statusDot,
    defaultGenerationAgent, generationAgentField, workflowViews,
    TERMINAL_RUN_STATUSES, DATA_TEXT_LIMIT,
  });
  router = new Router((next) => {
    route = next;
    render();
  });
  route = router.route;

  const select = document.getElementById("localeSelect");
  for (const locale of LOCALES) {
    const catalog = await I18n.load(locale);
    select.append(el("option", { value: locale, text: catalog.t("locale.name") }));
  }
  select.value = i18n.locale;
  select.addEventListener("change", (event) => setLocale(event.target.value));
  const refreshInterval = document.getElementById("refreshInterval");
  views.syncRefreshIntervalSelect(refreshInterval);
  refreshInterval.addEventListener("change", () => views.saveRefreshInterval(refreshInterval));
  installCustomSelects();

  const themeOptions = [...document.querySelectorAll(".theme-option")];
  const applyTheme = (value) => {
    document.documentElement.dataset.theme = value;
    for (const option of themeOptions) {
      const active = option.dataset.themeValue === value;
      option.classList.toggle("active", active);
      option.setAttribute("aria-pressed", String(active));
    }
  };
  for (const option of themeOptions) {
    option.addEventListener("click", () => {
      localStorage.setItem("orbit.theme", option.dataset.themeValue);
      applyTheme(option.dataset.themeValue);
    });
  }
  applyTheme(localStorage.getItem("orbit.theme") || "dark");
  installMoreMenu();

  try {
    shellFacts = (await api.capabilities()).data;
    mayStartRun = Boolean(shellFacts.permissions && shellFacts.permissions.start_run);
    const agentsNav = document.querySelector('[data-view="agents"]');
    if (agentsNav) {
      agentsNav.hidden = shellFacts.product_mode?.workflow_ui_mode === "single-agent";
    }
    const workflowsNav = document.querySelector('[data-view="workflows"]');
    if (workflowsNav) {
      workflowsNav.hidden = shellFacts.product_mode?.workflow_ui_mode === "single-agent";
    }
    views.update({ i18n, shellFacts, mayStartRun });
    const shutdown = runtimeShutdownCommand();
    document.getElementById("shutdownRuntime").hidden = !shutdown;
    document.getElementById("shutdownRuntime").addEventListener(
      "click", openRuntimeShutdownDialog,
    );
  } catch (error) {
    reportError(error);
  }
  // Permissions have been resolved, so every view now renders the command set
  // this actor really has. Views read it; automation waits on it.
  document.documentElement.dataset.shell = "ready";

  document.getElementById("refresh").addEventListener("click", () => render());
  window.addEventListener("orbit:refresh", () => render());
  views.scheduleLivePolling();
  for (const button of document.querySelectorAll(".nav-button")) {
    button.addEventListener("click", () => {
      // A message about the page you just left is noise on the next one.
      announce("");
      navigate({ view: button.dataset.view, runId: null });
    });
  }

  applyStaticText();
  // Awaited so the first paint already carries the runtime's own health word.
  // After applyStaticText: the static catalog must not overwrite the status.
  await views.refreshRuntimeCard();
  await render();
}

boot();
