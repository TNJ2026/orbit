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

async function renderHome(root) {
  const dashboard = (await api.dashboard()).data;
  root.append(
    el("section", { class: "home-hero panel" }, [
      el("div", {}, [
        el("div", { class: "eyebrow", text: i18n.t("home.eyebrow") }),
        el("h2", { text: i18n.t("home.heading") }),
        el("p", { class: "muted", text: i18n.t("home.description") }),
      ]),
      mayStartRun ? el("button", {
        class: "button primary", text: i18n.t("action.newGoal"), onclick: newRunDialog,
      }) : null,
    ]),
  );

  const stats = el("section", { class: "stat-grid", "aria-label": i18n.t("home.overview") });
  for (const [key, value] of Object.entries({
    total: dashboard.counts.total,
    active: dashboard.counts.active,
    waiting: dashboard.counts.waiting,
    failed: dashboard.counts.failed,
  })) {
    stats.append(el("article", { class: `stat-card ${key}` }, [
      el("div", { class: "stat-label", text: i18n.t(`home.stat.${key}`) }),
      el("div", { class: "stat-value", text: i18n.number(value) }),
    ]));
  }
  root.append(stats);

  const recent = dashboard.recent_runs || [];
  root.append(el("div", { class: "home-grid" }, [
    el("section", { class: "panel attention-panel" }, [
      el("div", { class: "panel-head" }, [
        el("div", { class: "panel-title", text: i18n.t("home.attention") }),
        el("span", { class: "pill waiting", text: i18n.number(dashboard.attention_count) }),
      ]),
      el("div", { class: "panel-body" }, [
        el("p", { class: "muted", text: i18n.t(
          dashboard.attention_count ? "home.attention.body" : "home.attention.empty",
        ) }),
        el("button", {
          class: "button", text: i18n.t("action.openInbox"),
          onclick: () => navigate({ view: "inbox", runId: null }),
        }),
      ]),
    ]),
    el("section", { class: "panel" }, [
      el("div", { class: "panel-head" }, [
        el("div", { class: "panel-title", text: i18n.t("home.recent") }),
        el("button", {
          class: "button", text: i18n.t("action.viewAll"),
          onclick: () => navigate({ view: "goals", runId: null }),
        }),
      ]),
      el("div", { class: "panel-body recent-list" }, recent.length
        ? recent.map((run) => el("button", {
            class: "recent-run",
            onclick: () => navigate({ view: "goal", runId: run.run_id }),
          }, [
            el("span", {}, [
              el("strong", { class: "with-dot" }, [
                statusDot(run.status), el("span", { text: runName(run) }),
              ]),
              el("span", { class: "muted mono", text: run.workflow_id }),
            ]),
            pill(run.status),
          ]))
        : [el("div", { class: "muted", text: i18n.t("goals.empty") })]),
    ]),
  ]));
}

