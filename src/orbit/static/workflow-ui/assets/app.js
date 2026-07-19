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

const api = new Api();
let i18n;
let route = { view: "runs", runId: null };

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

/** Render the buttons a responsibility advertises — and nothing else. */
function commandButtons(commands, onDone) {
  return commands.map((allowed) =>
    el("button", {
      class: allowed.command === "run.cancel" ? "button danger" : "button",
      text: i18n.command(allowed),
      onclick: () => promptAndExecute(allowed, onDone),
    }),
  );
}

/** Collect whatever the command's payload schema needs, then send it once. */
async function promptAndExecute(allowed, onDone) {
  let payload = {};
  if (allowed.payload_schema.startsWith("human-submit")) {
    payload = await humanSubmitDialog(allowed);
  } else if (allowed.payload_schema.startsWith("budget-add")) {
    payload = await budgetDialog();
  } else if (allowed.payload_schema.startsWith("run-cancel")) {
    payload = { reason: "cancelled from the console" };
  } else if (allowed.payload_schema.startsWith("recovery-apply")) {
    payload = await recoveryDialog(allowed);
  }
  if (payload === null) return;

  announce("");
  try {
    await api.execute(allowed, payload);
    announce(i18n.t("command.accepted", { command: i18n.command(allowed) }));
  } catch (error) {
    const failure = reportError(error);
    // A conflict is not a dead end: reload so the operator sees the state that
    // beat them, with fresh expected_versions to act on.
    if (failure.requiresRefresh) await onDone();
    return;
  }
  await onDone();
}

/** Show `dialog` and resolve to `collect()` on confirm, or null on dismiss.
 *
 * The result is taken from the form's `submit` event rather than the dialog's
 * `close` event: a `method="dialog"` form does not fire `close` in every
 * browser, and a confirm that silently resolves to nothing looks exactly like
 * a command that did not work.
 */
function dialogResult(dialog, collect) {
  return new Promise((resolve) => {
    let settled = false;
    const settle = (value) => {
      if (settled) return;
      settled = true;
      dialog.remove();
      resolve(value);
    };

    dialog.querySelector("form").addEventListener("submit", (event) => {
      const confirmed = (event.submitter && event.submitter.value) === "confirm";
      // Let the native method="dialog" close run first, then settle.
      setTimeout(() => settle(confirmed ? collect() : null), 0);
    });
    // Esc and backdrop dismissal never reach the form.
    dialog.addEventListener("close", () => setTimeout(() => settle(null), 0));

    document.body.append(dialog);
    dialog.showModal();
  });
}

function humanSubmitDialog(allowed) {
  const decision = allowed.command.endsWith("reject") ? "reject" : "approve";
  const token = el("input", { type: "text", id: "humanToken", required: "required" });
  const value = el("textarea", { id: "humanValue" });
  const dialog = el("dialog", { "aria-label": i18n.t("human.title") }, [
    el("form", { method: "dialog" }, [
      el("h2", { text: i18n.t("human.title") }),
      el("p", {
        class: "muted",
        text: `${i18n.t("human.decision")}: ${i18n.t(`human.decision.${decision}`)}`,
      }),
      el("div", { class: "field" }, [
        el("label", { for: "humanToken", text: i18n.t("human.token") }),
        token,
        el("small", { class: "muted", text: i18n.t("human.token.hint") }),
      ]),
      el("div", { class: "field" }, [
        el("label", { for: "humanValue", text: i18n.t("human.value") }),
        value,
      ]),
      el("div", { class: "actions" }, [
        el("button", { class: "button", value: "cancel", text: i18n.t("action.cancel") }),
        el("button", {
          class: "button primary",
          value: "confirm",
          text: i18n.t("action.submit"),
        }),
      ]),
    ]),
  ]);
  return dialogResult(dialog, () => ({
    submission_token: token.value,
    decision,
    value: value.value ? JSON.parse(value.value) : null,
  }));
}

