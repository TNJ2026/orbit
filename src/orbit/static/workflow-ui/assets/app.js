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
  compatibleHandlers, diagnosticTarget, formatWorkflowSource, parseWorkflowSource,
  removeEdge, removeNode, removePolicy, replaceEdge, replaceMetadata, replaceNode, replacePolicy,
} from "./workflow-editor.js";
import {
  budgetDialog, cancelRunDialog, humanSubmitDialog, recoveryDialog,
} from "./components/command-dialog.js";
import { dataState } from "./components/data-state.js";

const api = new Api();
let i18n;
let router;
let route = { view: "home", runId: null };
let mayStartRun = false;
let shellFacts = null;
let liveCursor = null;
let refreshTimer = null;
let rendering = false;
let renderQueued = false;
let activeViewCleanup = null;
let activeViewLeaveGuard = null;
let customSelectSequence = 0;
const runFilters = { q: "", status: "", responsibility: "", activeOnly: false };
const goalFilters = { q: "", status: "" };
const artifactFilters = { q: "", runId: "", contentType: "" };

const el = (tag, props = {}, children = []) => {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2).toLowerCase(), value);
    else if (value !== null && value !== undefined) node.setAttribute(key, value);
  }
  for (const child of [].concat(children)) {
    if (child) node.append(child);
  }
  return node;
};

function syncCustomSelect(select) {
  const wrapper = select.closest(".custom-select");
  if (!wrapper) return;
  const selected = select.selectedOptions[0];
  const trigger = wrapper.querySelector(".custom-select-trigger");
  if (select.getAttribute("aria-label")) {
    select.dataset.customSelectLabel = select.getAttribute("aria-label");
  }
  trigger.setAttribute("aria-label", select.dataset.customSelectLabel || "");
  select.removeAttribute("aria-label");
  trigger.querySelector(".custom-select-value").textContent = selected?.textContent || "";
  trigger.disabled = select.disabled;
  for (const option of wrapper.querySelectorAll(".custom-select-option")) {
    const active = option.dataset.value === select.value;
    option.setAttribute("aria-selected", String(active));
    option.classList.toggle("selected", active);
  }
}

function enhanceSelect(select) {
  if (select.dataset.customSelect === "true") return;
  select.dataset.customSelect = "true";
  const label = select.getAttribute("aria-label")
    || select.labels?.[0]?.textContent.trim() || "";
  select.dataset.customSelectLabel = label;
  const listId = `custom-select-${customSelectSequence += 1}`;
  const wrapper = el("span", { class: "custom-select" });
  const trigger = el("button", {
    type: "button", class: "button custom-select-trigger",
    role: "combobox", "aria-haspopup": "listbox", "aria-expanded": "false",
    "aria-controls": listId, "aria-label": label,
  }, [
    el("span", { class: "custom-select-value" }),
    el("span", { class: "custom-select-chevron", "aria-hidden": "true", text: "⌄" }),
  ]);
  const list = el("span", {
    class: "custom-select-options", id: listId, role: "listbox",
    ...(label ? { "aria-label": label } : {}), hidden: "hidden",
  });

  const close = (restoreFocus = false) => {
    list.hidden = true;
    trigger.setAttribute("aria-expanded", "false");
    wrapper.classList.remove("open");
    if (restoreFocus) trigger.focus();
  };
  const open = (focusSelected = false) => {
    if (trigger.disabled) return;
    for (const other of document.querySelectorAll(".custom-select.open")) {
      if (other !== wrapper) {
        other.querySelector(".custom-select-options").hidden = true;
        other.querySelector(".custom-select-trigger").setAttribute("aria-expanded", "false");
        other.classList.remove("open");
      }
    }
    list.hidden = false;
    trigger.setAttribute("aria-expanded", "true");
    wrapper.classList.add("open");
    if (focusSelected) {
      (list.querySelector(".custom-select-option.selected") || list.firstElementChild)?.focus();
    }
  };

  for (const nativeOption of select.options) {
    const option = el("button", {
      type: "button", class: "custom-select-option", role: "option",
      "data-value": nativeOption.value,
      "aria-selected": String(nativeOption.selected),
      ...(nativeOption.disabled ? { disabled: "disabled" } : {}),
      text: nativeOption.textContent,
    });
    option.addEventListener("click", () => {
      select.value = option.dataset.value;
      close(true);
      syncCustomSelect(select);
      select.dispatchEvent(new Event("change", { bubbles: true }));
    });
    option.addEventListener("keydown", (event) => {
      const options = [...list.querySelectorAll(".custom-select-option:not(:disabled)")];
      const index = options.indexOf(option);
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        const delta = event.key === "ArrowDown" ? 1 : -1;
        options[(index + delta + options.length) % options.length]?.focus();
      } else if (event.key === "Home" || event.key === "End") {
        event.preventDefault();
        options[event.key === "Home" ? 0 : options.length - 1]?.focus();
      } else if (event.key === "Escape" || event.key === "Tab") {
        close(event.key === "Escape");
      }
    });
    list.append(option);
  }

  trigger.addEventListener("click", () => {
    if (list.hidden) open(false); else close(false);
  });
  trigger.addEventListener("keydown", (event) => {
    if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
      event.preventDefault();
      open(true);
    } else if (event.key === "Escape") close(false);
  });
  document.addEventListener("pointerdown", (event) => {
    if (!wrapper.contains(event.target)) close(false);
  });

  select.parentNode.insertBefore(wrapper, select);
  wrapper.append(select, trigger, list);
  select.classList.add("custom-select-native");
  select.hidden = true;
  select.setAttribute("tabindex", "-1");
  select.setAttribute("aria-hidden", "true");
  select.addEventListener("change", () => syncCustomSelect(select));
  syncCustomSelect(select);
}