async function renderGoals(root, selectedRunId = null) {
  const response = await api.listRuns({ limit: 25, q: goalFilters.q, status: goalFilters.status });
  const runs = response.data.runs;
  let selected = selectedRunId ? runs.find((item) => item.run_id === selectedRunId) : runs[0];
  if (selectedRunId && !selected) selected = (await api.runSummary(selectedRunId)).data;

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
    detail.append(
      el("div", { class: "panel-head" }, [
        el("div", {}, [
          el("div", { class: "eyebrow", text: i18n.t("goals.detail") }),
          el("div", { class: "panel-title", text: runName(selected) }),
        ]),
        pill(selected.status),
      ]),
      el("div", { class: "panel-body" }, [
        el("p", { class: selected.goal ? "goal-copy" : "muted", text: selected.goal || i18n.t("goals.noDescription") }),
        el("dl", { class: "fact-grid" }, [
          el("div", {}, [el("dt", { text: i18n.t("run.workflow") }), el("dd", { text: `${selected.workflow_id} · v${selected.workflow_version}` })]),
          el("div", {}, [el("dt", { text: i18n.t("goals.waitingOn") }), el("dd", { text: waitText(selected) })]),
          el("div", {}, [el("dt", { text: i18n.t("runs.column.updated") }), el("dd", { text: i18n.dateTime(selected.updated_at) })]),
          budget ? el("div", {}, [
            el("dt", { text: i18n.t("run.budget") }),
            el("dd", { text: i18n.t("run.budget.used", {
              used: i18n.number(budget.consumed_microunits), total: i18n.number(budget.total_microunits), unit: budget.unit,
            }) }),
          ]) : null,
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
        el("tr", {}, [
          el("td", {
            "data-field": "run", "data-label": i18n.t("runs.column.run"),
          }, [
            el("button", {
              class: "button id-button",
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
          el("table", {}, [
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
  return el("tr", {}, [
        el("td", {}, [
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
        el("td", {}, [
          el("button", {
            class: "button id-button",
            text: item.run_id,
            title: item.run_id,
            onclick: () => navigate({ view: "run", runId: item.run_id }),
          }),
        ]),
        el("td", {}, [pill(item.status)]),
        el("td", {}, [
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
      grid.append(artifactCard(item));
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

function artifactCard(item) {
  return el("article", { class: "artifact-card panel" }, [
    el("button", {
      class: "artifact-card-main",
      onclick: () => navigate({ view: "artifact", artifactId: item.artifact_id, runId: null }),
    }, [
      el("span", { class: "pill", text: item.content_type }),
      el("strong", { class: "mono", text: item.output_port_id }),
      el("span", { class: "muted", text: i18n.t("artifacts.size", {
        size: i18n.number(item.size_bytes),
      }) }),
      el("span", { class: "muted mono", text: item.artifact_id }),
      el("span", { class: "muted mono", text: `${item.run_id} · ${item.producer_id}` }),
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
        return el("article", { class: "data-card" }, [
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
        if (live.changed) await render();
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
  if (generateCommand) {
    root.append(el("div", { class: "actions" }, [
      el("button", {
        class: "button primary", id: "generateWorkflow",
        text: i18n.t("generate.action"),
        onclick: () => generateWorkflowDialog(generateCommand),
      }),
    ]));
  }
  const cards = el("section", { class: "workflow-grid", "aria-label": i18n.t("workflows.list") });
  const detail = el("section", { class: "panel workflow-detail" }, [
    el("div", { class: "empty", text: i18n.t("workflows.select") }),
  ]);

  const showDetail = async (entry) => {
    detail.replaceChildren(dataState(el, i18n, "loading"));
    try {
      const value = (await api.workflowDetail(entry.workflow_id, entry.latest_version)).data;
      const definition = value.definition;
      detail.replaceChildren(
        el("div", { class: "panel-head" }, [
          el("div", {}, [
            el("div", { class: "eyebrow", text: `${value.workflow_id} · v${value.latest_version}` }),
            el("div", { class: "panel-title", text: value.name }),
          ]),
          value.allowed_commands.length ? el("button", {
            class: "button primary", text: i18n.t("action.newGoal"),
            onclick: () => newRunDialog(value.workflow_id),
          }) : null,
        ]),
        el("div", { class: "panel-body" }, [
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
    cards.append(el("article", { class: "workflow-card panel" }, [
      el("button", { class: "workflow-card-main", onclick: () => showDetail(entry) }, [
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
    ]));
  }
  if (!entries.length) cards.append(el("div", { class: "empty panel", text: i18n.t("workflows.empty") }));
  if (entries.length) await showDetail(entries[0]);
  root.append(el("div", { class: "workflows-layout" }, [cards, detail]));
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

async function newRunDialog(preselectedWorkflowId = null) {
  let catalog;
  try {
    catalog = await api.workflowCatalog();
  } catch (error) {
    reportError(error);
    announce(i18n.t("newRun.catalog.unavailable"), "error");
    return;
  }
  const entries = catalog.data.workflows;
  const state = {
    step: 0,
    workflowId: entries.some((item) => item.workflow_id === preselectedWorkflowId)
      ? preselectedWorkflowId : null,
    goal: "",
    input: {},
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
        choices.append(el("label", { class: `wizard-workflow${item.workflow_id === state.workflowId ? " selected" : ""}` }, [
          el("input", {
            type: "radio", name: "workflow", value: item.workflow_id,
            ...(item.workflow_id === state.workflowId ? { checked: "checked" } : {}),
            onchange: () => { state.workflowId = item.workflow_id; draw(); },
          }),
          el("span", {}, [el("strong", { text: item.name }), el("small", { class: "muted mono", text: `${item.workflow_id} · v${item.latest_version}` })]),
          el("span", { class: "muted", text: item.description || i18n.t("workflows.noDescription") }),
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
          el("div", {}, [el("dt", { text: i18n.t("newRun.workflow") }), el("dd", { text: `${entry.name} · v${entry.latest_version}` })]),
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
        el("p", { class: "muted", text: i18n.t("newRun.start.body", { workflow: entry.name, version: entry.latest_version }) }),
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
            const fresh = (await api.workflowCatalog()).data.workflows.find(
              (item) => item.workflow_id === state.workflowId,
            );
            if (!fresh) return fail("newRun.workflow.unavailable");
            if (fresh.latest_version !== entry.latest_version) {
              dialog.close();
              announce(i18n.t("newRun.workflow.changed"), "error");
              return;
            }
            const allowed = fresh.allowed_commands[0];
            if (!allowed) return fail("newRun.workflow.forbidden");
            const started = await api.execute(allowed, {
              workflow_id: fresh.workflow_id,
              workflow_version: fresh.latest_version,
              goal: state.goal,
              input: state.input,
            }, state.intent);
            dialog.close();
            announce(i18n.t("newRun.started", { runId: started.data.run_id }));
            navigate({ view: "run", runId: started.data.run_id });
          } catch (error) {
            if (error instanceof ApiError && error.requiresRefresh) {
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
  const root = document.getElementById("content");
  root.replaceChildren();
  root.append(dataState(el, i18n, "loading"));

  for (const button of document.querySelectorAll(".nav-button")) {
    const section = route.view === "run" ? "runs"
      : route.view === "goal" ? "goals"
        : route.view === "artifact" ? "artifacts" : route.view;
    const active = button.dataset.view === section;
    if (active) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  }
  document.getElementById("viewTitle").textContent = i18n.t(
    route.view === "run" ? "run.title"
      : route.view === "goal" ? "goals.title"
        : route.view === "artifact" ? "artifacts.title" : `${route.view}.title`,
  );

  try {
    const fresh = el("div", { class: "content" });
    if (route.view === "home") await renderHome(fresh);
    else if (route.view === "goal") await renderGoals(fresh, route.runId);
    else if (route.view === "goals") await renderGoals(fresh);
    else if (route.view === "workflows") await renderWorkflows(fresh);
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
  const actor = document.getElementById("actorChip");
  if (actor.textContent) {
    actor.title = i18n.t("actor.signedIn", { actor: actor.textContent });
  }
}

async function setLocale(locale) {
  i18n = await I18n.load(locale);
  i18n.persist();
  document.getElementById("localeSelect").value = locale;
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
    runFilters.q = document.getElementById("globalSearch").value.trim();
    navigate({ view: "runs", runId: null });
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