function budgetDialog() {
  const amount = el("input", { type: "number", id: "budgetAmount", min: "1", value: "1000" });
  const dialog = el("dialog", { "aria-label": i18n.t("budget.title") }, [
    el("form", { method: "dialog" }, [
      el("h2", { text: i18n.t("budget.title") }),
      el("div", { class: "field" }, [
        el("label", {
          for: "budgetAmount",
          text: i18n.t("budget.amount", { unit: "microunits" }),
        }),
        amount,
      ]),
      el("div", { class: "actions" }, [
        el("button", { class: "button", value: "cancel", text: i18n.t("action.cancel") }),
        el("button", {
          class: "button primary",
          value: "confirm",
          text: i18n.t("action.submit"),
        }),
      ]),
    ]),
  ]);
  return dialogResult(dialog, () => ({ amount_microunits: Number(amount.value) }));
}

function recoveryDialog(allowed) {
  const dialog = el("dialog", { "aria-label": i18n.t("recovery.title") }, [
    el("form", { method: "dialog" }, [
      el("h2", { text: i18n.t("recovery.title") }),
      el("p", { text: i18n.t("recovery.confirm") }),
      el("div", { class: "mono muted", text: allowed.action_id }),
      el("div", { class: "actions" }, [
        el("button", { class: "button", value: "cancel", text: i18n.t("action.cancel") }),
        el("button", {
          class: "button primary", value: "confirm", text: i18n.t("action.apply"),
        }),
      ]),
    ]),
  ]);
  return dialogResult(dialog, () => ({ action_ids: [allowed.action_id] }));
}

/* ------------------------------------------------------------------- views */

async function renderRuns(root) {
  const activeOnly = document.getElementById("activeOnly")?.checked || false;
  const body = el("tbody");
  const table = el("table", {}, [
    el("thead", {}, [
      el("tr", {}, [
        el("th", { text: i18n.t("runs.column.run") }),
        el("th", { text: i18n.t("runs.column.workflow") }),
        el("th", { text: i18n.t("runs.column.status") }),
        el("th", { text: i18n.t("runs.column.updated") }),
      ]),
    ]),
    body,
  ]);
  const panel = el("section", { class: "panel" }, [
    el("div", { class: "panel-head" }, [
      el("div", { class: "panel-title", text: i18n.t("runs.title") }),
      el("label", {}, [
        el("input", {
          type: "checkbox",
          id: "activeOnly",
          ...(activeOnly ? { checked: "checked" } : {}),
          onchange: () => render(),
        }),
        el("span", { text: ` ${i18n.t("runs.activeOnly")}` }),
      ]),
    ]),
    el("div", { class: "table-scroll" }, [table]),
  ]);
  root.append(panel);

  let cursor = null;
  const more = el("button", { class: "button", text: i18n.t("action.loadMore") });

  const page = async () => {
    const response = await api.listRuns({ cursor, activeOnly });
    for (const run of response.data.runs) {
      body.append(
        el("tr", {}, [
          el("td", {}, [
            el("button", {
              class: "button id-button",
              text: run.run_id,
              title: run.run_id,
              onclick: () => navigate({ view: "run", runId: run.run_id }),
            }),
          ]),
          el("td", { text: run.workflow_id }),
          el("td", {}, [pill(run.status)]),
          el("td", { text: i18n.dateTime(run.updated_at) }),
        ]),
      );
    }
    cursor = response.next_cursor;
    more.hidden = !cursor;
    if (!body.children.length) {
      panel.append(el("div", { class: "empty", text: i18n.t("runs.empty") }));
    }
  };

  more.addEventListener("click", () => page().catch(reportError));
  panel.append(el("div", { class: "panel-body" }, [more]));
  await page();
}