function installCustomSelects() {
  document.querySelectorAll("select").forEach(enhanceSelect);
  const observer = new MutationObserver((records) => {
    for (const record of records) {
      for (const node of record.addedNodes) {
        if (!(node instanceof Element)) continue;
        if (node.matches("select")) enhanceSelect(node);
        node.querySelectorAll?.("select").forEach(enhanceSelect);
      }
    }
  });
  observer.observe(document.body, { childList: true, subtree: true });
}

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
  const context = { api, el, i18n, reportError };
  let payload = {};
  if (allowed.payload_schema.startsWith("human-submit")) {
    payload = await humanSubmitDialog(context, allowed, siblings);
  } else if (allowed.payload_schema.startsWith("budget-add")) {
    payload = await budgetDialog(context);
  } else if (allowed.payload_schema.startsWith("run-cancel")) {
    payload = await cancelRunDialog(context);
  } else if (allowed.payload_schema.startsWith("recovery-apply")) {
    payload = await recoveryDialog(context, allowed);
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

function runName(run) {
  return run.display_name || run.goal || run.run_id;
}

function waitText(run) {
  if (run.primary_responsibility) {
    return run.primary_responsibility.label
      || i18n.t(`responsibility.${run.primary_responsibility.kind}`);
  }
  if (run.wait_reason) return i18n.t(`wait.${run.wait_reason}`);
  return i18n.t("wait.none");
}

function statusSelect(value, onChange, labelKey = "runs.filter.status") {
  const select = el("select", { "aria-label": i18n.t(labelKey), onchange: onChange });
  for (const status of ["", "pending", "running", "waiting", "succeeded", "failed", "cancelled"]) {
    select.append(el("option", {
      value: status,
      ...(status === value ? { selected: "selected" } : {}),
      text: status ? i18n.status(status) : i18n.t("runs.filter.allStatuses"),
    }));
  }
  return select;
}

async function renderHome(root, selectedRunId = null) {
  const dashboard = (await api.dashboard()).data;
  const active = dashboard.active_goal;
  let responsibilities = [];
  let responsibilityError = null;
  if (active) {
    try {
      responsibilities = (await api.responsibilities(active.run_id)).data.responsibilities;
    } catch (error) {
      responsibilityError = error;
    }
  }
  const reload = () => render();
  root.append(
    el("section", { class: `home-hero panel${active ? " active-goal-hero" : " empty-goal-hero"}` }, [
      el("div", {}, [
        el("div", { class: "eyebrow", text: i18n.t(active ? "home.active.eyebrow" : "home.empty.eyebrow") }),
        active ? el("div", { class: "run-hero-title" }, [
          statusDot(active.status), el("h2", { text: runName(active) }), pill(active.status),
        ]) : el("h2", { text: i18n.t("home.empty.heading") }),
        el("p", { class: "muted", text: active
          ? `${active.workflow_id} · v${i18n.number(active.workflow_version)} · ${i18n.t("run.updated", { time: i18n.dateTime(active.updated_at) })}`
          : i18n.t("home.empty.description") }),
      ]),
      el("div", { class: "home-hero-actions" }, [
        active ? el("button", {
          class: "button primary", text: i18n.t("home.active.open"),
          onclick: () => navigate({ view: "run", runId: active.run_id }),
        }) : el("button", {
          class: "button", text: i18n.t("action.browseWorkflows"),
          onclick: () => navigate({ view: "workflows", runId: null }),
        }),
        mayStartRun && !active ? el("button", {
          class: "button primary", text: i18n.t("action.newGoal"), onclick: newRunDialog,
        }) : null,
        ...(active ? commandButtons(active.allowed_commands || [], reload) : []),
      ]),
    ]),
  );

  if (active) root.append(el("div", { class: "home-grid workbench-grid" }, [
    el("section", { class: "panel attention-panel current-step-panel" }, [
      el("div", { class: "panel-head" }, [
        el("div", {}, [
          el("div", { class: "panel-title", text: i18n.t("home.active.next") }),
          el("div", { class: "panel-subtitle", text: i18n.t("home.active.next.subtitle") }),
        ]),
        responsibilities.length ? el("span", { class: "pill waiting", text: i18n.number(responsibilities.length) }) : null,
      ]),
      el("div", { class: "panel-body attention-home-list" }, responsibilityError
        ? [dataState(el, i18n, "error", { onRetry: reload })]
        : responsibilities.length ? responsibilities.slice(0, 3).map((item) => {
          const kind = item.kind || "human";
          return el("div", { class: "home-attention-row workbench-action" }, [
            el("span", { class: `attention-symbol ${kind}`, text: kind === "budget" ? "$" : kind.slice(0, 1).toUpperCase() }),
            el("span", { class: "attention-copy" }, [
              el("strong", { text: item.label }),
              el("span", { class: "muted", text: item.detail || i18n.t(`responsibility.${kind}`) }),
            ]),
            el("span", { class: "actions" }, commandButtons(item.allowed_commands || [], reload)),
          ]);
        }) : [el("div", { class: "workbench-running" }, [
          el("span", { class: "live-dot", "aria-hidden": "true" }),
          el("div", {}, [
            el("strong", { text: i18n.t("home.active.running") }),
            el("p", { class: "muted", text: i18n.t("home.active.running.detail") }),
          ]),
        ])]),
    ]),
    el("section", { class: "panel" }, [
      el("div", { class: "panel-head" }, [
        el("div", {}, [
          el("div", { class: "panel-title", text: i18n.t("home.active.context") }),
          el("div", { class: "panel-subtitle", text: i18n.t("home.active.context.subtitle") }),
        ]),
      ]),
      el("div", { class: "panel-body workbench-facts" }, [
        el("div", {}, [el("span", { class: "muted", text: i18n.t("home.active.goal") }), el("strong", { text: active.goal || runName(active) })]),
        el("div", {}, [el("span", { class: "muted", text: i18n.t("home.active.wait") }), el("strong", { text: waitText(active) })]),
        el("div", {}, [el("span", { class: "muted", text: i18n.t("home.active.started") }), el("strong", { text: i18n.dateTime(active.created_at) })]),
      ]),
    ]),
  ]));

  const historySection = el("section", { class: "workspace-history" }, [
    el("div", { class: "section-heading" }, [
      el("div", {}, [
        el("div", { class: "eyebrow", text: i18n.t("home.history.eyebrow") }),
        el("h2", { text: i18n.t("home.history.heading") }),
        el("p", { class: "muted", text: i18n.t("home.history.description") }),
      ]),
    ]),
  ]);
  await renderGoals(historySection, selectedRunId);
  root.append(historySection);
}

async function renderGoals(root, selectedRunId = null) {
  const response = await api.listRuns({ limit: 25, q: goalFilters.q, status: goalFilters.status });
  const runs = response.data.runs;
  let selected = selectedRunId ? runs.find((item) => item.run_id === selectedRunId) : runs[0];
  if (selectedRunId && !selected) selected = (await api.runSummary(selectedRunId)).data;
  else if (selected) selected = (await api.runSummary(selected.run_id)).data;

  const search = el("input", {
    type: "search", value: goalFilters.q, placeholder: i18n.t("goals.search.placeholder"),
    "aria-label": i18n.t("goals.search.label"),
  });
  const filters = el("form", { class: "filter-bar", onsubmit: (event) => {
    event.preventDefault();
    goalFilters.q = search.value.trim();
    render();
  } }, [
    search,
    statusSelect(goalFilters.status, (event) => {
      goalFilters.status = event.target.value;
      render();
    }, "goals.filter.status"),
    el("button", { class: "button", type: "submit", text: i18n.t("action.search") }),
  ]);
  root.append(filters);

  const list = el("div", { class: "goal-list", "aria-label": i18n.t("goals.list") });
  for (const run of runs) {
    list.append(el("button", {
      class: `goal-row${run.run_id === selected?.run_id ? " selected" : ""}`,
      "aria-current": run.run_id === selected?.run_id ? "true" : null,
      onclick: () => navigate({ view: "goal", runId: run.run_id }),
    }, [
      el("span", { class: "goal-row-main" }, [
        el("strong", { class: "with-dot" }, [
          statusDot(run.status), el("span", { text: runName(run) }),
        ]),
        el("span", { class: "muted", text: waitText(run) }),
      ]),
      pill(run.status),
    ]));
  }
  if (!runs.length) list.append(el("div", { class: "empty", text: i18n.t("goals.empty") }));

  const detail = el("section", { class: "panel goal-detail" });
  if (!selected) {
    detail.append(el("div", { class: "empty", text: i18n.t("goals.select") }));
  } else {
    const budget = selected.budget_summary;
    const primary = selected.primary_responsibility;
    const budgetPercent = budget && budget.total_microunits > 0
      ? Math.min(100, Math.round((budget.consumed_microunits / budget.total_microunits) * 100)) : null;
    detail.append(
      el("div", { class: "panel-head goal-detail-head" }, [
        el("div", {}, [
          el("div", { class: "eyebrow", text: i18n.t("goals.detail") }),
          el("div", { class: "panel-title", text: runName(selected) }),
          el("div", { class: "goal-detail-id muted mono", text: selected.run_id }),
        ]),
        pill(selected.status),
      ]),
      el("div", { class: "panel-body" }, [
        el("p", { class: selected.goal ? "goal-copy" : "muted", text: selected.goal || i18n.t("goals.noDescription") }),
        el("section", { class: `goal-state-card${primary ? " waiting" : ""}` }, [
          el("div", { class: "eyebrow", text: i18n.t("goals.currentState") }),
          el("strong", { text: primary?.label || i18n.t(`status.${selected.status}`) }),
          el("span", { class: "muted", text: primary
            ? i18n.t("goals.currentState.waiting") : i18n.t("goals.currentState.clear") }),
        ]),
        el("div", { class: "goal-section-label eyebrow", text: i18n.t("goals.facts") }),
        el("dl", { class: "fact-grid" }, [
          el("div", {}, [el("dt", { text: i18n.t("run.workflow") }), el("dd", { text: `${selected.workflow_id} · v${selected.workflow_version}` })]),
          el("div", {}, [el("dt", { text: i18n.t("goals.waitingOn") }), el("dd", { text: waitText(selected) })]),
          el("div", {}, [el("dt", { text: i18n.t("runs.column.updated") }), el("dd", { text: i18n.dateTime(selected.updated_at) })]),
          el("div", {}, [el("dt", { text: i18n.t("goals.planVersion") }), el("dd", { text: selected.plan_version ? `v${i18n.number(selected.plan_version)}` : i18n.t("goals.planVersion.none") })]),
          budget ? el("div", {}, [
            el("dt", { text: i18n.t("run.budget") }),
            el("dd", { text: i18n.t("run.budget.used", {
              used: i18n.number(budget.consumed_microunits), total: i18n.number(budget.total_microunits), unit: budget.unit,
            }) }),
          ]) : null,
        ]),
        budgetPercent === null ? null : el("div", { class: "goal-budget" }, [
          el("div", { class: "goal-budget-label" }, [
            el("span", { text: i18n.t("goals.budgetUsage") }),
            el("strong", { text: `${i18n.number(budgetPercent)}%` }),
          ]),
          el("div", { class: "goal-progress-bar" }, [
            el("span", { style: `width:${budgetPercent}%` }),
          ]),
        ]),
        el("div", { class: "actions" }, [
          el("button", {
            class: "button primary", text: i18n.t("action.openRun"),
            onclick: () => navigate({ view: "run", runId: selected.run_id }),
          }),
        ]),
      ]),
    );
  }
  root.append(el("div", { class: "goals-layout" }, [list, detail]));
}

async function renderRuns(root) {
  const body = el("tbody");
  const table = el("table", { class: "runs-table" }, [
    el("thead", {}, [
      el("tr", {}, [
        el("th", { scope: "col", text: i18n.t("runs.column.run") }),
        el("th", { scope: "col", text: i18n.t("runs.column.workflow") }),
        el("th", { scope: "col", text: i18n.t("runs.column.status") }),
        el("th", { scope: "col", text: i18n.t("runs.column.responsibility") }),
        el("th", { scope: "col", text: i18n.t("runs.column.updated") }),
      ]),
    ]),
    body,
  ]);
  const search = el("input", {
    type: "search", value: runFilters.q,
    placeholder: i18n.t("runs.search.placeholder"), "aria-label": i18n.t("runs.search.label"),
  });
  const responsibility = el("select", {
    "aria-label": i18n.t("runs.filter.responsibility"), onchange: (event) => {
      runFilters.responsibility = event.target.value;
      render();
    },
  });
  // Recovery becomes selectable with API-3/P5's durable responsibility
  // projection. The frozen API-1 vocabulary accepts it now, but showing an
  // always-empty filter would promise a capability this deployment lacks.
  for (const value of ["", "human", "budget", "unknown"]) {
    responsibility.append(el("option", {
      value, ...(value === runFilters.responsibility ? { selected: "selected" } : {}),
      text: value ? i18n.t(`responsibility.${value}`) : i18n.t("runs.filter.allResponsibilities"),
    }));
  }
  root.append(el("form", { class: "filter-bar", onsubmit: (event) => {
    event.preventDefault();
    runFilters.q = search.value.trim();
    render();
  } }, [
    search,
    statusSelect(runFilters.status, (event) => {
      runFilters.status = event.target.value;
      render();
    }),
    responsibility,
    el("label", { class: "check-field" }, [
      el("input", {
        type: "checkbox", ...(runFilters.activeOnly ? { checked: "checked" } : {}),
        onchange: (event) => { runFilters.activeOnly = event.target.checked; render(); },
      }),
      el("span", { text: i18n.t("runs.activeOnly") }),
    ]),
    el("button", { class: "button", type: "submit", text: i18n.t("action.search") }),
  ]));
  const panel = el("section", { class: "panel" }, [
    el("div", { class: "panel-head" }, [
      el("div", { class: "panel-title", text: i18n.t("runs.title") }),
      el("span", { class: "muted", text: i18n.t("runs.orderHint") }),
    ]),
    el("div", { class: "table-scroll" }, [table]),
  ]);
  root.append(panel);

  let cursor = null;
  const more = el("button", { class: "button", text: i18n.t("action.loadMore") });

  const page = async () => {
    const response = await api.listRuns({ cursor, ...runFilters });
    for (const run of response.data.runs) {
      body.append(
        el("tr", { class: `list-option-row run-list-option${run.requires_actor_action ? " needs-attention" : ""}` }, [
          el("td", {
            "data-field": "run", "data-label": i18n.t("runs.column.run"),
          }, [
            el("button", {
              class: "list-option-link id-button",
              text: runName(run),
              title: run.run_id,
              onclick: () => navigate({ view: "run", runId: run.run_id }),
            }),
          ]),
          el("td", {
            "data-field": "workflow", "data-label": i18n.t("runs.column.workflow"),
            text: run.workflow_id,
          }),
          el("td", {
            "data-field": "status", "data-label": i18n.t("runs.column.status"),
          }, [
            el("span", { class: "with-dot" }, [statusDot(run.status), pill(run.status)]),
          ]),
          el("td", {
            "data-field": "responsibility",
            "data-label": i18n.t("runs.column.responsibility"),
            class: run.requires_actor_action ? "needs-action" : "", text: waitText(run),
          }),
          el("td", {
            "data-field": "updated", "data-label": i18n.t("runs.column.updated"),
            text: i18n.dateTime(run.updated_at),
          }),
        ]),
      );
    }
    cursor = response.next_cursor;
    moreWrap.hidden = !cursor;
    if (!body.children.length) {
      panel.append(el("div", { class: "empty", text: i18n.t("runs.empty") }));
    }
  };

  more.addEventListener("click", () => page().catch(reportError));
  // Hidden until a cursor exists: an always-rendered .panel-body reads as an
  // empty strip under the table.
  const moreWrap = el("div", { class: "panel-body" }, [more]);
  moreWrap.hidden = true;
  panel.append(moreWrap);
  await page();
}

async function renderRun(root, runId, activeTab = "overview") {
  let summary;
  try {
    summary = (await api.runSummary(runId)).data;
  } catch (error) {
    reportError(error);
    root.append(el("div", { class: "empty", text: i18n.t("run.notFound") }));
    return;
  }

  const reload = () => navigate({ view: "run", runId, tab: activeTab });
  const budget = summary.budget_summary;
  root.append(
    el("section", { class: "run-hero panel" }, [
      el("div", { class: "panel-head run-hero-head" }, [
        el("div", {}, [
          el("div", { class: "eyebrow", text: i18n.t("run.title") }),
          el("div", { class: "run-hero-title" }, [
            statusDot(summary.status),
            el("h2", { text: runName(summary) }),
          ]),
          el("div", { class: "mono muted", text: summary.run_id }),
        ]),
        pill(summary.status),
      ]),
      el("div", { class: "panel-body" }, [
        el("div", { class: "run-hero-meta" }, [
          el("span", { text: `${summary.workflow_id} · v${i18n.number(summary.workflow_version)}` }),
          el("span", { text: i18n.t("run.updated", { time: i18n.dateTime(summary.updated_at) }) }),
        ]),
      ]),
    ]),
  );

  let responsibilities = [];
  let responsibilitiesError = null;
  try {
    responsibilities = (await api.responsibilities(runId)).data.responsibilities;
  } catch (error) {
    responsibilitiesError = error;
  }
  root.append(whyPanel(summary, responsibilities, responsibilitiesError, budget, reload));

  const tabs = el("nav", { class: "run-tabs", "aria-label": i18n.t("run.tabs.label") });
  for (const tab of ["overview", "timeline", "plan", "graph", "data", "errors"]) {
    tabs.append(el("button", {
      class: `run-tab${tab === activeTab ? " active" : ""}`,
      "aria-current": tab === activeTab ? "page" : null,
      "data-run-tab": tab,
      text: i18n.t(`run.tab.${tab}`),
      onclick: () => navigate({ view: "run", runId, tab }),
    }));
  }
  root.append(tabs);

  const tabContent = el("section", { class: "run-tab-content", "data-active-tab": activeTab }, [
    dataState(el, i18n, "loading"),
  ]);
  root.append(tabContent);
  try {
    let content;
    if (activeTab === "overview") content = await overviewPanel(runId, summary, responsibilities);
    else if (activeTab === "plan") content = await planPanel(runId);
    else if (activeTab === "graph") content = await graphPanel(runId);
    else if (activeTab === "data") content = await dataPanel(runId);
    else if (activeTab === "timeline") content = await pagedPanel(runId, "timeline", "run.timeline", (item) =>
      el("div", { class: "timeline-item" }, [
        el("span", { class: "mono muted", text: i18n.dateTime(item.occurred_at) }),
        el("strong", { text: item.type }),
        el("span", { class: "mono muted", text: item.aggregate_id }),
      ]));
    else content = await pagedPanel(runId, "errors", "run.errors", errorItem);
    tabContent.replaceChildren(content);
  } catch (error) {
    tabContent.replaceChildren(dataState(el, i18n, "error", {
      message: error instanceof ApiError
        ? i18n.t(error.messageKey, { message: error.message }) : null,
      onRetry: reload,
    }));
    reportError(error);
  }
}

function whyPanel(summary, responsibilities, failure, budget, reload) {
  const responsibilityList = el("div", { class: "responsibility-list" }, [
    el("div", { class: "eyebrow", text: i18n.t("run.responsibilities") }),
  ]);
  if (failure) {
    responsibilityList.append(dataState(el, i18n, "error", { onRetry: reload }));
  } else if (!responsibilities.length) {
    const reason = summary.wait_reason
      ? i18n.t(`wait.${summary.wait_reason}`)
      : i18n.t(`run.why.${summary.status}`);
    responsibilityList.append(el("p", {
      class: "muted", text: reason,
    }));
  } else {
    for (const item of responsibilities) {
      responsibilityList.append(el("div", { class: "responsibility-row" }, [
        el("div", {}, [
          el("strong", { text: item.label }),
          el("div", { class: "muted mono", text: item.responsibility_id }),
        ]),
        pill(item.status),
        el("div", { class: "actions" }, commandButtons(item.allowed_commands, reload)),
      ]));
    }
  }
  return el("section", { class: "why-panel panel" }, [
    el("div", { class: "panel-head" }, [
      el("div", {}, [
        el("div", { class: "eyebrow", text: i18n.t("run.why.eyebrow") }),
        el("div", { class: "panel-title", text: i18n.t("run.why.title") }),
      ]),
    ]),
    el("div", { class: "panel-body why-grid" }, [responsibilityList, budgetView(budget)]),
  ]);
}

function budgetView(budget) {
  const view = el("div", { class: "budget-view" }, [
    el("div", { class: "eyebrow", text: i18n.t("run.budget") }),
  ]);
  if (!budget) {
    view.append(el("p", { class: "muted", text: i18n.t("run.budget.none") }));
    return view;
  }
  const total = Math.max(0, budget.total_microunits);
  const denominator = Math.max(total, budget.consumed_microunits + budget.reserved_microunits, 1);
  const width = (value) => `${Math.max(0, Math.min(100, (value / denominator) * 100))}%`;
  view.append(
    el("div", { class: "budget-bar", role: "img", "aria-label": i18n.t("run.budget.used", {
      used: i18n.number(budget.consumed_microunits), total: i18n.number(total), unit: budget.unit,
    }) }, [
      el("span", { class: "consumed", style: `width:${width(budget.consumed_microunits)}` }),
      el("span", { class: "reserved", style: `width:${width(budget.reserved_microunits)}` }),
    ]),
    el("div", { class: "budget-legend" }, [
      el("span", { text: i18n.t("run.budget.consumed", { value: i18n.number(budget.consumed_microunits) }) }),
      el("span", { text: i18n.t("run.budget.reserved", { value: i18n.number(budget.reserved_microunits) }) }),
      el("span", { text: i18n.t("run.budget.remaining", { value: i18n.number(budget.remaining_microunits) }) }),
    ]),
    el("div", { class: "muted", text: i18n.t("run.budget.used", {
      used: i18n.number(budget.consumed_microunits), total: i18n.number(total), unit: budget.unit,
    }) }),
    budget.overrun ? el("div", { class: "banner error", text: i18n.t("run.budget.overrun") }) : null,
  );
  return view;
}

async function overviewPanel(runId, summary, responsibilities) {
  const panel = el("section", { class: "panel" }, [
    el("div", { class: "panel-head" }, [
      el("div", { class: "panel-title", text: i18n.t("run.tab.overview") }),
    ]),
    el("div", { class: "panel-body" }, [
      el("p", { class: summary.goal ? "goal-copy" : "muted", text: summary.goal || i18n.t("goals.noDescription") }),
      el("dl", { class: "fact-grid" }, [
        el("div", {}, [el("dt", { text: i18n.t("run.workflow") }), el("dd", { class: "mono", text: summary.workflow_id })]),
        el("div", {}, [el("dt", { text: i18n.t("run.version") }), el("dd", { text: i18n.number(summary.workflow_version) })]),
        el("div", {}, [el("dt", { text: i18n.t("run.status") }), el("dd", {}, [pill(summary.status)])]),
        el("div", {}, [el("dt", { text: i18n.t("run.responsibilities") }), el("dd", { text: i18n.number(responsibilities.length) })]),
      ]),
    ]),
  ]);
  const [subflowResponse, foreachResponse] = await Promise.all([
    api.subflows(runId), api.foreachGroups(runId),
  ]);
  const links = subflowResponse.data.items;
  if (links.length) {
    const body = panel.querySelector(".panel-body");
    body.append(el("div", { class: "eyebrow", text: i18n.t("subflow.title") }));
    for (const link of links) {
      const isParent = link.parent_run_id === runId;
      const related = isParent ? link.child_run_id : link.parent_run_id;
      body.append(el("div", { class: "data-item" }, [
        el("div", { class: "actions" }, [
          pill(link.status),
          el("span", {
            class: "muted",
            text: i18n.t(isParent ? "subflow.child" : "subflow.parent"),
          }),
          el("button", {
            class: "button mono", text: related,
            onclick: () => navigate({ view: "run", runId: related }),
          }),
        ]),
        el("div", { class: "muted", text: i18n.t("subflow.depth", {
          depth: i18n.number(link.recursion_depth),
        }) }),
      ]));
    }
  }
  const groups = foreachResponse.data.items;
  if (groups.length) {
    const body = panel.querySelector(".panel-body");
    body.append(el("div", { class: "eyebrow", text: i18n.t("foreach.title") }));
    for (const group of groups) {
      const items = el("div", {});
      const loadItems = async () => {
        items.replaceChildren(el("div", { class: "muted", text: i18n.t("loading") }));
        try {
          const grid = el("div", {
            class: "virtual-window foreach-grid", role: "grid",
            "aria-label": i18n.t("foreach.items"),
          });
          const notice = el("div", { class: "banner info", hidden: "hidden" });
          const more = el("button", {
            class: "button", text: i18n.t("action.loadMore"),
          });
          let cursor = null;
          let rendered = 0;
          let omitted = 0;
          const nextPage = async () => {
            more.disabled = true;
            try {
              const response = await api.foreachItems(runId, group.group_id, cursor);
              for (const item of response.data.items) {
                grid.insertBefore(el("div", { class: "actions", role: "row" }, [
                  el("span", { class: "mono", role: "gridcell", text: item.item_key }),
                  el("span", { role: "gridcell" }, [pill(item.status)]),
                  ...(item.child_run_id ? [el("button", {
                    class: "button mono", role: "gridcell", text: item.child_run_id,
                    onclick: () => navigate({ view: "run", runId: item.child_run_id }),
                  })] : []),
                ]), more);
                rendered += 1;
              }
              while (rendered > 200) {
                const candidate = [...grid.children].find(
                  (child) => child !== notice && child !== more,
                );
                if (!candidate) break;
                candidate.remove();
                rendered -= 1;
                omitted += 1;
              }
              if (omitted) {
                notice.textContent = i18n.t("foreach.windowed", {
                  count: i18n.number(omitted),
                });
                notice.hidden = false;
              }
              cursor = response.next_cursor;
              more.hidden = !cursor;
            } finally {
              more.disabled = false;
            }
          };
          more.addEventListener("click", () => nextPage().catch(reportError));
          grid.append(notice, more);
          items.replaceChildren(grid);
          await nextPage();
        } catch (error) {
          items.replaceChildren(dataState(el, i18n, "error", { onRetry: loadItems }));
        }
      };
      body.append(el("div", { class: "data-item" }, [
        el("div", { class: "actions" }, [
          el("span", { class: "mono", text: group.group_id }),
          pill(group.status),
          el("span", { class: "muted", text: i18n.t("foreach.progress", {
            done: i18n.number(group.counts.succeeded + group.counts.failed),
            total: i18n.number(group.item_count),
          }) }),
          el("button", { class: "button", text: i18n.t("foreach.items"), onclick: loadItems }),
        ]),
        el("div", { class: "muted", text: i18n.t("foreach.policy", {
          policy: group.failure_policy,
          concurrency: i18n.number(group.concurrency_limit),
        }) }),
        items,
      ]));
    }
  }
  return panel;
}

function errorItem(item) {
  const error = (item.payload && item.payload.error) || {};
  const where = (item.payload && item.payload.node_run_id) || item.aggregate_id;
  return el("div", { class: "error-item" }, [
    el("strong", { text: error.message || error.code || item.type }),
    el("div", { class: "muted mono", text: [error.category, error.source, where].filter(Boolean).join(" · ") }),
    el("div", { class: "muted mono", text: i18n.dateTime(item.occurred_at) }),
  ]);
}

async function graphPanel(runId) {
  const graph = (await api.graph(runId)).data;
  const definition = graph.definition;
  const overlay = graph.runtime_overlay;
  const statuses = new Map();
  for (const node of overlay.nodes) {
    const current = statuses.get(node.node_id);
    if (!current || node.generation >= current.generation) statuses.set(node.node_id, node);
  }
  const positions = new Map(definition.layout.positions.map((item) => [item.node_id, item]));
  const maxDepth = Math.max(0, ...definition.layout.positions.map((item) => item.depth));
  const canvas = el("div", {
    class: `graph-canvas ${definition.layout.mode}`,
    style: `grid-template-columns:repeat(${maxDepth + 1},minmax(150px,1fr))`,
  });
  for (const node of definition.nodes) {
    const position = positions.get(node.node_id) || { depth: 0, lane: 0 };
    const runtime = statuses.get(node.node_id);
    canvas.append(el("article", {
      class: `graph-node${runtime ? ` ${runtime.status}` : ""}`,
      style: `grid-column:${position.depth + 1};grid-row:${position.lane + 1}`,
    }, [
      el("div", { class: "graph-node-head" }, [
        el("strong", { class: "mono", text: node.node_id }),
        runtime ? pill(runtime.status) : el("span", { class: "pill", text: i18n.t("graph.notStarted") }),
      ]),
      el("span", { class: "muted", text: node.kind }),
      runtime ? el("span", { class: "muted", text: i18n.t("plan.overlay.counts", {
        generation: i18n.number(runtime.generation), attempts: i18n.number(runtime.attempts),
      }) }) : null,
    ]));
  }
  return el("section", { class: "panel graph-panel" }, [
    el("div", { class: "panel-head" }, [
      el("div", {}, [
        el("div", { class: "panel-title", text: i18n.t("run.tab.graph") }),
        el("div", { class: "muted", text: i18n.t("graph.scopes", { version: graph.plan_version }) }),
      ]),
      el("span", { class: "pill", text: definition.layout.mode }),
    ]),
    el("div", { class: "panel-body graph-body" }, [
      canvas,
      el("div", { class: "graph-edges" }, definition.edges.map((edge) =>
        el("span", { class: "mono", text: `${edge.from} → ${edge.to}${edge.back_edge ? ` · ${i18n.t("graph.loop")}` : ""}` }),
      )),
      el("div", { class: "graph-facts" }, [
        el("span", { text: i18n.t("graph.branches", { count: i18n.number(overlay.branch_tokens.length) }) }),
        el("span", { text: i18n.t("graph.joins", { count: i18n.number(overlay.join_groups.length) }) }),
        el("span", { text: i18n.t("graph.counters", { count: i18n.number(overlay.control_counters.length) }) }),
      ]),
    ]),
  ]);
}

async function dataPanel(runId) {
  return pagedPanel(runId, "data", "run.data", (item) => {
    const lineage = el("div", { class: "muted mono", hidden: "hidden" });
    const button = el("button", {
      class: "button",
      text: i18n.t("run.data.lineage"),
      onclick: async () => {
        try {
          const response = await api.lineage(runId, item.data_id);
          const links = response.data.links;
          lineage.textContent = links.length
            ? links.map((link) => `${link.type}: ${link.source_id} → ${link.target_id}`).join(" · ")
            : i18n.t("run.data.lineage.empty");
          lineage.hidden = false;
        } catch (error) {
          reportError(error);
        }
      },
    });
    const rawValue = item.kind === "value" && item.value !== null
      ? JSON.stringify(item.value)
      : null;
    const value = rawValue === null
      ? `${item.content_type || item.schema_id} · ${i18n.number(item.size_bytes)} B`
      : rawValue.length <= 500
        ? rawValue
        : `${rawValue.slice(0, 500)}… · ${i18n.number(item.size_bytes)} B`;
    return el("div", { class: "data-item" }, [
      el("div", { class: "actions" }, [
        el("span", { class: "mono", text: item.data_id }),
        el("span", { class: "pill", text: i18n.t(`run.data.kind.${item.kind}`) }),
        button,
      ]),
      el("div", { text: `${item.port_id}: ${value}` }),
      el("div", { class: "muted mono", text: item.checksum }),
      lineage,
    ]);
  });
}

/** The plan, in three separately-labelled views.
 *
 * Definition, overlay and diff are fetched and rendered apart, and the overlay
 * is only drawn against the plan version it names. Painting last version's
 * statuses onto this version's graph is the bug this shape prevents; showing
 * "no run state for this version" is the correct, honest alternative.
 */
async function planPanel(runId) {
  const body = el("div", { class: "panel-body" });
  const panel = el("section", { class: "panel" }, [
    el("div", { class: "panel-head" }, [
      el("div", { class: "panel-title", text: i18n.t("plan.title") }),
    ]),
    body,
  ]);

  let definition;
  try {
    definition = (await api.planDefinition(runId)).data;
  } catch (error) {
    if (!(error instanceof ApiError) || error.status !== 404) throw error;
    body.append(el("div", { class: "muted", text: i18n.t("plan.none") }));
    return panel;
  }

  const versions = definition.available_versions || [definition.plan_version];
  const state = { version: definition.plan_version, view: "definition", asOf: null };

  const tabs = el("div", { class: "actions" });
  const content = el("div", {});

  const draw = async () => {
    content.replaceChildren(el("div", { class: "muted", text: i18n.t("loading") }));
    for (const button of tabs.querySelectorAll("button[data-view]")) {
      button.setAttribute("aria-pressed", String(button.dataset.view === state.view));
    }
    try {
      if (state.view === "definition") content.replaceChildren(await planDefinitionView(runId, state));
      else if (state.view === "overlay") content.replaceChildren(await planOverlayView(runId, state));
      else if (state.view === "diff") content.replaceChildren(await planDiffView(runId, state, versions));
      else content.replaceChildren(await plannerDecisionsView(runId));
    } catch (error) {
      content.replaceChildren();
      reportError(error);
    }
  };
  state.redraw = draw;

  for (const view of ["definition", "overlay", "diff", "decisions"]) {
    if (view === "diff" && versions.length < 2) continue;
    tabs.append(
      el("button", {
        class: "button",
        "data-view": view,
        "aria-pressed": String(view === state.view),
        text: i18n.t(`plan.${view}`),
        onclick: () => {
          state.view = view;
          draw();
        },
      }),
    );
  }

  if (versions.length > 1) {
    const select = el("select", { "aria-label": i18n.t("plan.version") });
    for (const version of versions) {
      select.append(
        el("option", {
          value: String(version),
          ...(version === state.version ? { selected: "selected" } : {}),
          text: `v${version}`,
        }),
      );
    }
    select.addEventListener("change", (event) => {
      state.version = Number(event.target.value);
      draw();
    });
    tabs.append(select);
  }

  body.append(tabs, content);
  await draw();
  return panel;
}

async function plannerDecisionsView(runId) {
  const response = await api.plannerDecisions(runId);
  const items = response.data.items;
  const list = el("div", {}, [
    el("div", { class: "eyebrow", text: i18n.t("plan.decisions.title") }),
  ]);
  if (!items.length) {
    list.append(el("div", { class: "muted", text: i18n.t("plan.decisions.empty") }));
    return list;
  }
  for (const item of items) {
    const proposal = item.proposal;
    const patch = item.patch;
    const policy = item.policy;
    list.append(el("div", { class: "data-item" }, [
      el("div", { class: "actions" }, [
        el("span", { class: "mono", text: `#${item.attempt_number}` }),
        pill(item.status),
        el("span", { class: "muted", text: `${item.provider_id} · ${item.model_id}` }),
        ...(item.usage ? [el("span", {
          class: "muted",
          text: i18n.t("plan.decisions.cost", {
            cost: i18n.number(item.usage.cost_microunits),
          }),
        })] : []),
      ]),
      ...(proposal ? [
        el("div", { class: "actions" }, [
          el("span", { class: "mono", text: proposal.proposal_id }),
          pill(proposal.action.kind),
          pill(proposal.status),
        ]),
        el("div", { text: proposal.reason }),
      ] : []),
      ...(patch ? [el("div", {
        class: "muted mono",
        text: i18n.t("plan.decisions.patch", {
          status: patch.status,
          version: patch.result_plan_version ?? "—",
        }),
      })] : []),
      ...(policy ? [el("div", {
        class: "muted",
        text: i18n.t(
          policy.allowed ? "plan.decisions.policy.allowed" : "plan.decisions.policy.denied",
        ),
      })] : []),
    ]));
  }
  return list;
}

async function planDefinitionView(runId, state) {
  const definition = (await api.planDefinition(runId, state.version)).data;
  const list = el("div", {}, [
    el("div", {
      class: "eyebrow",
      text: i18n.t("plan.definition.version", { version: definition.plan_version }),
    }),
  ]);
  for (const node of definition.nodes) {
    list.append(
      el("div", { class: "actions" }, [
        el("span", { class: "mono", text: node.node_id }),
        el("span", { class: "muted", text: node.kind }),
        el("span", {
          class: "muted mono",
          text: node.handler_name ? `${node.handler_name}@${node.handler_version}` : "",
        }),
      ]),
    );
  }
  list.append(
    el("div", {
      class: "muted",
      text: definition.edges.map((edge) => `${edge.from} → ${edge.to}`).join("   "),
    }),
  );
  return list;
}

async function planOverlayView(runId, state) {
  const overlay = (await api.planOverlay(runId, state.version, state.asOf)).data;
  const position = el("input", {
    type: "number", min: "0", inputmode: "numeric",
    value: state.asOf === null ? "" : String(state.asOf),
    placeholder: i18n.t("plan.overlay.history.position"),
    "aria-label": i18n.t("plan.overlay.history.position"),
  });
  const historyControls = el("div", { class: "actions" }, [
    position,
    el("button", {
      class: "button", text: i18n.t("plan.overlay.history.apply"),
      onclick: () => {
        if (!position.value.length || Number(position.value) < 0) return;
        state.asOf = Number(position.value);
        state.redraw();
      },
    }),
    el("button", {
      class: "button", text: i18n.t("plan.overlay.history.current"),
      onclick: () => { state.asOf = null; state.redraw(); },
    }),
  ]);
  const list = el("div", {}, [
    el("div", {
      class: "eyebrow",
      text: i18n.t("plan.overlay.for", { version: overlay.plan_version }),
    }),
    historyControls,
    ...(overlay.as_of_global_position === null ? [] : [el("div", {
      class: "muted mono",
      text: i18n.t("plan.overlay.history.asOf", {
        position: i18n.number(overlay.as_of_global_position),
        head: i18n.number(overlay.event_head),
      }),
    })]),
  ]);
  if (!overlay.nodes.length) {
    list.append(el("div", { class: "muted", text: i18n.t("plan.overlay.empty") }));
    return list;
  }
  for (const node of overlay.nodes) {
    list.append(
      el("div", { class: "actions" }, [
        el("span", { class: "mono", text: node.node_id }),
        pill(node.status),
        el("span", {
          class: "muted",
          text: i18n.t("plan.overlay.counts", {
            generation: i18n.number(node.generation),
            attempts: i18n.number(node.attempts),
          }),
        }),
      ]),
    );
  }
  return list;
}

async function planDiffView(runId, state, versions) {
  const base = versions[versions.indexOf(state.version) - 1] ?? versions[0];
  const diff = (await api.planDiff(runId, base, state.version)).data;
  const list = el("div", {}, [
    el("div", {
      class: "eyebrow",
      text: i18n.t("plan.diff.between", {
        base: diff.base_version, target: diff.target_version,
      }),
    }),
  ]);
  if (diff.identical) {
    list.append(el("div", { class: "muted", text: i18n.t("plan.diff.identical") }));
    return list;
  }
  const rows = [
    ["plan.diff.added", diff.added_nodes],
    ["plan.diff.removed", diff.removed_nodes],
    ["plan.diff.changed", diff.changed_nodes.map((node) => node.node_id)],
  ];
  for (const [key, values] of rows) {
    if (!values.length) continue;
    list.append(
      el("div", { class: "actions" }, [
        el("span", { class: "muted", text: i18n.t(key) }),
        el("span", { class: "mono", text: values.join(", ") }),
      ]),
    );
  }
  return list;
}

/** A cursor-paged section. Paging is the server's; the UI only carries tokens. */
async function pagedPanel(runId, kind, titleKey, renderItem) {
  // Keep a bounded scroll window: cursor pages can be arbitrarily large, but
  // the DOM remains capped while the server remains the source of truth.
  const body = el("div", { class: "panel-body virtual-window", role: "log" });
  const more = el("button", { class: "button", text: i18n.t("action.loadMore") });
  const windowNotice = el("div", { class: "banner info", hidden: "hidden" });
  let cursor = null;
  let rendered = 0;
  let omitted = 0;

  const page = async () => {
    const response = await api.runPage(runId, kind, cursor);
    for (const item of response.data.items) {
      body.insertBefore(renderItem(item), more);
      rendered += 1;
    }
    while (rendered > 200) {
      const candidate = [...body.children].find(
        (child) => child !== windowNotice && child !== more,
      );
      if (!candidate) break;
      candidate.remove();
      rendered -= 1;
      omitted += 1;
    }
    if (omitted) {
      windowNotice.textContent = i18n.t("run.windowed", { count: i18n.number(omitted) });
      windowNotice.hidden = false;
    }
    cursor = response.next_cursor;
    more.hidden = !cursor;
    if (rendered === 0) {
      body.insertBefore(el("div", { class: "muted", text: i18n.t(`${titleKey}.empty`) }), more);
    }
  };

  more.addEventListener("click", () => page().catch(reportError));
  body.append(windowNotice, more);
  const panel = el("section", { class: "panel" }, [
    el("div", { class: "panel-head" }, [
      el("div", { class: "panel-title", text: i18n.t(titleKey) }),
    ]),
    body,
  ]);
  await page();
  return panel;
}

async function renderInbox(root) {
  const response = await api.inbox();
  const items = response.data.items;
  const body = el("tbody");
  for (const item of items) {
    body.append(inboxRow(item));
  }
  let cursor = response.next_cursor;
  const more = el("button", {
    class: "button", text: i18n.t("action.loadMore"),
    ...(cursor ? {} : { hidden: "hidden" }),
    onclick: async () => {
      const next = await api.inbox(cursor);
      for (const item of next.data.items) body.append(inboxRow(item));
      cursor = next.next_cursor;
      more.hidden = !cursor;
    },
  });
  const panel = el("section", { class: "panel" }, [
    el("div", { class: "panel-head" }, [
      el("div", { class: "panel-title", text: i18n.t("inbox.title") }),
    ]),
    items.length
      ? el("div", { class: "table-scroll" }, [
          el("table", { class: "inbox-table" }, [
            el("thead", {}, [
              el("tr", {}, [
                el("th", { text: i18n.t("inbox.column.item") }),
                el("th", { text: i18n.t("inbox.column.run") }),
                el("th", { text: i18n.t("inbox.column.status") }),
                el("th", { text: i18n.t("inbox.column.actions") }),
              ]),
            ]),
            body,
          ]),
        ])
      : el("div", { class: "empty", text: i18n.t("inbox.empty") }),
    more,
  ]);
  root.append(panel);
  const count = response.data.action_count || 0;
  document.getElementById("inboxCount").textContent = count ? String(count) : "";
}

function inboxRow(item) {
  const glyphs = { human: "H", budget: "$", unknown: "?", recovery: "R" };
  return el("tr", { class: "list-option-row inbox-list-option" }, [
        el("td", {
          class: "inbox-item-copy", "data-field": "item",
          "data-label": i18n.t("inbox.column.item"),
        }, [
          el("div", { class: "actions inbox-item-head" }, [
            el("span", {
              class: `inbox-kind ${item.kind}`, "aria-hidden": "true",
              text: glyphs[item.kind] || "•",
            }),
            el("span", { class: "pill", text: i18n.t(`responsibility.${item.kind}`) }),
            el("strong", { text: item.label }),
          ]),
          item.deadline_at ? el("div", { class: "muted", text: i18n.t("inbox.deadline", {
            time: i18n.dateTime(item.deadline_at),
          }) }) : null,
          item.quorum ? el("div", { class: "muted", text: i18n.t("inbox.quorum", {
            submitted: i18n.number(item.quorum.submitted), required: i18n.number(item.quorum.count),
          }) }) : null,
          el("div", { class: "muted mono", text: i18n.t("inbox.source", {
            source: item.item_id,
          }) }),
        ]),
        el("td", { "data-field": "run", "data-label": i18n.t("inbox.column.run") }, [
          el("button", {
            class: "list-option-link id-button",
            text: item.run_id,
            title: item.run_id,
            onclick: () => navigate({ view: "run", runId: item.run_id }),
          }),
        ]),
        el("td", { "data-field": "status", "data-label": i18n.t("inbox.column.status") }, [pill(item.status)]),
        el("td", { "data-field": "actions", "data-label": i18n.t("inbox.column.actions") }, [
          el("div", { class: "actions" }, commandButtons(item.allowed_commands, () => render())),
        ]),
      ]);
}

async function refreshInboxCount() {
  try {
    const response = await api.inbox();
    const count = response.data.action_count || 0;
    document.getElementById("inboxCount").textContent = count ? String(count) : "";
  } catch {
    // A badge is supplementary; the destination keeps its own error boundary.
  }
}

/* Sidebar health card: the same facts `/health/ready` serves, nothing more.
   A failed fetch means "degraded" — the card never claims a state the
   runtime did not report. */
async function refreshRuntimeCard() {
  const dot = document.getElementById("runtimeDot");
  const status = document.getElementById("runtimeStatus");
  const detail = document.getElementById("runtimeDetail");
  let health = null;
  try {
    health = await api.health();
  } catch {
    health = null;
  }
  const ready = Boolean(health && health.ok && health.status === "ready");
  dot.classList.toggle("degraded", !ready);
  status.textContent = i18n.t(ready ? "shell.runtime.healthy" : "shell.runtime.degraded");
  const components = health?.checks?.components?.detail;
  detail.textContent = Array.isArray(components)
    ? i18n.t("shell.runtime.components", { count: i18n.number(components.length) })
    : "";
}

async function renderArtifacts(root, selectedArtifactId = null) {
  const search = el("input", {
    type: "search", value: artifactFilters.q,
    placeholder: i18n.t("artifacts.search.placeholder"),
    "aria-label": i18n.t("artifacts.search.label"),
  });
  const run = el("input", {
    value: artifactFilters.runId, placeholder: i18n.t("artifacts.filter.run"),
    "aria-label": i18n.t("artifacts.filter.run"),
  });
  const type = el("input", {
    value: artifactFilters.contentType,
    placeholder: i18n.t("artifacts.filter.contentType"),
    "aria-label": i18n.t("artifacts.filter.contentType"),
  });
  root.append(el("form", { class: "filter-bar", onsubmit: (event) => {
    event.preventDefault();
    artifactFilters.q = search.value.trim();
    artifactFilters.runId = run.value.trim();
    artifactFilters.contentType = type.value.trim();
    render();
  } }, [
    search, run, type,
    el("button", { class: "button", type: "submit", text: i18n.t("action.search") }),
  ]));

  const grid = el("section", { class: "artifact-grid", "aria-label": i18n.t("artifacts.list") });
  let cursor = null;
  const more = el("button", { class: "button", text: i18n.t("action.loadMore") });
  const load = async () => {
    const response = await api.artifacts({ cursor, ...artifactFilters });
    for (const item of response.data.artifacts) {
      grid.append(artifactCard(item, item.artifact_id === selectedArtifactId));
    }
    cursor = response.next_cursor;
    more.hidden = !cursor;
    if (!grid.children.length) {
      grid.append(el("div", { class: "empty panel", text: i18n.t("artifacts.empty") }));
    }
  };
  more.addEventListener("click", () => load().catch(reportError));
  root.append(grid, more);
  await load();
  if (selectedArtifactId) await renderArtifactDetail(root, selectedArtifactId);
}

function artifactCard(item, selected = false) {
  const contentType = item.content_type || "application/octet-stream";
  const subtype = contentType.split("/").pop() || "file";
  const fileType = subtype === "plain" ? "TXT"
    : subtype === "json" ? "JSON"
      : subtype.replace(/^x-/, "").slice(0, 4).toUpperCase();
  return el("article", { class: `artifact-card panel list-option-card${selected ? " selected" : ""}` }, [
    el("button", {
      class: "artifact-card-main",
      "aria-current": selected ? "true" : null,
      onclick: () => navigate({ view: "artifact", artifactId: item.artifact_id, runId: null }),
    }, [
      el("span", { class: "artifact-top" }, [
        el("span", { class: "file-icon", "aria-hidden": "true", text: fileType }),
        el("span", { class: "artifact-size", text: i18n.t("artifacts.size", {
          size: i18n.number(item.size_bytes),
        }) }),
      ]),
      el("strong", { class: "artifact-name", text: item.output_port_id }),
      el("span", { class: "artifact-meta muted", text: `${item.workflow_id} · ${item.producer_id}` }),
      el("span", { class: "artifact-id muted mono", text: item.artifact_id }),
      el("span", { class: "artifact-flow", "aria-hidden": "true" }, [
        el("span", { text: item.run_id }),
        el("b", { text: "→" }),
        el("span", { text: item.output_port_id }),
      ]),
    ]),
  ]);
}

async function renderArtifactDetail(root, artifactId) {
  const panel = el("section", { class: "panel artifact-detail" }, [dataState(el, i18n, "loading")]);
  root.append(panel);
  try {
    const [detailResponse, lineageResponse] = await Promise.all([
      api.artifact(artifactId), api.artifactLineage(artifactId),
    ]);
    const item = detailResponse.data;
    const lineage = lineageResponse.data;
    const preview = el("pre", { class: "artifact-preview", hidden: "hidden" });
    const links = [
      ...lineage.producers, ...lineage.consumers, ...lineage.derived_from,
    ];
    panel.replaceChildren(
      el("div", { class: "panel-head" }, [
        el("div", {}, [
          el("div", { class: "eyebrow", text: i18n.t("artifacts.detail") }),
          el("div", { class: "panel-title mono", text: item.artifact_id }),
        ]),
        el("button", {
          class: "button", text: i18n.t("action.close"),
          onclick: () => navigate({ view: "artifacts", runId: null }),
        }),
      ]),
      el("div", { class: "panel-body" }, [
        el("dl", { class: "fact-grid" }, [
          el("div", {}, [el("dt", { text: i18n.t("artifacts.run") }), el("dd", { class: "mono", text: item.run_id })]),
          el("div", {}, [el("dt", { text: i18n.t("artifacts.type") }), el("dd", { text: item.content_type })]),
          el("div", {}, [el("dt", { text: i18n.t("artifacts.sizeLabel") }), el("dd", { text: i18n.number(item.size_bytes) })]),
          el("div", {}, [el("dt", { text: i18n.t("artifacts.producer") }), el("dd", { class: "mono", text: item.producer_id })]),
        ]),
        el("div", { class: "actions" }, [
          item.previewable ? el("button", {
            class: "button", text: i18n.t("artifacts.preview"),
            onclick: async () => {
              try {
                preview.textContent = await api.artifactPreview(item.artifact_id);
                preview.hidden = false;
              } catch (error) { reportError(error); }
            },
          }) : null,
          el("a", {
            class: "button", href: api.artifactDownloadUrl(item.artifact_id),
            text: i18n.t("artifacts.download"), download: "",
          }),
        ]),
        preview,
        el("div", { class: "eyebrow", text: i18n.t("artifacts.lineage") }),
        ...(links.length ? links.map((link) => el("div", {
          class: "lineage-row mono", text: `${link.type}: ${link.source_id} → ${link.target_id}`,
        })) : [el("div", { class: "muted", text: i18n.t("artifacts.lineage.empty") })]),
      ]),
    );
  } catch (error) {
    panel.replaceChildren(dataState(el, i18n, "error", {
      message: error instanceof ApiError
        ? i18n.t(error.messageKey, { message: error.message }) : null,
      onRetry: () => renderArtifactDetail(root, artifactId),
    }));
    reportError(error);
  }
}

async function renderOps(root) {
  const [statusResponse, recoveryResponse] = await Promise.all([
    api.opsStatus(), api.recovery(),
  ]);
  const status = statusResponse.data;
  const recovery = recoveryResponse.data;

  root.append(
    el("section", { class: "panel" }, [
      el("div", { class: "panel-head" }, [
        el("div", { class: "panel-title", text: i18n.t("ops.integrity") }),
        pill(status.integrity.status === "ok" ? "succeeded" : "failed"),
      ]),
      el("div", { class: "panel-body" }, [
        el("div", { text: i18n.t("ops.integrity.summary", {
          version: i18n.number(status.integrity.migration_version),
        }) }),
      ]),
    ]),
  );

  const findings = recovery.findings;
  root.append(
    el("section", { class: "panel" }, [
      el("div", { class: "panel-head" }, [
        el("div", { class: "panel-title", text: i18n.t("ops.recovery") }),
      ]),
      el("div", { class: "panel-body" }, [
        el("div", {
          class: "muted",
          text: i18n.t("ops.recovery.scanned", {
            count: i18n.number(recovery.scanned_runs),
          }),
        }),
        ...(findings.length
          ? findings.map((finding) =>
              el("div", { class: "actions" }, [
                el("span", { class: "mono", text: `${finding.code} · ${finding.entity_id}` }),
                ...commandButtons(finding.allowed_commands || [], () => render()),
              ]),
            )
          : [el("div", { class: "muted", text: i18n.t("ops.recovery.empty") })]),
      ]),
    ]),
  );

  root.append(el("section", { class: "stat-grid" }, [
    el("article", { class: "stat-card" }, [
      el("div", { class: "panel-title", text: i18n.t("ops.capacity") }),
      el("div", { class: "stat-value", text: i18n.number(status.capacity.ready_jobs) }),
      el("div", { class: "muted", text: i18n.t("ops.capacity.ready") }),
      el("div", { class: "muted", text: i18n.t("ops.capacity.workers", {
        count: i18n.number(status.capacity.configured_workers || 0),
      }) }),
    ]),
    el("article", { class: "stat-card" }, [
      el("div", { class: "panel-title", text: i18n.t("ops.durable") }),
      el("div", { class: "stat-value", text: i18n.number(status.durable.active_leases) }),
      el("div", { class: "muted", text: i18n.t("ops.durable.leases") }),
      el("div", { class: "muted", text: i18n.t("ops.durable.unknown", {
        count: i18n.number(status.durable.unknown_external_results),
      }) }),
    ]),
  ]));
}

async function renderAgents(root) {
  const catalog = (await api.handlerCatalog()).data;
  root.append(el("div", { class: "banner info", text: i18n.t("agents.registrationOnly") }));
  root.append(el("section", { class: "panel" }, [
    el("div", { class: "panel-head" }, [
      el("div", { class: "panel-title", text: i18n.t("agents.handlers") }),
    ]),
    el("div", { class: "panel-body agents-grid" }, catalog.handlers.length
      ? catalog.handlers.map((handler) => {
        const attempt = handler.recent_attempt;
        const initials = handler.name.replace(/^handler:/, "").slice(0, 2).toUpperCase();
        return el("article", { class: "data-card list-option-card agent-card" }, [
          el("div", { class: "agent-head" }, [
            el("span", { class: "agent-avatar", "aria-hidden": "true", text: initials }),
            el("div", {}, [
              el("div", { class: "panel-title mono", text: `${handler.name} ${handler.version}` }),
              el("div", { class: "muted", text: i18n.t("agents.registered") }),
            ]),
          ]),
          (handler.capabilities || []).length
            ? el("div", { class: "capabilities" }, handler.capabilities.map((capability) =>
                el("span", { class: "capability", text: capability })))
            : el("div", { class: "muted", text: i18n.t("agents.noCapabilities") }),
          el("div", { class: "muted mono", text: attempt
            ? `${attempt.status} · ${attempt.run_id} · ${i18n.dateTime(attempt.occurred_at)}`
            : i18n.t("agents.noAttempts") }),
        ]);
      })
      : [el("div", { class: "muted", text: i18n.t("agents.empty") })]),
  ]));
  root.append(el("section", { class: "panel" }, [
    el("div", { class: "panel-head" }, [
      el("div", { class: "panel-title", text: i18n.t("agents.discovered") }),
    ]),
    el("div", { class: "panel-body" }, catalog.agents.length
      ? catalog.agents.map((agent) =>
        el("div", { class: "mono", text: `${agent.name} ${agent.version}` }))
      : [el("div", { class: "muted", text: i18n.t("agents.discovered.empty") })]),
  ]));
}

function refreshSeconds() {
  const value = Number(localStorage.getItem("orbit.refreshSeconds") || 15);
  return Number.isFinite(value) && value >= 5 && value <= 300 ? value : 15;
}

function scheduleLivePolling() {
  // A timeout chain rather than an interval, so failures can back off:
  // doubling up to five minutes instead of repainting the error banner every
  // tick of an outage. The first failure is announced; repeats stay quiet
  // until a success resets the cadence.
  if (refreshTimer) clearTimeout(refreshTimer);
  let failures = 0;
  const delaySeconds = () =>
    Math.min(300, refreshSeconds() * 2 ** failures);
  const tick = async () => {
    if (!document.hidden && !rendering && !document.querySelector("dialog[open]")) {
      try {
        const live = (await api.live(liveCursor)).data;
        liveCursor = live.cursor;
        failures = 0;
        // An Editor owns unsaved local text. Background projection changes
        // must never tear down that view; explicit Draft commands redraw it.
        if (live.changed && route.view !== "workflowEdit") await render();
      } catch (error) {
        // Programming errors must stay loud; only transport failures back off.
        if (!(error instanceof ApiError)) throw error;
        failures += 1;
        if (failures === 1) reportError(error);
      }
    }
    refreshTimer = setTimeout(tick, delaySeconds() * 1000);
  };
  refreshTimer = setTimeout(tick, delaySeconds() * 1000);
}

async function renderSettings(root) {
  const status = shellFacts?.permissions?.ops_read ? (await api.opsStatus()).data : null;
  const interval = el("select", { "aria-label": i18n.t("settings.refresh") });
  for (const seconds of [5, 15, 30, 60, 300]) interval.append(el("option", {
    value: String(seconds), text: i18n.t("settings.seconds", { count: seconds }),
    ...(seconds === refreshSeconds() ? { selected: "selected" } : {}),
  }));
  interval.addEventListener("change", () => {
    localStorage.setItem("orbit.refreshSeconds", interval.value);
    scheduleLivePolling();
    announce(i18n.t("settings.saved"));
  });
  root.append(el("section", { class: "panel" }, [
    el("div", { class: "panel-head" }, [el("div", {
      class: "panel-title", text: i18n.t("settings.preferences"),
    })]),
    el("div", { class: "panel-body" }, [
      el("label", { class: "settings-row" }, [
        el("span", { text: i18n.t("settings.refresh") }), interval,
      ]),
      el("div", { class: "muted", text: i18n.t("settings.localOnly") }),
    ]),
  ]));
  root.append(el("section", { class: "panel" }, [
    el("div", { class: "panel-head" }, [el("div", {
      class: "panel-title", text: i18n.t("settings.server"),
    })]),
    el("div", { class: "panel-body mono", text: status
      ? i18n.t("settings.server.summary", {
        workers: status.server_config.worker_count,
        poll: status.server_config.poll_seconds,
        artifacts: String(status.server_config.artifact_store_configured),
      })
      : i18n.t("settings.server.restricted") }),
  ]));
}

/* ------------------------------------------------ workflow catalog / wizard */

async function renderWorkflows(root) {
  const catalog = (await api.workflowCatalog()).data;
  const entries = catalog.workflows;
  // Generation appears only when the server advertised it: capability off or
  // read-only actor simply means the button does not exist.
  const generateCommand = (catalog.allowed_commands || []).find(
    (item) => item.command === "workflow.generate",
  );
  root.append(el("header", { class: "view-intro" }, [
    el("div", {}, [
      el("div", { class: "eyebrow", text: i18n.t("workflows.eyebrow") }),
      el("h2", { text: i18n.t("workflows.heading") }),
      el("p", { class: "muted", text: i18n.t("workflows.description") }),
    ]),
    generateCommand ? el("button", {
        class: "button primary", id: "generateWorkflow",
        text: i18n.t("generate.action"),
        onclick: () => generateWorkflowDialog(generateCommand),
      }) : null,
  ]));
  const cards = el("section", { class: "workflow-grid", "aria-label": i18n.t("workflows.list") });
  const detail = el("section", { class: "panel workflow-detail" }, [
    el("div", { class: "empty", text: i18n.t("workflows.select") }),
  ]);

  const goToDraft = (workflowId, draftId) => navigate({
    view: "workflowEdit", workflowId, draftId, runId: null,
  });

  const resolveDraftCollision = (failure, create, value) => {
    const existing = failure.details.draft;
    const dialog = el("dialog", {
      class: "command-dialog", "aria-label": i18n.t("editor.activeDraftTitle"),
    });
    const close = () => { dialog.close(); dialog.remove(); };
    dialog.append(el("div", { class: "dialog-body" }, [
      el("h3", { text: i18n.t("editor.activeDraftTitle") }),
      el("p", { class: "muted", text: i18n.t("editor.activeDraftBody", {
        active: existing.base_version, requested: value.selected_version,
      }) }),
      el("div", { class: "actions" }, [
        el("button", {
          type: "button", class: "button", text: i18n.t("action.cancel"), onclick: close,
        }),
        el("button", {
          type: "button", class: "button", id: "continueActiveDraft",
          text: i18n.t("editor.continueActiveDraft"),
          onclick: () => { close(); goToDraft(value.workflow_id, existing.draft_id); },
        }),
        el("button", {
          type: "button", class: "button danger", id: "replaceActiveDraft",
          text: i18n.t("editor.discardCreateVersion", { version: value.selected_version }),
          onclick: async (event) => {
            event.currentTarget.disabled = true;
            try {
              const current = (await api.workflowDraft(existing.draft_id)).data;
              const discard = current.allowed_commands.find(
                (item) => item.command === "workflow.draft.discard",
              );
              if (!discard) return;
              await api.execute(
                discard, {}, `workflow.draft.discard:${existing.draft_id}:replace`,
              );
              const next = (await api.execute(
                create, { base_version: value.selected_version },
                `workflow.draft.create:${value.workflow_id}:${value.selected_version}:replace`,
              )).data;
              close();
              goToDraft(value.workflow_id, next.draft_id);
            } catch (error) {
              event.currentTarget.disabled = false;
              reportError(error);
            }
          },
        }),
      ]),
    ]));
    document.body.append(dialog);
    dialog.addEventListener("close", () => dialog.remove(), { once: true });
    dialog.showModal();
  };

  let selectedCard = null;
  const showDetail = async (entry, card = null, requestedVersion = undefined) => {
    if (selectedCard) {
      selectedCard.classList.remove("selected");
      selectedCard.querySelector(".workflow-card-main")?.removeAttribute("aria-current");
    }
    selectedCard = card || cards.querySelector(`[data-workflow-id="${CSS.escape(entry.workflow_id)}"]`);
    if (selectedCard) {
      selectedCard.classList.add("selected");
      selectedCard.querySelector(".workflow-card-main")?.setAttribute("aria-current", "true");
    }
    detail.replaceChildren(dataState(el, i18n, "loading"));
    try {
      const value = (await api.workflowDetail(entry.workflow_id, requestedVersion)).data;
      const definition = value.definition;
      const versionSelect = el("select", {
        id: "workflowVersionSelect", "aria-label": i18n.t("workflows.versionHistory"),
      }, value.versions.map((version) => el("option", {
        value: String(version.version),
        ...(version.version === value.selected_version ? { selected: "selected" } : {}),
        text: i18n.t("workflows.versionOption", {
          version: i18n.number(version.version),
          date: i18n.dateTime(version.created_at),
        }),
      })));
      versionSelect.addEventListener("change", () => {
        showDetail(entry, selectedCard, Number(versionSelect.value));
      });
      detail.replaceChildren(
        el("div", { class: "panel-head" }, [
          el("div", {}, [
            el("div", { class: "eyebrow", text: `${value.workflow_id} · v${value.selected_version}` }),
            el("div", { class: "panel-title", text: value.name }),
          ]),
          el("div", { class: "actions" }, [
            (() => {
              const create = value.allowed_commands.find(
                (item) => item.command === "workflow.draft.create",
              );
              return create ? el("button", {
                class: "button", id: "editWorkflow",
                text: i18n.t("editor.edit"),
                onclick: async () => {
                  try {
                    const draft = (await api.execute(
                      create, { base_version: value.selected_version },
                      `workflow.draft.create:${value.workflow_id}:${value.selected_version}`,
                    )).data;
                    goToDraft(value.workflow_id, draft.draft_id);
                  } catch (error) {
                    if (error instanceof ApiError && error.code === "draft_already_active") {
                      resolveDraftCollision(error, create, value);
                      return;
                    }
                    reportError(error);
                  }
                },
              }) : null;
            })(),
            value.allowed_commands.some((item) => item.command === "run.start")
              ? el("button", {
                  class: "button primary", text: i18n.t("action.newGoal"),
                  onclick: () => newRunDialog(value.workflow_id, value.selected_version),
                })
              : null,
          ]),
        ]),
        el("div", { class: "panel-body" }, [
          el("div", { class: "workflow-version-picker" }, [
            el("div", { class: "field" }, [
              el("label", { text: i18n.t("workflows.versionHistory") }), versionSelect,
            ]),
            value.selected_version === value.latest_version
              ? el("span", { class: "pill succeeded", text: i18n.t("workflows.latestVersion") })
              : el("span", { class: "pill", text: i18n.t("workflows.historicalVersion") }),
          ]),
          el("p", { class: value.description ? "" : "muted", text: value.description || i18n.t("workflows.noDescription") }),
          el("dl", { class: "fact-grid" }, [
            el("div", {}, [el("dt", { text: i18n.t("workflows.nodes") }), el("dd", { text: i18n.number(value.summary.node_count) })]),
            el("div", {}, [el("dt", { text: i18n.t("workflows.inputs") }), el("dd", { text: i18n.number(value.inputs.length) })]),
          ]),
          el("div", { class: "eyebrow", text: i18n.t("workflows.definition") }),
          el("div", { class: "definition-list" }, definition.nodes.map((node) =>
            el("div", { class: "actions" }, [
              el("span", { class: "mono", text: node.id }),
              el("span", { class: "pill", text: node.kind }),
              node.handler ? el("span", { class: "muted mono", text: `${node.handler.name}@${node.handler.version}` }) : null,
            ]),
          )),
        ]),
      );
    } catch (error) {
      detail.replaceChildren(dataState(el, i18n, "error", { onRetry: () => showDetail(entry) }));
      reportError(error);
    }
  };

  for (const entry of entries) {
    const kinds = Object.entries(entry.summary.node_kinds || {});
    const visualNodes = [];
    for (const [kind, count] of kinds) {
      for (let index = 0; index < Math.min(count, 4 - visualNodes.length); index += 1) {
        visualNodes.push(el("span", {
          class: `workflow-node ${kind}`, title: kind,
          text: kind === "terminal" ? "✓" : kind === "human" ? "H" : kind === "decision" ? "?" : kind.slice(0, 1).toUpperCase(),
        }));
      }
      if (visualNodes.length === 4) break;
    }
    if (entry.summary.node_count > visualNodes.length) visualNodes.push(el("span", {
      class: "workflow-node more", text: `+${entry.summary.node_count - visualNodes.length}`,
    }));
    const card = el("article", {
      class: "workflow-card panel", "data-workflow-id": entry.workflow_id,
    }, [
      el("button", { class: "workflow-card-main" }, [
        el("span", { class: "workflow-visual", "aria-hidden": "true" }, visualNodes),
        el("span", { class: "eyebrow", text: `${entry.workflow_id} · v${entry.latest_version}` }),
        el("strong", { text: entry.name }),
        el("span", { class: "muted", text: entry.description || i18n.t("workflows.noDescription") }),
        el("span", { class: "workflow-meta", text: i18n.t("workflows.summary", {
          nodes: i18n.number(entry.summary.node_count), inputs: i18n.number(entry.inputs.length),
        }) }),
      ]),
      entry.allowed_commands.length ? el("button", {
        class: "button", text: i18n.t("action.newGoal"),
        onclick: () => newRunDialog(entry.workflow_id),
      }) : null,
    ]);
    card.querySelector(".workflow-card-main").addEventListener("click", () => showDetail(entry, card));
    cards.append(card);
  }
  if (!entries.length) cards.append(el("div", { class: "empty panel", text: i18n.t("workflows.empty") }));
  if (entries.length) await showDetail(entries[0]);
  root.append(el("div", { class: "workflows-layout" }, [cards, detail]));
}

/* --------------------------------------------------------- workflow editor */

/** Persistent Workflow Editor (editor plan P3).
 *
 * Structured forms only produce candidate JSON. Save, validation, revision
 * and the command set remain server-owned; a candidate is applied through the
 * advertised save command and immediately checked by the production compiler.
 */
async function renderWorkflowEditor(root, draftId) {
  const [draftResponse, catalogResponse] = await Promise.all([
    api.workflowDraft(draftId),
    api.handlerCatalog().catch(() => ({ data: { handlers: [] } })),
  ]);
  let draft = draftResponse.data;
  const handlers = catalogResponse.data.handlers || [];

  const source = el("textarea", {
    id: "draftSource", class: "draft-source", spellcheck: "false",
    "aria-label": i18n.t("editor.source"),
  });
  source.value = draft.source;
  const saveState = el("span", {
    class: "pill", id: "draftSaveState", role: "status", "aria-live": "polite",
  });
  const conflict = el("div", { class: "banner error", hidden: "hidden", role: "alert" });
  const controls = el("div", { class: "actions", id: "draftControls" });
  const diagnostics = el("div", { class: "panel-body", id: "draftDiagnostics" });
  const editorNav = el("nav", {
    class: "editor-nav", role: "tablist", "aria-label": i18n.t("editor.sections"),
  });
  const editorMain = el("div", { class: "editor-main" });
  const outlinePane = el("section", { class: "editor-pane", "data-editor-pane": "outline" });
  const metadataPane = el("section", { class: "editor-pane", "data-editor-pane": "metadata", hidden: "hidden" });
  const nodesPane = el("section", { class: "editor-pane", "data-editor-pane": "nodes", hidden: "hidden" });
  const edgesPane = el("section", { class: "editor-pane", "data-editor-pane": "edges", hidden: "hidden" });
  const policiesPane = el("section", { class: "editor-pane", "data-editor-pane": "policies", hidden: "hidden" });
  const sourcePane = el("section", { class: "editor-pane", "data-editor-pane": "source", hidden: "hidden" }, [source]);
  const draftFacts = el("footer", { class: "editor-facts mono" });

  let dirtySinceSave = false;
  let saving = false;
  let saveTimer = null;
  let lastCommandFailure = null;
  let selectedNode = 0;
  let selectedEdge = 0;
  let selectedPolicy = 0;
  let activePane = "outline";

  const command = (name) =>
    (draft.allowed_commands || []).find((item) => item.command === name);

  const setSaveState = (key) => {
    saveState.textContent = i18n.t(`editor.state.${key}`);
    saveState.className = `pill ${key === "conflict" ? "failed" : key === "saved" ? "succeeded" : "waiting"}`;
  };

  const copyText = async (value) => {
    try {
      await navigator.clipboard.writeText(value);
    } catch {
      const fallback = el("textarea", { readonly: "readonly" });
      fallback.value = value;
      document.body.append(fallback);
      fallback.select();
      document.execCommand("copy");
      fallback.remove();
    }
    announce(i18n.t("editor.copied"));
  };

  const showConflict = async (error) => {
    const localSource = source.value;
    conflict.replaceChildren(
      el("span", { text: i18n.t("editor.conflict", { message: error.message }) }),
      el("button", {
        type: "button", class: "button", id: "draftCopyLocal",
        text: i18n.t("editor.copyLocal"), onclick: () => copyText(localSource),
      }),
      el("button", {
        type: "button", class: "button", id: "draftReload",
        text: i18n.t("editor.reload"),
        onclick: async () => {
          draft = (await api.workflowDraft(draft.draft_id)).data;
          source.value = draft.source;
          dirtySinceSave = false;
          conflict.hidden = true;
          setSaveState("saved");
          redraw();
          drawStructured();
        },
      }),
    );
    conflict.hidden = false;
    setSaveState("conflict");
    try {
      const remote = error.code === "workflow_version_conflict"
        ? (await api.workflowDetail(draft.workflow_id, error.details.latest_version)).data
        : (await api.workflowDraft(draft.draft_id)).data;
      if (remote.source !== undefined && remote.source !== localSource) {
        const local = el("textarea", { readonly: "readonly", class: "mono" });
        local.value = localSource;
        const server = el("textarea", { readonly: "readonly", class: "mono" });
        server.value = remote.source || "";
        const comparison = el("details", { class: "editor-conflict-compare" }, [
          el("summary", { text: i18n.t("editor.compareConflict") }),
          el("div", { class: "editor-compare-grid" }, [
            field(i18n.t("editor.localDraft"), local),
            field(error.code === "workflow_version_conflict"
              ? i18n.t("editor.latestPublished", { version: error.details.latest_version })
              : i18n.t("editor.serverDraft"), server),
          ]),
        ]);
        conflict.append(comparison);
        if (error.code === "workflow_version_conflict") {
          conflict.append(
            el("button", {
              type: "button", class: "button", text: i18n.t("editor.viewLatest"),
              onclick: () => { comparison.open = true; comparison.scrollIntoView({ block: "nearest" }); },
            }),
            el("button", {
              type: "button", class: "button danger", id: "draftRebaseLatest",
              text: i18n.t("editor.discardCreateLatest"),
              onclick: async () => {
                const discard = command("workflow.draft.discard");
                const create = remote.allowed_commands?.find(
                  (item) => item.command === "workflow.draft.create",
                );
                if (!discard || !create) return;
                const removed = await api.execute(
                  discard, {}, `workflow.draft.discard:${draft.draft_id}:rebase`,
                );
                if (!removed) return;
                const next = await api.execute(
                  create, { base_version: remote.selected_version },
                  `workflow.draft.create:${draft.workflow_id}:${remote.selected_version}`,
                );
                navigate({
                  view: "workflowEdit", workflowId: draft.workflow_id,
                  draftId: next.data.draft_id, runId: null,
                });
              },
            }),
          );
        }
      }
    } catch {
      // The conflict itself remains actionable even if the comparison read
      // races with another update or the connection drops.
    }
  };

  const runCommand = async (name, payload = {}, intent) => {
    const allowed = command(name);
    if (!allowed) return null;
    try {
      lastCommandFailure = null;
      const response = await api.execute(allowed, payload, intent);
      draft = response.data;
      conflict.hidden = true;
      redraw();
      return response;
    } catch (error) {
      const failure = reportError(error);
      lastCommandFailure = failure;
      if (failure.requiresRefresh) showConflict(failure);
      return null;
    }
  };

  const save = async () => {
    if (saving || !command("workflow.draft.save")) return null;
    saving = true;
    const submittedSource = source.value;
    setSaveState("saving");
    const response = await runCommand(
      "workflow.draft.save", { source: submittedSource },
      `workflow.draft.save:${draft.draft_id}:${Date.now()}`,
    );
    saving = false;
    if (response) {
      dirtySinceSave = source.value !== submittedSource;
      setSaveState(dirtySinceSave ? "unsaved" : "saved");
    } else {
      dirtySinceSave = true;
      if (lastCommandFailure?.status === 0) setSaveState("offline");
      else if (!lastCommandFailure?.requiresRefresh) setSaveState("unsaved");
    }
    return response;
  };

  source.addEventListener("input", () => {
    dirtySinceSave = true;
    setSaveState("unsaved");
    if (saveTimer) clearTimeout(saveTimer);
    saveTimer = setTimeout(save, 800);
  });

  const applyCandidate = async (candidate) => {
    if (saveTimer) clearTimeout(saveTimer);
    source.value = formatWorkflowSource(candidate);
    dirtySinceSave = true;
    setSaveState("unsaved");
    const saved = await save();
    if (!saved) return;
    const checked = await runCommand(
      "workflow.draft.validate", {},
      `workflow.draft.validate:${draft.draft_id}:${draft.revision}`,
    );
    if (checked) drawStructured();
  };

  const focusSource = (line, column) => {
    showPane("source");
    const lines = source.value.split("\n");
    const row = Math.max(0, Math.min(lines.length - 1, line - 1));
    const offset = lines.slice(0, row).reduce((total, value) => total + value.length + 1, 0);
    const position = Math.min(source.value.length, offset + Math.max(0, column - 1));
    source.focus();
    source.setSelectionRange(position, position);
  };

  const locateDiagnostic = (item, target) => {
    if (target.pane === "nodes" && target.index !== null) selectedNode = target.index;
    if (target.pane === "edges" && target.index !== null) selectedEdge = target.index;
    if (target.pane === "policies" && target.index !== null) selectedPolicy = target.index;
    if (["nodes", "edges", "policies"].includes(target.pane)) {
      drawStructured();
      showPane(target.pane);
    } else {
      focusSource(target.line, target.column);
    }
    announce(i18n.t("editor.locatedDiagnostic", { code: item.code, path: target.path }));
  };

  const field = (label, control) => el("div", { class: "field" }, [
    el("label", { text: label }), control,
  ]);

  const paneButton = (name, key) => {
    const paneId = `draft-pane-${name}`;
    const tabId = `draft-tab-${name}`;
    const button = el("button", {
      type: "button", class: "editor-nav-item", id: tabId,
      role: "tab", tabindex: "-1", "aria-controls": paneId,
      "aria-selected": "false", "data-editor-tab": name,
      text: i18n.t(key),
      onclick: () => showPane(name),
    });
    const pane = {
      outline: outlinePane, metadata: metadataPane, nodes: nodesPane,
      edges: edgesPane, policies: policiesPane, source: sourcePane,
    }[name];
    pane.id = paneId;
    pane.setAttribute("role", "tabpanel");
    pane.setAttribute("aria-labelledby", tabId);
    button.addEventListener("keydown", (event) => {
      const tabs = [...editorNav.querySelectorAll("[role='tab']")];
      const index = tabs.indexOf(button);
      let next = null;
      if (event.key === "ArrowRight") next = (index + 1) % tabs.length;
      if (event.key === "ArrowLeft") next = (index - 1 + tabs.length) % tabs.length;
      if (event.key === "Home") next = 0;
      if (event.key === "End") next = tabs.length - 1;
      if (next === null) return;
      event.preventDefault();
      showPane(tabs[next].dataset.editorTab);
      tabs[next].focus();
    });
    editorNav.append(button);
  };

  const showPane = (name) => {
    activePane = name;
    for (const pane of editorMain.querySelectorAll("[data-editor-pane]")) {
      pane.hidden = pane.dataset.editorPane !== name;
    }
    for (const button of editorNav.querySelectorAll("[data-editor-tab]")) {
      const active = button.dataset.editorTab === name;
      button.classList.toggle("active", active);
      button.setAttribute("aria-selected", String(active));
      button.setAttribute("tabindex", active ? "0" : "-1");
    }
  };

  paneButton("outline", "editor.outline");
  paneButton("metadata", "editor.metadata");
  paneButton("nodes", "editor.nodes");
  paneButton("edges", "editor.edges");
  paneButton("policies", "editor.policies");
  paneButton("source", "editor.sourceTab");

  const renderPortEditor = (title, ports, onChange) => {
    const body = el("div", { class: "editor-port-list" });
    const repaint = () => {
      body.replaceChildren();
      ports.forEach((port, index) => {
        const id = el("input", { value: port.id || "", "aria-label": i18n.t("editor.portId") });
        const schema = el("input", { value: port.schema_id || "", "aria-label": i18n.t("editor.schemaId") });
        id.addEventListener("input", () => { port.id = id.value; onChange(); });
        schema.addEventListener("input", () => { port.schema_id = schema.value; onChange(); });
        body.append(el("div", { class: "editor-port-row" }, [
          id, schema,
          el("button", {
            type: "button", class: "button danger", text: i18n.t("editor.remove"),
            onclick: () => { ports.splice(index, 1); onChange(); repaint(); },
          }),
        ]));
      });
      body.append(el("button", {
        type: "button", class: "button", text: i18n.t("editor.addPort"),
        onclick: () => { ports.push({ id: "port", schema_id: "schema://object/1.0" }); onChange(); repaint(); },
      }));
    };
    repaint();
    return el("fieldset", { class: "editor-fieldset" }, [
      el("legend", { text: title }), body,
    ]);
  };

  const drawStructured = () => {
    let document;
    try {
      document = parseWorkflowSource(source.value);
    } catch (error) {
      const unavailable = el("div", {
        class: "banner error", text: i18n.t("editor.structuredUnavailable", { message: error.message }),
      });
      outlinePane.replaceChildren(unavailable);
      metadataPane.replaceChildren(unavailable.cloneNode(true));
      nodesPane.replaceChildren(unavailable.cloneNode(true));
      edgesPane.replaceChildren(unavailable.cloneNode(true));
      policiesPane.replaceChildren(unavailable.cloneNode(true));
      return;
    }

    outlinePane.replaceChildren(
      el("div", { class: "editor-section-head" }, [
        el("div", {}, [
          el("div", { class: "panel-title", text: document.metadata.name }),
          el("div", { class: "muted", text: document.metadata.description || i18n.t("workflows.noDescription") }),
        ]),
      ]),
      el("div", { class: "stat-grid editor-stats" }, [
        el("article", { class: "stat-card" }, [
          el("div", { class: "stat-value", text: i18n.number(document.nodes.length) }),
          el("div", { class: "muted", text: i18n.t("editor.nodes") }),
        ]),
        el("article", { class: "stat-card" }, [
          el("div", { class: "stat-value", text: i18n.number((document.edges || []).length) }),
          el("div", { class: "muted", text: i18n.t("editor.edges") }),
        ]),
        el("article", { class: "stat-card" }, [
          el("div", { class: "stat-value", text: i18n.number((document.policies || []).length) }),
          el("div", { class: "muted", text: i18n.t("editor.policies") }),
        ]),
      ]),
      el("div", { class: "editor-outline-list" }, document.nodes.map((node, index) =>
        el("button", {
          type: "button", class: "list-option-card editor-outline-node",
          onclick: () => { selectedNode = index; drawStructured(); showPane("nodes"); },
        }, [
          el("strong", { class: "mono", text: node.id }),
          el("span", { class: "pill", text: node.kind }),
          el("span", { class: "muted mono", text: node.handler
            ? `${node.handler.name}@${node.handler.version}` : i18n.t("editor.controller") }),
        ])),
      ),
    );

    const name = el("input", { id: "draftMetadataName", value: document.metadata.name });
    const description = el("textarea", { id: "draftMetadataDescription" });
    description.value = document.metadata.description || "";
    const labels = el("textarea", { id: "draftMetadataLabels", class: "mono" });
    labels.value = JSON.stringify(document.metadata.labels || {}, null, 2);
    const metadataError = el("div", { class: "banner error", hidden: "hidden", role: "alert" });
    metadataPane.replaceChildren(
      el("div", { class: "editor-section-head" }, [
        el("div", { class: "panel-title", text: i18n.t("editor.metadata") }),
      ]),
      metadataError,
      el("div", { class: "editor-form-grid" }, [
        field(i18n.t("editor.name"), name),
        field(i18n.t("editor.description"), description),
        field(i18n.t("editor.labels"), labels),
      ]),
      el("button", {
        type: "button", class: "button primary", id: "draftApplyMetadata",
        text: i18n.t("editor.applyValidate"),
        onclick: async () => {
          try {
            const parsedLabels = JSON.parse(labels.value || "{}");
            if (!name.value.trim() || !parsedLabels || Array.isArray(parsedLabels)
                || typeof parsedLabels !== "object") throw new Error(i18n.t("editor.metadataInvalid"));
            metadataError.hidden = true;
            await applyCandidate(replaceMetadata(document, {
              name: name.value.trim(), description: description.value,
              labels: parsedLabels,
            }));
          } catch (error) {
            metadataError.textContent = error.message;
            metadataError.hidden = false;
          }
        },
      }),
    );

    if (selectedNode !== null) {
      selectedNode = Math.min(selectedNode, Math.max(0, document.nodes.length - 1));
    }
    const nodeList = el("div", { class: "editor-node-list" });
    document.nodes.forEach((node, index) => nodeList.append(el("button", {
      type: "button", class: `editor-nav-item ${index === selectedNode ? "active" : ""}`,
      text: `${node.id} · ${node.kind}`,
      onclick: () => { selectedNode = index; drawStructured(); showPane("nodes"); },
    })));
    nodeList.append(el("button", {
      type: "button", class: "button", id: "draftAddNode", text: i18n.t("editor.addNode"),
      onclick: () => { selectedNode = null; drawStructured(); showPane("nodes"); },
    }));
    const original = selectedNode === null ? null : document.nodes[selectedNode];
    const working = JSON.parse(JSON.stringify(original || {
      id: `node_${document.nodes.length + 1}`, kind: "terminal", inputs: [], outputs: [],
    }));
    working.inputs ||= [];
    working.outputs ||= [];
    working.config ||= {};
    const nodeEditor = el("div", { class: "editor-node-form" });
    const configFields = el("div", { class: "editor-config-fields" });
    const handlerField = el("div");
    const config = el("textarea", { id: "draftNodeConfig", class: "mono" });
    config.value = JSON.stringify(working.config, null, 2);
    const nodeError = el("div", { class: "banner error", hidden: "hidden", role: "alert" });
    const nodeId = el("input", { id: "draftNodeId", value: working.id });
    const entryNode = el("input", {
      type: "checkbox",
      ...((document.entry || []).includes(working.id) ? { checked: "checked" } : {}),
    });
    const kind = el("select", { id: "draftNodeKind", "aria-label": i18n.t("editor.kind") },
      ["action", "human", "agentic", "foreach", "subflow", "decision", "join", "terminal", "extension"].map((value) =>
        el("option", { value, ...(value === working.kind ? { selected: "selected" } : {}), text: value })));
    kind.addEventListener("change", () => {
      working.kind = kind.value;
      if (working.kind !== "action") delete working.handler;
      drawHandlerField();
      drawConfigFields();
    });
    nodeEditor.append(
      field(i18n.t("editor.nodeId"), nodeId),
      field(i18n.t("editor.kind"), kind),
      handlerField,
    );

    const selectedManifest = () => handlers.find((handler) =>
      handler.name === working.handler?.name && handler.version === working.handler?.version);
    const drawConfigFields = () => {
      configFields.replaceChildren();
      const schema = selectedManifest()?.config_schema || {};
      const properties = schema.properties || {};
      const required = new Set(schema.required || []);
      for (const [name, property] of Object.entries(properties)) {
        let control;
        const value = working.config[name];
        if (Array.isArray(property.enum)) {
          control = el("select", { "aria-label": name }, property.enum.map((option) =>
            el("option", {
              value: String(option), ...(option === value ? { selected: "selected" } : {}),
              text: String(option),
            })));
          control.addEventListener("change", () => { working.config[name] = control.value; });
        } else if (property.type === "boolean") {
          control = el("input", { type: "checkbox", ...(value ? { checked: "checked" } : {}) });
          control.addEventListener("change", () => { working.config[name] = control.checked; });
        } else {
          control = el("input", {
            type: ["integer", "number"].includes(property.type) ? "number" : "text",
            value: value ?? "", ...(property.minimum !== undefined ? { min: property.minimum } : {}),
          });
          control.addEventListener("input", () => {
            if (control.value === "" && !required.has(name)) delete working.config[name];
            else working.config[name] = ["integer", "number"].includes(property.type)
              ? Number(control.value) : control.value;
            config.value = JSON.stringify(working.config, null, 2);
          });
        }
        control.addEventListener("change", () => {
          config.value = JSON.stringify(working.config, null, 2);
        });
        configFields.append(field(`${name}${required.has(name) ? " *" : ""}`, control));
      }
      configFields.hidden = !Object.keys(properties).length;
    };

    const drawHandlerField = () => {
      handlerField.replaceChildren();
      handlerField.hidden = working.kind !== "action";
      if (working.kind !== "action") return;
      const compatible = compatibleHandlers(working, handlers);
      const handlerSelect = el("select", {
        id: "draftNodeHandler", "aria-label": i18n.t("editor.handler"),
      }, compatible.map((handler) => el("option", {
        value: `${handler.name}\u0000${handler.version}`,
        ...(working.handler?.name === handler.name && working.handler?.version === handler.version
          ? { selected: "selected" } : {}),
        text: `${handler.name}@${handler.version}`,
      })));
      if (!compatible.length) handlerSelect.append(el("option", {
        value: "", text: i18n.t("editor.noCompatibleHandlers"), disabled: "disabled",
      }));
      handlerSelect.addEventListener("change", () => {
        const [handlerName, version] = handlerSelect.value.split("\u0000");
        working.handler = { name: handlerName, version };
        drawConfigFields();
      });
      handlerField.append(field(i18n.t("editor.handler"), handlerSelect));
    };

    drawHandlerField();

    let portsChanged = false;
    nodeEditor.append(
      renderPortEditor(i18n.t("editor.inputs"), working.inputs, () => {
        portsChanged = true; drawHandlerField();
      }),
      renderPortEditor(i18n.t("editor.outputs"), working.outputs, () => {
        portsChanged = true; drawHandlerField();
      }),
    );
    drawConfigFields();
    nodeEditor.append(configFields, field(i18n.t("editor.configAdvanced"), config));
    const routeMode = el("select", { "aria-label": i18n.t("editor.routeMode") }, [
      el("option", { value: "", text: i18n.t("editor.defaultRouteMode") }),
      ...["exclusive", "parallel"].map((value) => el("option", {
        value, ...(working.route_mode === value ? { selected: "selected" } : {}), text: value,
      })),
    ]);
    routeMode.addEventListener("change", () => {
      if (routeMode.value) working.route_mode = routeMode.value;
      else delete working.route_mode;
    });
    const policyChecks = el("div", { class: "editor-check-grid" },
      (document.policies || []).map((policy) => {
        const checkbox = el("input", {
          type: "checkbox", value: policy.id,
          ...((working.policies || []).includes(policy.id) ? { checked: "checked" } : {}),
        });
        checkbox.addEventListener("change", () => {
          const selected = new Set(working.policies || []);
          if (checkbox.checked) selected.add(policy.id); else selected.delete(policy.id);
          working.policies = [...selected];
        });
        return el("label", { class: "editor-check" }, [
          checkbox, el("span", { text: `${policy.id} · ${policy.kind}` }),
        ]);
      }));
    nodeEditor.append(
      field(i18n.t("editor.routeMode"), routeMode),
      el("label", { class: "editor-check" }, [
        entryNode, el("span", { text: i18n.t("editor.entryNode") }),
      ]),
      el("fieldset", { class: "editor-fieldset" }, [
        el("legend", { text: i18n.t("editor.nodePolicies") }),
        policyChecks.childElementCount ? policyChecks
          : el("div", { class: "muted", text: i18n.t("editor.noPolicies") }),
      ]),
    );
    let nodeDeleteArmed = false;
    const nodeDelete = el("button", {
      type: "button", class: "button danger", id: "draftDeleteNode",
      text: i18n.t("editor.deleteNode"),
      onclick: async () => {
        if (!nodeDeleteArmed) {
          nodeDeleteArmed = true;
          nodeDelete.textContent = i18n.t("editor.deleteConfirm");
          return;
        }
        const removedIndex = selectedNode;
        selectedNode = Math.max(0, removedIndex - 1);
        await applyCandidate(removeNode(document, removedIndex));
      },
    });
    nodeEditor.append(el("div", { class: "actions" }, [
      el("button", {
        type: "button", class: "button primary", id: "draftApplyNode",
        text: i18n.t("editor.applyValidate"),
        onclick: async () => {
          try {
            working.id = nodeId.value.trim();
            if (!working.id) throw new Error(i18n.t("editor.nodeIdRequired"));
            if (document.nodes.some((node, index) =>
              node.id === working.id && index !== selectedNode)) {
              throw new Error(i18n.t("editor.nodeIdDuplicate"));
            }
            working.config = JSON.parse(config.value || "{}");
            if (working.kind === "action") {
              const compatible = compatibleHandlers(working, handlers);
              const selected = compatible.find((handler) =>
                handler.name === working.handler?.name && handler.version === working.handler?.version);
              if (!selected) throw new Error(portsChanged
                ? i18n.t("editor.handlerPortsChanged") : i18n.t("editor.handlerRequired"));
            }
            nodeError.hidden = true;
            const replacedIndex = selectedNode;
            if (selectedNode === null) selectedNode = document.nodes.length;
            let candidate = replaceNode(document, replacedIndex, working);
            const entries = new Set(candidate.entry || []);
            if (entryNode.checked) entries.add(working.id); else entries.delete(working.id);
            candidate = { ...candidate, entry: [...entries] };
            await applyCandidate(candidate);
          } catch (error) {
            nodeError.textContent = error.message;
            nodeError.hidden = false;
          }
        },
      }),
      ...(original ? [nodeDelete] : []),
    ]));
    nodesPane.replaceChildren(
      el("div", { class: "editor-node-layout" }, [nodeList, el("div", {}, [nodeError, nodeEditor])]),
    );

    const endpointOptions = (direction) => document.nodes.flatMap((node) =>
      (node[direction] || []).map((port) => ({
        value: `${node.id}\u0000${port.id}`,
        label: `${node.id}.${port.id} · ${port.schema_id}`,
      })));
    const sourceEndpoints = endpointOptions("outputs");
    const targetEndpoints = endpointOptions("inputs");
    const edges = document.edges || [];
    if (selectedEdge !== null) {
      selectedEdge = Math.min(selectedEdge, Math.max(0, edges.length - 1));
    }
    const edgeList = el("div", { class: "editor-node-list" });
    edges.forEach((edge, index) => edgeList.append(el("button", {
      type: "button", class: `editor-nav-item ${index === selectedEdge ? "active" : ""}`,
      text: `${edge.id} · ${edge.from.node} → ${edge.to.node}`,
      onclick: () => { selectedEdge = index; drawStructured(); showPane("edges"); },
    })));
    edgeList.append(el("button", {
      type: "button", class: "button", id: "draftAddEdge", text: i18n.t("editor.addEdge"),
      onclick: () => { selectedEdge = null; drawStructured(); showPane("edges"); },
    }));
    const priorEdge = selectedEdge === null ? null : edges[selectedEdge];
    const edgeWorking = JSON.parse(JSON.stringify(priorEdge || {
      id: `edge_${edges.length + 1}`,
      from: { node: sourceEndpoints[0]?.value.split("\u0000")[0] || "", port: sourceEndpoints[0]?.value.split("\u0000")[1] || "" },
      to: { node: targetEndpoints[0]?.value.split("\u0000")[0] || "", port: targetEndpoints[0]?.value.split("\u0000")[1] || "" },
    }));
    const edgeError = el("div", { class: "banner error", hidden: "hidden", role: "alert" });
    const edgeId = el("input", { id: "draftEdgeId", value: edgeWorking.id });
    const endpointSelect = (options, selected, label) => el("select", {
      "aria-label": label,
    }, options.map((option) => el("option", {
      value: option.value,
      ...(option.value === `${selected.node}\u0000${selected.port}` ? { selected: "selected" } : {}),
      text: option.label,
    })));
    const edgeFrom = endpointSelect(sourceEndpoints, edgeWorking.from, i18n.t("editor.edgeFrom"));
    const edgeTo = endpointSelect(targetEndpoints, edgeWorking.to, i18n.t("editor.edgeTo"));
    const edgeRoute = el("select", { "aria-label": i18n.t("editor.edgeRoute") },
      ["success", "error", "timeout", "cancel"].map((value) => el("option", {
        value, ...(value === (edgeWorking.route || "success") ? { selected: "selected" } : {}), text: value,
      })));
    const edgePriority = el("input", { type: "number", min: "0", value: edgeWorking.priority ?? 0 });
    const edgeBack = el("input", { type: "checkbox", ...(edgeWorking.back_edge ? { checked: "checked" } : {}) });
    const edgePolicy = el("select", { "aria-label": i18n.t("editor.edgePolicy") }, [
      el("option", { value: "", text: i18n.t("editor.noPolicy") }),
      ...(document.policies || []).map((policy) => el("option", {
        value: policy.id, ...(edgeWorking.policy === policy.id ? { selected: "selected" } : {}),
        text: `${policy.id} · ${policy.kind}`,
      })),
    ]);
    const edgeCondition = el("textarea", { class: "mono", id: "draftEdgeCondition" });
    edgeCondition.value = edgeWorking.condition === undefined ? "" : JSON.stringify(edgeWorking.condition, null, 2);
    const edgeMapping = el("textarea", { class: "mono", id: "draftEdgeMapping" });
    edgeMapping.value = edgeWorking.mapping === undefined ? "" : JSON.stringify(edgeWorking.mapping, null, 2);
    let edgeDeleteArmed = false;
    const edgeDelete = el("button", {
      type: "button", class: "button danger", id: "draftDeleteEdge",
      text: i18n.t("editor.deleteEdge"),
      onclick: async () => {
        if (!edgeDeleteArmed) {
          edgeDeleteArmed = true;
          edgeDelete.textContent = i18n.t("editor.deleteConfirm");
          return;
        }
        const removedIndex = selectedEdge;
        selectedEdge = Math.max(0, removedIndex - 1);
        await applyCandidate(removeEdge(document, removedIndex));
      },
    });
    const edgeForm = el("div", { class: "editor-form-grid" }, [
      edgeError,
      field(i18n.t("editor.edgeId"), edgeId),
      field(i18n.t("editor.edgeFrom"), edgeFrom),
      field(i18n.t("editor.edgeTo"), edgeTo),
      field(i18n.t("editor.edgeRoute"), edgeRoute),
      field(i18n.t("editor.edgePriority"), edgePriority),
      field(i18n.t("editor.edgePolicy"), edgePolicy),
      el("label", { class: "editor-check" }, [edgeBack, el("span", { text: i18n.t("editor.backEdge") })]),
      field(i18n.t("editor.edgeCondition"), edgeCondition),
      field(i18n.t("editor.edgeMapping"), edgeMapping),
      el("div", { class: "actions" }, [
        el("button", {
          type: "button", class: "button primary", id: "draftApplyEdge",
          text: i18n.t("editor.applyValidate"),
          onclick: async () => {
            try {
              const [fromNode, fromPort] = edgeFrom.value.split("\u0000");
              const [toNode, toPort] = edgeTo.value.split("\u0000");
              const next = {
                id: edgeId.value.trim(),
                from: { node: fromNode, port: fromPort },
                to: { node: toNode, port: toPort },
                route: edgeRoute.value,
                priority: Number(edgePriority.value),
                back_edge: edgeBack.checked,
              };
              if (edgePolicy.value) next.policy = edgePolicy.value;
              if (edgeCondition.value.trim()) {
                try { next.condition = JSON.parse(edgeCondition.value); }
                catch { next.condition = edgeCondition.value; }
              }
              if (edgeMapping.value.trim()) {
                next.mapping = JSON.parse(edgeMapping.value);
                if (!next.mapping || Array.isArray(next.mapping) || typeof next.mapping !== "object") {
                  throw new Error(i18n.t("editor.mappingInvalid"));
                }
              }
              if (!next.id || !fromNode || !fromPort || !toNode || !toPort) {
                throw new Error(i18n.t("editor.edgeInvalid"));
              }
              edgeError.hidden = true;
              const replacedIndex = selectedEdge;
              if (selectedEdge === null) selectedEdge = edges.length;
              await applyCandidate(replaceEdge(document, replacedIndex, next));
            } catch (error) {
              edgeError.textContent = error.message;
              edgeError.hidden = false;
            }
          },
        }),
        ...(priorEdge ? [edgeDelete] : []),
      ]),
    ]);
    edgesPane.replaceChildren(el("div", { class: "editor-node-layout" }, [edgeList, edgeForm]));

    const policies = document.policies || [];
    if (selectedPolicy !== null) {
      selectedPolicy = Math.min(selectedPolicy, Math.max(0, policies.length - 1));
    }
    const policyList = el("div", { class: "editor-node-list" });
    policies.forEach((policy, index) => policyList.append(el("button", {
      type: "button", class: `editor-nav-item ${index === selectedPolicy ? "active" : ""}`,
      text: `${policy.id} · ${policy.kind}`,
      onclick: () => { selectedPolicy = index; drawStructured(); showPane("policies"); },
    })));
    policyList.append(el("button", {
      type: "button", class: "button", id: "draftAddPolicy", text: i18n.t("editor.addPolicy"),
      onclick: () => { selectedPolicy = null; drawStructured(); showPane("policies"); },
    }));
    const priorPolicy = selectedPolicy === null ? null : policies[selectedPolicy];
    const policyWorking = JSON.parse(JSON.stringify(priorPolicy || {
      id: `policy_${policies.length + 1}`, kind: "retry", config: { max_attempts: 3 },
    }));
    const policyError = el("div", { class: "banner error", hidden: "hidden", role: "alert" });
    const policyId = el("input", { id: "draftPolicyId", value: policyWorking.id });
    const policyKind = el("select", { id: "draftPolicyKind", "aria-label": i18n.t("editor.policyKind") },
      ["route", "join", "retry", "rework", "loop", "completion"].map((value) => el("option", {
        value, ...(value === policyWorking.kind ? { selected: "selected" } : {}), text: value,
      })));
    const policyConfig = el("textarea", { id: "draftPolicyConfig", class: "mono" });
    policyConfig.value = JSON.stringify(policyWorking.config, null, 2);
    const templates = {
      route: {}, join: { mode: "all" }, retry: { max_attempts: 3 },
      rework: { max_generations: 3 }, loop: { max_iterations: 3 }, completion: {},
    };
    policyKind.addEventListener("change", () => {
      policyConfig.value = JSON.stringify(templates[policyKind.value], null, 2);
    });
    let policyDeleteArmed = false;
    const policyDelete = el("button", {
      type: "button", class: "button danger", id: "draftDeletePolicy",
      text: i18n.t("editor.deletePolicy"),
      onclick: async () => {
        if (!policyDeleteArmed) {
          policyDeleteArmed = true;
          policyDelete.textContent = i18n.t("editor.deleteConfirm");
          return;
        }
        const removedIndex = selectedPolicy;
        selectedPolicy = Math.max(0, removedIndex - 1);
        await applyCandidate(removePolicy(document, removedIndex));
      },
    });
    const policyForm = el("div", { class: "editor-form-grid" }, [
      policyError,
      field(i18n.t("editor.policyId"), policyId),
      field(i18n.t("editor.policyKind"), policyKind),
      field(i18n.t("editor.policyConfig"), policyConfig),
      el("div", { class: "actions" }, [
        el("button", {
          type: "button", class: "button primary", id: "draftApplyPolicy",
          text: i18n.t("editor.applyValidate"),
          onclick: async () => {
            try {
              const configValue = JSON.parse(policyConfig.value || "{}");
              if (!policyId.value.trim() || !configValue || Array.isArray(configValue)
                  || typeof configValue !== "object") throw new Error(i18n.t("editor.policyInvalid"));
              policyError.hidden = true;
              const replacedIndex = selectedPolicy;
              if (selectedPolicy === null) selectedPolicy = policies.length;
              await applyCandidate(replacePolicy(document, replacedIndex, {
                id: policyId.value.trim(), kind: policyKind.value, config: configValue,
              }));
            } catch (error) {
              policyError.textContent = error.message;
              policyError.hidden = false;
            }
          },
        }),
        ...(priorPolicy ? [policyDelete] : []),
      ]),
    ]);
    policiesPane.replaceChildren(el("div", { class: "editor-node-layout" }, [policyList, policyForm]));
    showPane(activePane);
  };

  let discardArmed = false;
  const redraw = () => {
    controls.replaceChildren();
    if (command("workflow.draft.save")) {
      controls.append(el("button", {
        class: "button", id: "draftFormat", text: i18n.t("editor.format"),
        onclick: async () => {
          try {
            if (saveTimer) clearTimeout(saveTimer);
            source.value = formatWorkflowSource(parseWorkflowSource(source.value));
            dirtySinceSave = true;
            setSaveState("unsaved");
            await save();
          } catch (error) {
            reportError(error);
          }
        },
      }));
    }
    const validate = command("workflow.draft.validate");
    if (validate) {
      controls.append(el("button", {
        class: "button", id: "draftValidate", text: i18n.t("editor.validate"),
        onclick: () => runCommand(
          "workflow.draft.validate", {},
          `workflow.draft.validate:${draft.draft_id}:${draft.revision}`,
        ),
      }));
    }
    const publish = command("workflow.draft.publish");
    if (publish) {
      controls.append(el("button", {
        class: "button primary", id: "draftPublish", text: i18n.t("editor.publish"),
        onclick: async () => {
          const response = await runCommand(
            "workflow.draft.publish", {},
            `workflow.draft.publish:${draft.draft_id}`,
          );
          if (response) {
            announce(i18n.t("editor.published", {
              workflowId: draft.workflow_id,
              version: i18n.number(response.data.published.version),
            }));
            navigate({ view: "workflows", runId: null });
          }
        },
      }));
    }
    const discard = command("workflow.draft.discard");
    if (discard) {
      controls.append(el("button", {
        class: "button danger", id: "draftDiscard",
        text: i18n.t(discardArmed ? "editor.discardConfirm" : "editor.discard"),
        onclick: async () => {
          if (!discardArmed) {
            discardArmed = true;
            redraw();
            return;
          }
          const response = await runCommand(
            "workflow.draft.discard", {},
            `workflow.draft.discard:${draft.draft_id}`,
          );
          if (response) {
            announce(i18n.t("editor.discarded"));
            navigate({ view: "workflows", runId: null });
          }
        },
      }));
    }
    if (draft.status !== "active") {
      controls.append(el("span", {
        class: "pill", text: i18n.status(draft.status),
      }));
    }

    diagnostics.replaceChildren();
    if (draft.validation_status === "valid") {
      diagnostics.append(el("div", {
        class: "banner info",
        text: i18n.t("editor.valid", {
          hash: (draft.validated_definition_hash || "").slice(0, 19),
        }),
      }));
    } else if (draft.diagnostics.length) {
      for (const item of draft.diagnostics) {
        const target = diagnosticTarget(item);
        diagnostics.append(el("div", { class: "diagnostic-row" }, [
          el("button", {
            type: "button", class: "error-item diagnostic-item",
            "aria-label": i18n.t("editor.locateDiagnostic", { code: item.code, path: target.path }),
            onclick: () => locateDiagnostic(item, target),
          }, [
            el("div", { class: "mono", text: `${item.code} ${item.json_path || ""}` }),
            el("div", { text: item.message }),
          ]),
          el("button", {
            type: "button", class: "button diagnostic-copy",
            text: i18n.t("editor.copyDiagnostic"),
            onclick: () => copyText(`${item.code} ${target.path}`),
          }),
        ]));
      }
    } else {
      diagnostics.append(el("div", {
        class: "muted", text: i18n.t("editor.notValidated"),
      }));
    }
    draftFacts.textContent = i18n.t("editor.facts", {
      revision: i18n.number(draft.revision),
      updated: i18n.dateTime(draft.updated_at),
      hash: (draft.validated_definition_hash || draft.source_hash || "—").slice(0, 27),
    });
  };

  root.append(
    el("section", { class: "panel workflow-editor-panel" }, [
      el("div", { class: "panel-head" }, [
        el("div", {}, [
          el("div", { class: "eyebrow", text: `${draft.workflow_id} · v${draft.base_version}` }),
          el("div", { class: "panel-title", text: i18n.t("editor.title") }),
        ]),
        saveState,
        controls,
      ]),
      el("div", { class: "panel-body" }, [
        conflict,
        el("div", { class: "workflow-editor-layout" }, [
          editorNav,
          el("div", { class: "workflow-editor-content" }, [editorMain, draftFacts]),
          el("aside", { class: "editor-diagnostics" }, [
            el("div", { class: "panel-title", text: i18n.t("editor.diagnostics") }),
            diagnostics,
          ]),
        ]),
      ]),
    ]),
  );
  editorMain.append(
    outlinePane, metadataPane, nodesPane, edgesPane, policiesPane, sourcePane,
  );

  const warnBeforeUnload = (event) => {
    if (!dirtySinceSave && !saving) return;
    event.preventDefault();
    event.returnValue = "";
  };
  const retryWhenOnline = () => {
    if (dirtySinceSave && !saving) save();
  };
  const markOffline = () => {
    if (dirtySinceSave || saving) setSaveState("offline");
  };
  window.addEventListener("beforeunload", warnBeforeUnload);
  window.addEventListener("online", retryWhenOnline);
  window.addEventListener("offline", markOffline);
  activeViewCleanup = () => {
    if (saveTimer) clearTimeout(saveTimer);
    window.removeEventListener("beforeunload", warnBeforeUnload);
    window.removeEventListener("online", retryWhenOnline);
    window.removeEventListener("offline", markOffline);
    activeViewLeaveGuard = null;
  };
  activeViewLeaveGuard = () => (
    (!dirtySinceSave && !saving) || window.confirm(i18n.t("editor.leaveConfirm"))
  );
  setSaveState("saved");
  redraw();
  drawStructured();
  showPane("outline");
}

function generatedInputSupported(entry) {
  const simple = new Set(["string", "integer", "number", "boolean"]);
  return entry.input_mode === "structured" && entry.inputs.every((port) => {
    const schema = port.schema || {};
    return Array.isArray(schema.enum) || simple.has(schema.type);
  });
}

function bindGoalInput(entry, goal, input = {}) {
  const binding = entry.goal_binding;
  if (!binding) return input;
  const prior = input[binding.input_id];
  const envelope = prior && typeof prior === "object" && !Array.isArray(prior)
    ? { ...prior } : {};
  envelope[binding.property] = goal;
  return { ...input, [binding.input_id]: envelope };
}

function inputField(port, value) {
  const schema = port.schema || {};
  let control;
  if (Array.isArray(schema.enum)) {
    control = el("select", { id: `newRunInput-${port.id}`, "data-port": port.id });
    if (!port.required && !port.has_default) {
      control.append(el("option", { value: "", text: i18n.t("newRun.input.notSet") }));
    }
    for (const option of schema.enum) control.append(el("option", {
      value: JSON.stringify(option), text: String(option),
      ...(Object.is(option, value) ? { selected: "selected" } : {}),
    }));
  } else if (schema.type === "boolean") {
    control = el("input", {
      type: "checkbox", id: `newRunInput-${port.id}`, "data-port": port.id,
      "data-type": "boolean", ...(value === true ? { checked: "checked" } : {}),
    });
  } else {
    control = el("input", {
      type: ["integer", "number"].includes(schema.type) ? "number" : "text",
      id: `newRunInput-${port.id}`, "data-port": port.id, "data-type": schema.type || "string",
      ...(schema.minimum !== undefined ? { min: schema.minimum } : {}),
      ...(schema.maximum !== undefined ? { max: schema.maximum } : {}),
      ...(schema.minLength !== undefined ? { minlength: schema.minLength } : {}),
      ...(schema.maxLength !== undefined ? { maxlength: schema.maxLength } : {}),
      ...(schema.pattern !== undefined ? { pattern: schema.pattern } : {}),
      ...(["integer", "number"].includes(schema.type)
        ? { step: schema.type === "number" ? "any" : "1" } : {}),
      ...(value !== undefined && value !== null ? { value: String(value) } : {}),
      ...(port.required ? { required: "required" } : {}),
    });
  }
  return el("div", { class: "field" }, [
    el("label", { for: `newRunInput-${port.id}`, text: port.description || port.id }),
    control,
    el("small", { class: "muted mono", text: port.schema_id }),
  ]);
}

function readGeneratedInputs(container, entry) {
  const result = {};
  for (const port of entry.inputs) {
    const control = container.querySelector(`[data-port="${CSS.escape(port.id)}"]`);
    if (!control) continue;
    if (control.tagName === "SELECT" && control.value === "" && !port.required) continue;
    else if (control.tagName === "SELECT") result[port.id] = JSON.parse(control.value);
    else if (control.dataset.type === "boolean") result[port.id] = control.checked;
    else if (!control.value && !port.required) continue;
    else if (control.dataset.type === "integer") result[port.id] = Number.parseInt(control.value, 10);
    else if (control.dataset.type === "number") result[port.id] = Number(control.value);
    else result[port.id] = control.value;
  }
  return result;
}

/** Describe → draft → publish. The draft is the compiler-validated source the
 * server returned; publishing executes the AllowedCommand advertised on that
 * draft, so the dialog never invents a URL or an expected version. */
async function generateWorkflowDialog(generateCommand) {
  let agentHandlers = [];
  try {
    const catalog = await api.handlerCatalog();
    agentHandlers = (catalog.data.handlers || []).filter((item) =>
      item.registration_status === "registered"
      && (item.capabilities || []).includes("agent.invoke"),
    );
  } catch (error) {
    reportError(error);
  }
  const dialog = el("dialog", { "aria-label": i18n.t("generate.title") });
  const form = el("form", { method: "dialog" });
  dialog.append(form);
  let draft = null;
  let busy = false;
  let validating = false;
  let draftProblem = "";
  let instructionText = "";
  let defaultAgent = agentHandlers[0]?.name || "";

  const switchAgent = async (nodeId, handlerName) => {
    if (busy || validating) return;
    const handler = agentHandlers.find((item) => item.name === handlerName);
    const validateCommand = (draft.allowed_commands || []).find(
      (item) => item.command === "workflow.validate",
    );
    if (!handler || !validateCommand) return;
    const document_ = JSON.parse(draft.source);
    const node = document_.nodes.find((item) => item.id === nodeId);
    if (!node?.handler) return;
    node.handler = { name: handler.name, version: handler.version };
    draft = {
      ...draft,
      source: JSON.stringify(document_, null, 2),
      allowed_commands: [validateCommand],
    };
    validating = true;
    draftProblem = "";
    draw();
    try {
      const response = await api.execute(
        validateCommand, { source: draft.source },
        `workflow.validate:${Date.now()}`,
      );
      draft = { ...draft, ...response.data, attempts: draft.attempts };
    } catch (error) {
      draftProblem = describeGenerationFailure(error);
    } finally {
      validating = false;
      draw();
    }
  };

  const draw = () => {
    const problem = el("div", { class: "banner error", hidden: "hidden", role: "alert" });
    if (draftProblem) {
      problem.textContent = draftProblem;
      problem.hidden = false;
    }
    const actions = el("div", { class: "actions" }, [
      el("button", { class: "button", value: "cancel", text: i18n.t("action.cancel") }),
    ]);
    const body = [el("h2", { text: i18n.t("generate.title") }), problem];

    if (!draft) {
      const instruction = el("textarea", {
        id: "generateInstruction", required: "required", maxlength: "4000",
        placeholder: i18n.t("generate.instructionPh"), text: instructionText,
      });
      body.push(
        el("div", { class: "field" }, [
          el("label", { for: "generateInstruction", text: i18n.t("generate.instruction") }),
          instruction,
          el("small", { class: "muted", text: i18n.t("generate.hint") }),
        ]),
      );
      if (agentHandlers.length) {
        body.push(el("div", { class: "field" }, [
          el("label", { for: "generateDefaultAgent", text: i18n.t("generate.defaultAgent") }),
          el("select", {
            id: "generateDefaultAgent",
            onchange: (event) => { defaultAgent = event.target.value; },
          }, agentHandlers.map((handler) => el("option", {
            value: handler.name,
            text: `${handler.name}@${handler.version}`,
            ...(handler.name === defaultAgent ? { selected: "selected" } : {}),
          }))),
          el("small", { class: "muted", text: i18n.t("generate.defaultAgentHint") }),
        ]));
      }
      const generate = el("button", {
        type: "button", class: "button primary", id: "generateSubmit",
        text: i18n.t("generate.action"),
        onclick: async () => {
          if (busy || !instruction.value.trim()) return;
          busy = true;
          generate.disabled = true;
          generate.textContent = i18n.t("generate.generating");
          problem.hidden = true;
          try {
            instructionText = instruction.value.trim();
            const response = await api.execute(
              generateCommand, {
                instruction: instructionText,
                ...(defaultAgent ? { default_agent: defaultAgent } : {}),
              },
              `workflow.generate:${Date.now()}`,
            );
            draft = response.data;
            draw();
          } catch (error) {
            problem.textContent = describeGenerationFailure(error);
            problem.hidden = false;
          } finally {
            busy = false;
            generate.disabled = false;
            generate.textContent = i18n.t("generate.action");
          }
        },
      });
      actions.append(generate);
    } else {
      const document_ = JSON.parse(draft.source);
      body.push(
        el("div", { class: "eyebrow", text: `${draft.workflow_id} · ${draft.definition_hash.slice(0, 19)}…` }),
        el("p", { class: "muted", text: i18n.t("generate.preview", {
          nodes: i18n.number(draft.node_count),
          attempts: i18n.number(draft.attempts),
        }) }),
        validating ? el("div", {
          class: "banner info", text: i18n.t("generate.validating"),
        }) : null,
        el("div", { class: "definition-list" }, document_.nodes.map((node) => {
          const editableAgent = node.handler?.name.startsWith("agent.")
            && agentHandlers.length;
          return el("div", { class: "actions" }, [
            el("span", { class: "mono", text: node.id }),
            el("span", { class: "pill", text: node.kind }),
            editableAgent ? el("select", {
              class: "draft-agent-select",
              "aria-label": i18n.t("generate.nodeAgent", { node: node.id }),
              disabled: validating ? "disabled" : null,
              onchange: (event) => switchAgent(node.id, event.target.value),
            }, agentHandlers.map((handler) => el("option", {
              value: handler.name,
              text: `${handler.name}@${handler.version}`,
              ...(handler.name === node.handler.name ? { selected: "selected" } : {}),
            }))) : node.handler
              ? el("span", { class: "muted mono", text: `${node.handler.name}@${node.handler.version}` })
              : null,
          ]);
        })),
        el("details", {}, [
          el("summary", { class: "muted", text: i18n.t("generate.source") }),
          el("pre", { class: "artifact-preview", text: draft.source }),
        ]),
      );
      const publishCommand = (draft.allowed_commands || []).find(
        (item) => item.command === "workflow.publish",
      );
      const back = el("button", {
        type: "button", class: "button", text: i18n.t("generate.back"),
        disabled: validating ? "disabled" : null,
        onclick: () => { draft = null; draftProblem = ""; draw(); },
      });
      actions.append(back);
      if (publishCommand) {
        actions.append(el("button", {
          type: "button", class: "button primary", id: "generatePublish",
          text: i18n.t("generate.publish"),
          onclick: async () => {
            if (busy) return;
            busy = true;
            problem.hidden = true;
            try {
              const published = await api.execute(
                publishCommand, { source: draft.source },
                `workflow.publish:${draft.definition_hash}`,
              );
              dialog.close();
              announce(i18n.t("generate.published", {
                workflowId: published.data.workflow_id,
                version: i18n.number(published.data.version),
              }));
              await render();
            } catch (error) {
              problem.textContent = describeGenerationFailure(error);
              problem.hidden = false;
            } finally {
              busy = false;
            }
          },
        }));
      }
    }

    actions.querySelector("button[value=cancel]").textContent = i18n.t("action.cancel");
    form.replaceChildren(...body, actions);
  };

  dialog.addEventListener("close", () => dialog.remove(), { once: true });
  document.body.append(dialog);
  draw();
  dialog.showModal();
}

/** Generation failures carry the compiler's findings as JSON; show the
 * finding codes rather than a wall of serialized diagnostics. */
function describeGenerationFailure(error) {
  if (!(error instanceof ApiError)) throw error;
  try {
    const payload = JSON.parse(error.message);
    const codes = (payload.diagnostics || [])
      .map((item) => item.code)
      .filter(Boolean);
    return codes.length
      ? i18n.t("generate.failed", { codes: codes.join(", ") })
      : payload.message || error.message;
  } catch {
    return i18n.t(error.messageKey, { message: error.message });
  }
}

async function newRunDialog(preselectedWorkflowId = null, preselectedVersion = null) {
  try {
    const active = (await api.dashboard()).data.active_goal;
    if (active) {
      announce(i18n.t("newRun.active.exists", { goal: runName(active) }), "info");
      navigate({ view: "run", runId: active.run_id });
      return;
    }
  } catch (error) {
    reportError(error);
    return;
  }
  let catalog;
  try {
    catalog = await api.workflowCatalog();
  } catch (error) {
    reportError(error);
    announce(i18n.t("newRun.catalog.unavailable"), "error");
    return;
  }
  let entries = catalog.data.workflows;
  if (preselectedWorkflowId && preselectedVersion) {
    try {
      const pinned = (await api.workflowDetail(
        preselectedWorkflowId, preselectedVersion,
      )).data;
      entries = entries.map((item) =>
        item.workflow_id === preselectedWorkflowId ? pinned : item);
    } catch (error) {
      reportError(error);
      return;
    }
  }
  const state = {
    step: 0,
    workflowId: entries.some((item) => item.workflow_id === preselectedWorkflowId)
      ? preselectedWorkflowId : null,
    goal: "",
    input: {},
    pinnedVersion: preselectedVersion,
    intent: `run.start:${crypto.randomUUID ? crypto.randomUUID() : Date.now()}`,
  };
  const dialog = el("dialog", { class: "goal-wizard", "aria-label": i18n.t("newRun.title") });
  const form = el("form", { method: "dialog" });
  dialog.append(form);
  document.body.append(dialog);

  const fail = (key, values = {}) => {
    const problem = form.querySelector(".wizard-problem");
    problem.textContent = i18n.t(key, values);
    problem.hidden = false;
  };
  const selectedEntry = () => entries.find((item) => item.workflow_id === state.workflowId);
  const entryVersion = (entry) => entry.selected_version || entry.latest_version;

  const draw = () => {
    const entry = selectedEntry();
    const steps = el("ol", { class: "wizard-steps", "aria-label": i18n.t("newRun.steps") });
    for (let index = 0; index < 4; index += 1) {
      steps.append(el("li", {
        class: index === state.step ? "active" : index < state.step ? "done" : "",
        "aria-current": index === state.step ? "step" : null,
        text: `${index + 1}. ${i18n.t(`newRun.step.${index + 1}`)}`,
      }));
    }
    const content = el("div", { class: "wizard-content" });
    if (state.step === 0) {
      content.append(el("h3", { text: i18n.t("newRun.select.heading") }));
      const choices = el("div", { class: "wizard-workflows" });
      for (const item of entries) {
        choices.append(el("label", { class: `wizard-workflow list-option-card${item.workflow_id === state.workflowId ? " selected" : ""}` }, [
          el("input", {
            type: "radio", name: "workflow", value: item.workflow_id,
            ...(item.workflow_id === state.workflowId ? { checked: "checked" } : {}),
            onchange: () => {
              state.workflowId = item.workflow_id;
              if (item.workflow_id !== preselectedWorkflowId) state.pinnedVersion = null;
              draw();
            },
          }),
          el("span", { class: "wizard-workflow-mark", "aria-hidden": "true", text: "⌘" }),
          el("span", { class: "wizard-workflow-copy" }, [
            el("strong", { text: item.name }),
            el("small", { class: "muted mono", text: `${item.workflow_id} · v${entryVersion(item)}` }),
            el("span", { class: "muted", text: item.description || i18n.t("workflows.noDescription") }),
          ]),
          el("span", { class: "workflow-meta", text: i18n.t("workflows.summary", {
            nodes: i18n.number(item.summary.node_count), inputs: i18n.number(item.inputs.length),
          }) }),
        ]));
      }
      if (!entries.length) choices.append(el("div", { class: "empty", text: i18n.t("workflows.empty") }));
      content.append(choices);
    } else if (state.step === 1) {
      content.append(el("h3", {
        text: i18n.t(entry.goal_binding ? "newRun.goal.heading" : "newRun.inputs.heading"),
      }));
      content.append(el("div", { class: "field" }, [
        el("label", { for: "newRunGoal", text: i18n.t("newRun.goal") }),
        el("textarea", { id: "newRunGoal", required: "required", text: state.goal }),
      ]));
      const inputArea = el("div", { id: "newRunInputs", class: "wizard-inputs" });
      if (entry.goal_binding) {
        inputArea.append(
          el("div", { class: "banner info", text: i18n.t("newRun.input.goalBound", {
            input: entry.goal_binding.input_id,
          }) }),
          el("details", { class: "advanced-input" }, [
            el("summary", { text: i18n.t("newRun.input.advanced") }),
            el("p", { class: "muted", text: i18n.t("newRun.input.advancedHelp") }),
            el("div", { class: "field" }, [
              el("label", { for: "newRunInput", text: i18n.t("newRun.input") }),
              el("textarea", { id: "newRunInput", text: JSON.stringify(state.input, null, 2) }),
            ]),
          ]),
        );
      } else if (generatedInputSupported(entry)) {
        for (const port of entry.inputs) {
          const value = Object.prototype.hasOwnProperty.call(state.input, port.id)
            ? state.input[port.id] : port.has_default ? port.default : undefined;
          inputArea.append(inputField(port, value));
        }
        if (!entry.inputs.length) inputArea.append(el("div", { class: "muted", text: i18n.t("newRun.input.none") }));
      } else {
        inputArea.append(
          el("div", { class: "banner info", text: i18n.t("newRun.input.jsonFallback") }),
          el("div", { class: "field" }, [
            el("label", { for: "newRunInput", text: i18n.t("newRun.input") }),
            el("textarea", { id: "newRunInput", text: JSON.stringify(state.input, null, 2) }),
          ]),
        );
      }
      content.append(inputArea);
    } else if (state.step === 2) {
      content.append(
        el("h3", { text: i18n.t("newRun.review.heading") }),
        el("dl", { class: "review-list" }, [
          el("div", {}, [el("dt", { text: i18n.t("newRun.workflow") }), el("dd", { text: `${entry.name} · v${entryVersion(entry)}` })]),
          el("div", {}, [el("dt", { text: i18n.t("newRun.goal") }), el("dd", { text: state.goal })]),
          el("div", {}, [
            el("dt", { text: i18n.t(entry.goal_binding ? "newRun.input.binding" : "newRun.input") }),
            el("dd", {
              class: entry.goal_binding ? "" : "mono",
              text: entry.goal_binding
                ? i18n.t("newRun.input.goalBoundReview", { input: entry.goal_binding.input_id })
                : JSON.stringify(state.input, null, 2),
            }),
          ]),
        ]),
      );
    } else {
      content.append(
        el("h3", { text: i18n.t("newRun.start.heading") }),
        el("p", { class: "muted", text: i18n.t("newRun.start.body", { workflow: entry.name, version: entryVersion(entry) }) }),
      );
    }

    const problem = el("div", { class: "banner error wizard-problem", hidden: "hidden" });
    const actions = el("div", { class: "actions wizard-actions" }, [
      el("button", { class: "button", value: "cancel", text: i18n.t("action.cancel") }),
      state.step > 0 ? el("button", {
        class: "button", type: "button", text: i18n.t("action.back"),
        onclick: () => { state.step -= 1; draw(); },
      }) : null,
      state.step < 3 ? el("button", {
        class: "button primary", type: "button", "data-wizard-next": "true",
        text: i18n.t("action.next"), onclick: () => {
          if (state.step === 0) {
            if (!entry) return fail("newRun.workflow.invalid");
            if (!entry.allowed_commands.length) return fail("newRun.workflow.forbidden");
          }
          if (state.step === 1) {
            const goal = form.querySelector("#newRunGoal");
            if (!goal.value.trim()) return fail("newRun.goal.required");
            if (!goal.reportValidity()) return;
            state.goal = goal.value.trim();
            if (entry.goal_binding) {
              try {
                const value = JSON.parse(form.querySelector("#newRunInput").value || "{}");
                if (value === null || typeof value !== "object" || Array.isArray(value)) throw new Error();
                state.input = bindGoalInput(entry, state.goal, value);
              } catch {
                return fail("newRun.input.invalid");
              }
            } else if (generatedInputSupported(entry)) {
              if (!form.reportValidity()) return;
              state.input = readGeneratedInputs(form, entry);
            } else {
              try {
                const value = JSON.parse(form.querySelector("#newRunInput").value || "{}");
                if (value === null || typeof value !== "object" || Array.isArray(value)) throw new Error();
                state.input = value;
              } catch {
                return fail("newRun.input.invalid");
              }
            }
          }
          state.step += 1;
          draw();
        },
      }) : el("button", {
        class: "button primary", type: "button", id: "newGoalStart",
        text: i18n.t("newRun.submit"), onclick: async (event) => {
          event.currentTarget.disabled = true;
          problem.hidden = true;
          try {
            // Refetch immediately before mutation. If the published workflow or
            // the actor's permission changed, do not submit a stale command.
            const fresh = state.pinnedVersion
              ? (await api.workflowDetail(state.workflowId, state.pinnedVersion)).data
              : (await api.workflowCatalog()).data.workflows.find(
                  (item) => item.workflow_id === state.workflowId,
                );
            if (!fresh) return fail("newRun.workflow.unavailable");
            if (!state.pinnedVersion && fresh.latest_version !== entry.latest_version) {
              dialog.close();
              announce(i18n.t("newRun.workflow.changed"), "error");
              return;
            }
            const allowed = fresh.allowed_commands[0];
            if (!allowed) return fail("newRun.workflow.forbidden");
            const started = await api.execute(allowed, {
              workflow_id: fresh.workflow_id,
              workflow_version: entryVersion(fresh),
              goal: state.goal,
              input: state.input,
            }, state.intent);
            dialog.close();
            announce(i18n.t("newRun.started", { runId: started.data.run_id }));
            navigate({ view: "run", runId: started.data.run_id });
          } catch (error) {
            if (error instanceof ApiError && error.code === "active_goal_exists") {
              dialog.close();
              const active = error.details.active_goal;
              announce(i18n.t("newRun.active.exists", { goal: active?.display_name || active?.run_id || "" }), "info");
              if (active?.run_id) navigate({ view: "run", runId: active.run_id });
            } else if (error instanceof ApiError && error.requiresRefresh) {
              dialog.close();
              announce(i18n.t("newRun.workflow.changed"), "error");
            } else {
              fail(
                error instanceof ApiError ? error.messageKey : "error.generic",
                { message: error.message || String(error) },
              );
              reportError(error);
            }
          } finally {
            if (event.currentTarget.isConnected) event.currentTarget.disabled = false;
          }
        },
      }),
    ]);
    form.replaceChildren(el("h2", { text: i18n.t("newRun.title") }), steps, problem, content, actions);
  };

  dialog.addEventListener("close", () => dialog.remove(), { once: true });
  draw();
  dialog.showModal();
}

/* ------------------------------------------------------------------- shell */

function navigate(next) {
  if (activeViewLeaveGuard && !activeViewLeaveGuard()) return;
  route = next;
  router.navigate(next);
}

async function render() {
  // A render requested mid-flight is coalesced, not dropped: the state (or
  // locale) it reacted to is not in the in-flight paint.
  if (rendering) {
    renderQueued = true;
    return;
  }
  rendering = true;
  if (activeViewCleanup) {
    activeViewCleanup();
    activeViewCleanup = null;
  }
  const root = document.getElementById("content");
  root.replaceChildren();
  root.append(dataState(el, i18n, "loading"));

  for (const button of document.querySelectorAll(".nav-button")) {
    const section = route.view === "run" ? "runs"
      : route.view === "goal" || route.view === "goals" ? "home"
        : route.view === "artifact" ? "artifacts" : route.view;
    const active = button.dataset.view === section;
    if (active) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  }
  document.getElementById("viewTitle").textContent = i18n.t(
    route.view === "run" ? "run.title"
      : route.view === "goal" || route.view === "goals" ? "home.title"
        : route.view === "artifact" ? "artifacts.title"
          : route.view === "workflowEdit" ? "editor.title" : `${route.view}.title`,
  );

  try {
    const fresh = el("div", { class: "content" });
    if (route.view === "home") await renderHome(fresh);
    else if (route.view === "goal") await renderHome(fresh, route.runId);
    else if (route.view === "goals") await renderHome(fresh);
    else if (route.view === "workflows") await renderWorkflows(fresh);
    else if (route.view === "workflowEdit") await renderWorkflowEditor(fresh, route.draftId);
    else if (route.view === "run") await renderRun(fresh, route.runId, route.tab || "overview");
    else if (route.view === "inbox") await renderInbox(fresh);
    else if (route.view === "artifacts") await renderArtifacts(fresh);
    else if (route.view === "artifact") await renderArtifacts(fresh, route.artifactId);
    else if (route.view === "agents") await renderAgents(fresh);
    else if (route.view === "ops") await renderOps(fresh);
    else if (route.view === "settings") await renderSettings(fresh);
    else await renderRuns(fresh);
    root.replaceChildren(...fresh.childNodes);
    if (route.view !== "inbox") await refreshInboxCount();
    refreshRuntimeCard();
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
  const actor = document.getElementById("actorChip");
  if (actor.textContent) {
    actor.title = i18n.t("actor.signedIn", { actor: actor.textContent });
  }
}

async function setLocale(locale) {
  i18n = await I18n.load(locale);
  i18n.persist();
  document.getElementById("localeSelect").value = locale;
  syncCustomSelect(document.getElementById("localeSelect"));
  applyStaticText();
  await render();
}

async function boot() {
  i18n = await I18n.load(preferredLocale());
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
  installCustomSelects();

  document.getElementById("themeToggle").addEventListener("click", () => {
    const next = document.documentElement.dataset.theme === "light" ? "dark" : "light";
    document.documentElement.dataset.theme = next;
    localStorage.setItem("orbit.theme", next);
  });
  document.documentElement.dataset.theme = localStorage.getItem("orbit.theme") || "dark";

  try {
    shellFacts = (await api.capabilities()).data;
    document.getElementById("actorChip").textContent = shellFacts.actor;
    mayStartRun = Boolean(shellFacts.permissions && shellFacts.permissions.start_run);
    document.getElementById("newRun").hidden = !mayStartRun;
  } catch (error) {
    reportError(error);
  }

  const sidebar = document.getElementById("sidebar");
  const navToggle = document.getElementById("navToggle");
  const navBackdrop = document.getElementById("navBackdrop");
  const compactNavigation = matchMedia("(max-width: 860px)");
  const setNavOpen = (open, restoreFocus = false) => {
    document.body.dataset.navOpen = open ? "true" : "false";
    navToggle.setAttribute("aria-expanded", String(open));
    const hidden = !open && compactNavigation.matches;
    sidebar.setAttribute("aria-hidden", String(hidden));
    sidebar.inert = hidden;
    if (restoreFocus) navToggle.focus();
  };
  navToggle.addEventListener("click", () => setNavOpen(document.body.dataset.navOpen !== "true"));
  navBackdrop.addEventListener("click", () => setNavOpen(false, true));
  compactNavigation.addEventListener("change", () => setNavOpen(false));
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && document.body.dataset.navOpen === "true") {
      setNavOpen(false, true);
    }
  });
  setNavOpen(false);

  document.getElementById("newRun").addEventListener("click", () => newRunDialog());
  document.getElementById("refresh").addEventListener("click", () => render());
  window.addEventListener("orbit:refresh", () => render());
  scheduleLivePolling();
  document.getElementById("globalSearchForm").addEventListener("submit", (event) => {
    event.preventDefault();
    goalFilters.q = document.getElementById("globalSearch").value.trim();
    navigate({ view: "home", runId: null });
  });
  for (const button of document.querySelectorAll(".nav-button")) {
    button.addEventListener("click", () => {
      // A message about the page you just left is noise on the next one.
      announce("");
      setNavOpen(false, compactNavigation.matches);
      navigate({ view: button.dataset.view, runId: null });
    });
  }

  applyStaticText();
  // Awaited so the first paint already carries the runtime's own health word.
  // After applyStaticText: the static catalog must not overwrite the status.
  await refreshRuntimeCard();
  await render();
}

boot();