async function renderRun(root, runId) {
  let summary;
  try {
    summary = (await api.runSummary(runId)).data;
  } catch (error) {
    reportError(error);
    root.append(el("div", { class: "empty", text: i18n.t("run.notFound") }));
    return;
  }

  const reload = () => navigate({ view: "run", runId });
  const budget = summary.budget_summary;
  root.append(
    el("section", { class: "panel" }, [
      el("div", { class: "panel-head" }, [
        el("div", {}, [
          el("div", { class: "eyebrow", text: i18n.t("run.title") }),
          el("div", { class: "panel-title mono", text: summary.run_id }),
        ]),
        pill(summary.status),
      ]),
      el("div", { class: "panel-body" }, [
        el("div", { text: `${i18n.t("run.workflow")}: ${summary.workflow_id}` }),
        el("div", {
          text: `${i18n.t("run.version")}: ${i18n.number(summary.workflow_version)}`,
        }),
        budget
          ? el("div", {
              text: i18n.t("run.budget.used", {
                used: i18n.number(budget.consumed_microunits),
                total: i18n.number(budget.total_microunits),
                unit: budget.unit,
              }),
            })
          : null,
      ]),
    ]),
  );

  const responsibilities = (await api.responsibilities(runId)).data.responsibilities;
  const list = el("div", { class: "panel-body" });
  for (const item of responsibilities) {
    list.append(
      el("div", { class: "actions" }, [
        el("div", {}, [
          el("div", { text: item.label }),
          el("div", { class: "muted mono", text: item.responsibility_id }),
        ]),
        pill(item.status),
        ...commandButtons(item.allowed_commands, reload),
      ]),
    );
  }
  if (!responsibilities.length) {
    list.append(el("div", { class: "muted", text: i18n.t("run.responsibilities.empty") }));
  }
  root.append(
    el("section", { class: "panel" }, [
      el("div", { class: "panel-head" }, [
        el("div", { class: "panel-title", text: i18n.t("run.responsibilities") }),
      ]),
      list,
    ]),
  );

  root.append(await planPanel(runId));

  root.append(await dataPanel(runId));

  root.append(await pagedPanel(runId, "timeline", "run.timeline", (item) =>
    el("div", { class: "actions" }, [
      el("span", { class: "mono muted", text: i18n.dateTime(item.occurred_at) }),
      el("span", { text: item.type }),
      el("span", { class: "mono muted", text: item.aggregate_id }),
    ]),
  ));
  root.append(await pagedPanel(runId, "errors", "run.errors", (item) => {
    // The error lives in the event payload, not at the top level: an error
    // entry is an event, and flattening it here would hide the fields an
    // operator needs (category, code, which node) behind a bare message.
    const error = (item.payload && item.payload.error) || {};
    const where = (item.payload && item.payload.node_run_id) || item.aggregate_id;
    return el("div", {}, [
      el("div", { text: error.message || error.code || item.type }),
      el("div", {
        class: "muted mono",
        text: [error.category, error.source, where].filter(Boolean).join(" · "),
      }),
      el("div", { class: "muted mono", text: i18n.dateTime(item.occurred_at) }),
    ]);
  }));
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
    const value = item.kind === "value" && item.value !== null
      ? JSON.stringify(item.value)
      : `${item.content_type || item.schema_id} · ${i18n.number(item.size_bytes)} B`;
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
  const state = { version: definition.plan_version, view: "definition" };

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
      else content.replaceChildren(await planDiffView(runId, state, versions));
    } catch (error) {
      content.replaceChildren();
      reportError(error);
    }
  };

  for (const view of ["definition", "overlay", "diff"]) {
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

async function planDefinitionView(runId, state) {
  const definition = (await api.planDefinition(runId, state.version)).data;
  const list = el("div", {});
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
  const overlay = (await api.planOverlay(runId, state.version)).data;
  const list = el("div", {}, [
    el("div", {
      class: "eyebrow",
      text: i18n.t("plan.overlay.for", { version: overlay.plan_version }),
    }),
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
  const body = el("div", { class: "panel-body" });
  const more = el("button", { class: "button", text: i18n.t("action.loadMore") });
  let cursor = null;

  const page = async () => {
    const response = await api.runPage(runId, kind, cursor);
    for (const item of response.data.items) body.insertBefore(renderItem(item), more);
    cursor = response.next_cursor;
    more.hidden = !cursor;
    if (body.children.length === 1) {
      body.insertBefore(el("div", { class: "muted", text: i18n.t(`${titleKey}.empty`) }), more);
    }
  };

  more.addEventListener("click", () => page().catch(reportError));
  body.append(more);
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
    body.append(
      el("tr", {}, [
        el("td", { text: item.label }),
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
      ]),
    );
  }
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
  ]);
  root.append(panel);
  document.getElementById("inboxCount").textContent = items.length ? String(items.length) : "";
}

async function renderOps(root) {
  const [health, recovery, catalog] = await Promise.all([
    api.health().catch(() => null),
    api.recovery().catch(() => null),
    api.handlerCatalog().catch(() => null),
  ]);

  root.append(
    el("section", { class: "panel" }, [
      el("div", { class: "panel-head" }, [
        el("div", { class: "panel-title", text: i18n.t("ops.health") }),
        pill(health && health.status === "ready" ? "succeeded" : "failed"),
      ]),
      el("div", { class: "panel-body" }, [
        el("div", {
          text: i18n.t(
            health && health.status === "ready" ? "ops.health.ready" : "ops.health.notReady",
          ),
        }),
      ]),
    ]),
  );

  const findings = recovery ? recovery.data.findings : [];
  root.append(
    el("section", { class: "panel" }, [
      el("div", { class: "panel-head" }, [
        el("div", { class: "panel-title", text: i18n.t("ops.recovery") }),
      ]),
      el("div", { class: "panel-body" }, [
        el("div", {
          class: "muted",
          text: i18n.t("ops.recovery.scanned", {
            count: i18n.number(recovery ? recovery.data.scanned_runs : 0),
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

  const handlers = catalog ? catalog.data.handlers : [];
  const agents = catalog ? catalog.data.agents || [] : [];
  root.append(
    el("section", { class: "panel" }, [
      el("div", { class: "panel-head" }, [
        el("div", { class: "panel-title", text: i18n.t("ops.handlers") }),
      ]),
      el("div", { class: "panel-body" }, [
        ...handlers.map((handler) =>
          el("div", { class: "mono", text: `${handler.name} ${handler.version}` }),
        ),
        el("div", { class: "eyebrow", text: i18n.t("ops.agents") }),
        ...(agents.length
          ? agents.map((agent) =>
              el("div", { class: "mono", text: `${agent.name} ${agent.version}` }),
            )
          : [el("div", { class: "muted", text: i18n.t("ops.agents.empty") })]),
      ]),
    ]),
  );
}

/* ------------------------------------------------------------- new run flow */

async function newRunDialog() {
  const catalog = await api.workflowCatalog().catch(() => null);
  const entries = catalog ? catalog.data.workflows : [];
  const workflow = el("input", {
    type: "text", id: "newRunWorkflow", required: "required", list: "workflowOptions",
  });
  const options = el("datalist", { id: "workflowOptions" });
  for (const entry of entries) {
    options.append(el("option", { value: entry.workflow_id }));
  }
  const goal = el("input", { type: "text", id: "newRunGoal" });
  const input = el("textarea", { id: "newRunInput", text: "{}" });
  const problem = el("div", { class: "banner error", hidden: "hidden" });

  const dialog = el("dialog", { "aria-label": i18n.t("newRun.title") }, [
    el("form", { method: "dialog" }, [
      el("h2", { text: i18n.t("newRun.title") }),
      problem,
      el("div", { class: "field" }, [
        el("label", { for: "newRunWorkflow", text: i18n.t("newRun.workflow") }),
        workflow,
        options,
        el("small", { class: "muted", text: i18n.t("newRun.workflow.hint") }),
      ]),
      el("div", { class: "field" }, [
        el("label", { for: "newRunGoal", text: i18n.t("newRun.goal") }),
        goal,
      ]),
      el("div", { class: "field" }, [
        el("label", { for: "newRunInput", text: i18n.t("newRun.input") }),
        input,
      ]),
      el("div", { class: "actions" }, [
        el("button", { class: "button", value: "cancel", text: i18n.t("action.cancel") }),
        el("button", {
          class: "button primary",
          value: "confirm",
          text: i18n.t("newRun.submit"),
        }),
      ]),
    ]),
  ]);
  if (catalog === null) problem.hidden = false;

  // The wizard keeps nothing server-side: the whole thing resolves to exactly
  // one start_run, and closing the dialog leaves no draft behind.
  const body = await dialogResult(dialog, () => {
    let parsed;
    try {
      parsed = JSON.parse(input.value || "{}");
    } catch {
      return { invalid: true };
    }
    if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
      return { invalid: true };
    }
    return { workflow_id: workflow.value.trim(), goal: goal.value, input: parsed };
  });
  if (body === null) return;
  if (body.invalid) {
    announce(i18n.t("newRun.input.invalid"), "error");
    return;
  }

  const entry = entries.find((item) => item.workflow_id === body.workflow_id);
  const allowed = entry && entry.allowed_commands[0];
  if (!allowed) {
    announce(i18n.t("newRun.workflow.invalid"), "error");
    return;
  }

  try {
    const started = await api.execute(
      allowed, body, `run.start:${body.workflow_id}:${Date.now()}`,
    );
    announce(i18n.t("newRun.started", { runId: started.data.run_id }));
    navigate({ view: "run", runId: started.data.run_id });
  } catch (error) {
    reportError(error);
  }
}

/* ------------------------------------------------------------------- shell */

function navigate(next) {
  route = next;
  const hash = next.view === "run" ? `#/runs/${encodeURIComponent(next.runId)}` : `#/${next.view}`;
  if (location.hash !== hash) location.hash = hash;
  else render();
}

function readRoute() {
  const parts = location.hash.replace(/^#\/?/, "").split("/");
  if (parts[0] === "runs" && parts[1]) {
    return { view: "run", runId: decodeURIComponent(parts[1]) };
  }
  if (["runs", "inbox", "ops"].includes(parts[0])) return { view: parts[0], runId: null };
  return { view: "runs", runId: null };
}

async function render() {
  const root = document.getElementById("content");
  root.replaceChildren();
  root.append(el("div", { class: "muted", text: i18n.t("loading") }));

  for (const button of document.querySelectorAll(".nav-button")) {
    const active = button.dataset.view === route.view || (route.view === "run" && button.dataset.view === "runs");
    if (active) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  }
  document.getElementById("viewTitle").textContent = i18n.t(
    route.view === "run" ? "run.title" : `${route.view}.title`,
  );

  try {
    const fresh = el("div", { class: "content" });
    if (route.view === "run") await renderRun(fresh, route.runId);
    else if (route.view === "inbox") await renderInbox(fresh);
    else if (route.view === "ops") await renderOps(fresh);
    else await renderRuns(fresh);
    root.replaceChildren(...fresh.childNodes);
  } catch (error) {
    root.replaceChildren();
    reportError(error);
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

  document.getElementById("newRun").addEventListener("click", () => newRunDialog());
  document.getElementById("refresh").addEventListener("click", () => render());
  for (const button of document.querySelectorAll(".nav-button")) {
    button.addEventListener("click", () => {
      // A message about the page you just left is noise on the next one.
      announce("");
      navigate({ view: button.dataset.view, runId: null });
    });
  }
  window.addEventListener("hashchange", () => {
    route = readRoute();
    render();
  });

  applyStaticText();
  route = readRoute();
  await render();
}

boot();
