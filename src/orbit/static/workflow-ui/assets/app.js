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
  retryNodeDialog,
} from "./components/command-dialog.js";
import { dataState } from "./components/data-state.js";
import { semanticWorkflowDiff } from "./workflow-diff.js";

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
const goalFilters = { q: "", status: "" };
// A catalog of dozens is browsed, not scanned. The default order answers the
// question an author actually has — which workflow was I just running.
const simplifiedComposerState = { runId: null, workflowId: "", goal: "" };
let focusSimplifiedGoalOnRender = false;
let simplifiedWorkflowGenerationPending = false;
const TERMINAL_RUN_STATUSES = new Set(["succeeded", "failed", "cancelled"]);
// How much of one recorded value is rendered. Inline values are capped at
// 256 KB server-side; a page that pastes all of that stops responding.
const DATA_TEXT_LIMIT = 20_000;

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

const svgEl = (tag, props = {}, children = []) => {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2).toLowerCase(), value);
    else if (value !== null && value !== undefined) node.setAttribute(key, value);
  }
  for (const child of [].concat(children)) {
    if (child) node.append(child);
  }
  return node;
};

// Box geometry. The server hands us depth (column) and lane (row); turning
// those into pixels is the only thing the browser decides about this picture.
const GRAPH_BOX = {
  // The vertical gap also carries back/rework edges.  Keep enough clearance
  // that their dashed stroke does not visually cling to the card above it.
  width: 168, height: 60, gapX: 64, gapY: 40, pad: 16,
  // A one-lane flow would otherwise draw as a thin strip under the tabs,
  // and the panel would jump in height on every tab switch.
  minHeight: 260,
};

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
function readableNodeName(node) {
  const label = node?.label;
  if (typeof label === "string" && label.trim()) return label.trim();
  return i18n.t("simplified.workflow.step");
}

function workflowGraphView(graph, actionEditors = {}, onEditAction = null) {
  const { width, height, gapX, gapY, pad, minHeight } = GRAPH_BOX;
  const at = new Map();
  for (const position of graph.layout.positions) {
    at.set(position.node_id, {
      x: pad + position.depth * (width + gapX),
      y: pad + position.lane * (height + gapY),
    });
  }
  const geometry = new Map(graph.nodes.map((node) => {
    const spot = at.get(node.node_id);
    if (!spot) return [node.node_id, null];
    const nodeWidth = width;
    const nodeHeight = height;
    return [node.node_id, {
      x: spot.x + (width - nodeWidth) / 2,
      y: spot.y + (height - nodeHeight) / 2,
      width: nodeWidth,
      height: nodeHeight,
    }];
  }));
  const columns = Math.max(...graph.layout.positions.map((p) => p.depth), 0) + 1;
  const lanes = Math.max(...graph.layout.positions.map((p) => p.lane), 0) + 1;
  const canvasWidth = pad * 2 + columns * width + (columns - 1) * gapX;
  const drawnHeight = pad * 2 + lanes * height + (lanes - 1) * gapY;
  const canvasHeight = Math.max(drawnHeight, minHeight);
  // Centre a short graph in the taller canvas instead of pinning it to the top.
  const offsetY = Math.round((canvasHeight - drawnHeight) / 2);

  const edges = graph.edges.map((edge) => {
    const from = geometry.get(edge.from);
    const to = geometry.get(edge.to);
    if (!from || !to) return null;
    const start = { x: from.x + from.width, y: from.y + from.height / 2 };
    const end = { x: to.x, y: to.y + to.height / 2 };
    // A back edge points at an earlier column, so route it under the row it
    // came from instead of drawing a line straight through the boxes.
    const path = edge.back_edge
      ? `M${start.x - from.width / 2} ${from.y + from.height}`
        + ` V${from.y + from.height + gapY / 2}`
        + ` H${end.x + to.width / 2} V${to.y + to.height}`
      : `M${start.x} ${start.y} H${start.x + gapX / 2} V${end.y} H${end.x}`;
    return svgEl("path", {
      class: `graph-edge${edge.back_edge ? " back" : ""} route-${edge.route}`,
      d: path, "marker-end": "url(#graphArrow)",
    });
  });

  const boxes = graph.nodes.map((node, index) => {
    const spot = geometry.get(node.node_id);
    if (!spot) return null;
    // Node kinds are DSL vocabulary, shown verbatim here as they are in the
    // definition list below the picture. A handler gets one short line: the
    // registry calls it `agent.claude@1.0.0`, the reader recognises `claude`.
    // The exact name and version stay one hover (and one tab) away.
    const label = node.handler_name
      ? node.handler_name.replace(/^agent\./, "")
      : node.kind;
    // SVG text does not wrap or ellipsize, so a long id would spill past the
    // box. Clip each line to the box interior and keep the full value in a
    // <title> for hover — the drawing stays tidy, nothing is lost.
    const clipId = `graph-clip-${index}`;
    const editable = node.kind === "action" && Boolean(actionEditors[node.node_id]);
    const edit = () => {
      if (editable && onEditAction) onEditAction(node.node_id);
    };
    return svgEl("g", {
      class: `graph-box kind-${node.kind}${editable ? " editable" : ""}`,
      transform: `translate(${spot.x} ${spot.y})`,
      role: editable ? "button" : null,
      tabindex: editable ? "0" : null,
      "aria-label": editable ? i18n.t("workflows.editActionNamed", {
        name: readableNodeName(node),
      }) : null,
      onclick: edit,
      onkeydown: (event) => {
        if (editable && (event.key === "Enter" || event.key === " ")) {
          event.preventDefault();
          edit();
        }
      },
    }, [
      svgEl("title", {
        text: node.handler_name
          ? `${node.node_id} · ${node.handler_name}@${node.handler_version}`
          : node.node_id,
      }),
      svgEl("clipPath", { id: clipId }, [
        svgEl("rect", { x: 8, y: 0, width: spot.width - 16, height: spot.height }),
      ]),
      svgEl("rect", {
        width: spot.width, height: spot.height,
        rx: node.kind === "terminal" ? spot.height / 2 : 10,
      }),
      node.kind === "terminal"
        ? svgEl("text", {
            class: "graph-terminal-label", x: spot.width / 2, y: spot.height / 2 + 4,
            "text-anchor": "middle", "clip-path": `url(#${clipId})`,
            text: i18n.t("workflows.completed"),
          })
        : svgEl("text", {
            class: "graph-box-id", x: 12, y: 21, "clip-path": `url(#${clipId})`,
            text: readableNodeName(node),
          }),
      node.kind === "terminal" ? null : svgEl("text", {
        class: "graph-box-meta", x: 12, y: 38, "clip-path": `url(#${clipId})`,
        text: label,
      }),
    ]);
  });

  const arrow = svgEl("marker", {
    id: "graphArrow", viewBox: "0 0 8 8", refX: 7, refY: 4,
    markerWidth: 6, markerHeight: 6, orient: "auto-start-reverse",
  }, [svgEl("path", { d: "M0 0 L8 4 L0 8 z" })]);

  return svgEl("svg", {
    class: "workflow-graph", role: "img",
    viewBox: `0 0 ${canvasWidth} ${canvasHeight}`,
    width: canvasWidth, height: canvasHeight,
    "data-canvas-width": canvasWidth, "data-canvas-height": canvasHeight,
    "aria-label": i18n.t("workflows.graphAria", {
      nodes: i18n.number(graph.nodes.length), edges: i18n.number(graph.edges.length),
    }),
  }, [
    svgEl("defs", {}, [arrow]),
    svgEl("g", { transform: `translate(0 ${offsetY})` }, [...edges, ...boxes]),
  ]);
}

/* Each node kind owns an accent; the glyph tile and prompt key pick it up via
 * --row-accent. Terminal reads as neutral slate, matching the design list. */
const KIND_COLOR = {
  action: "blue", human: "amber", decision: "purple", terminal: "muted",
};

/* Inline glyphs (no icon-font dependency): one per kind, plus the row tools.
 * Anything we have no shape for falls back to the action brackets. */
function kindGlyph(kind) {
  const paths = {
    action: ["M9 8l-4 4 4 4", "M15 8l4 4-4 4"],
    human: ["M12 12a3.5 3.5 0 100-7 3.5 3.5 0 000 7", "M5 19a7 7 0 0114 0"],
    decision: ["M12 3l8 9-8 9-8-9z"],
    terminal: ["M4 5h16v14H4z", "M8 10l3 2-3 2", "M13 15h4"],
  };
  return svgEl("svg", {
    width: "20", height: "20", viewBox: "0 0 24 24", fill: "none",
    stroke: "currentColor", "stroke-width": "1.8",
    "stroke-linecap": "round", "stroke-linejoin": "round",
  }, (paths[kind] || paths.action).map((d) => svgEl("path", { d })));
}
function editGlyph() {
  return svgEl("svg", {
    width: "16", height: "16", viewBox: "0 0 24 24", fill: "none",
    stroke: "currentColor", "stroke-width": "1.8",
    "stroke-linecap": "round", "stroke-linejoin": "round",
  }, [
    svgEl("path", { d: "M4 20h4L18.5 9.5a2.1 2.1 0 00-3-3L5 17z" }),
    svgEl("path", { d: "M13.5 6.5l3 3" }),
  ]);
}
function noteGlyph() {
  return svgEl("svg", {
    width: "15", height: "15", viewBox: "0 0 24 24", fill: "none",
    stroke: "currentColor", "stroke-width": "1.8",
    "stroke-linecap": "round", "stroke-linejoin": "round",
  }, [svgEl("path", { d: "M7 7h10l-3-3 M17 17H7l3 3" })]);
}

/* No authored prompt? Show the real I/O signature (and a human's task kind)
 * instead of the design's placeholder copy — same slot, truthful content. */
function stepNote(node) {
  const ins = (node.inputs || []).map((port) => port.id).filter(Boolean);
  const outs = (node.outputs || []).map((port) => port.id).filter(Boolean);
  const taskKind = node.kind === "human" ? node.config?.task_kind : null;
  if (!ins.length && !outs.length && !taskKind) return null;
  return el("div", { class: "defn-note" }, [
    noteGlyph(),
    ins.length
      ? el("span", { class: "defn-io" }, ins.map((id) => el("span", { class: "defn-chip in", text: id })))
      : null,
    ins.length && outs.length ? el("span", { class: "defn-arrow", text: "→" }) : null,
    outs.length
      ? el("span", { class: "defn-io" }, outs.map((id) => el("span", { class: "defn-chip out", text: id })))
      : null,
    taskKind ? el("span", { class: "defn-kind defn-kind-amber", text: taskKind }) : null,
  ]);
}

function definitionList(definition, graph = null, actionEditors = {}, onEditAction = null) {
  const positions = new Map(
    (graph?.layout?.positions || []).map((item) => [item.node_id, item]),
  );
  const nodes = [...(definition?.nodes || [])];
  if (positions.size) nodes.sort((left, right) => {
    const a = positions.get(left.id);
    const b = positions.get(right.id);
    if (!a && !b) return left.id.localeCompare(right.id);
    if (!a) return 1;
    if (!b) return -1;
    return a.depth - b.depth || a.lane - b.lane || left.id.localeCompare(right.id);
  });
  return el("div", { class: "definition-list defn-list" }, nodes.map((node) => {
    const color = KIND_COLOR[node.kind] || "muted";
    const prompt = typeof node.config?.prompt === "string" ? node.config.prompt.trim() : "";
    const editable = node.kind === "action" && actionEditors[node.id];
    return el("div", { class: `defn-row kind-${node.kind}` }, [
      el("div", { class: "defn-id" }, [
        el("span", { class: `defn-glyph glyph-${color}`, "aria-hidden": "true" }, [kindGlyph(node.kind)]),
        el("div", { class: "defn-id-text" }, [
          el("div", { class: "defn-name", text: node.id }),
          el("div", { class: "defn-meta" }, [
            el("span", {
              class: `defn-kind${color === "amber" || color === "purple" ? ` defn-kind-${color}` : ""}`,
              text: node.kind,
            }),
            node.handler ? el("span", {
              class: "defn-handler", text: node.handler.name.replace(/^agent\./, ""),
            }) : null,
          ]),
        ]),
      ]),
      // The authored prompt is what this step actually asks its Agent; a
      // reader scanning the definition wants it next to the handler name, not
      // buried in the source. No prompt → the I/O signature fills the slot.
      prompt
        ? el("div", { class: "defn-prompt" }, [
          el("div", { class: "defn-prompt-bar", text: i18n.t("workflows.promptConfig") }),
          el("div", { class: "defn-prompt-body", title: prompt }, [
            el("span", { class: "defn-prompt-key", text: "prompt:" }),
            el("span", { text: prompt }),
          ]),
        ])
        : stepNote(node),
      editable ? el("div", { class: "defn-tools" }, [
        el("button", {
          type: "button", class: "defn-edit",
          "aria-label": i18n.t("workflows.editAction"),
          title: i18n.t("workflows.editAction"),
          onclick: () => onEditAction?.(node.id),
        }, [editGlyph()]),
      ]) : null,
    ]);
  }));
}

function openActionEditorDialog(workflow, nodeId, onSaved) {
  const editor = workflow.action_editors?.[nodeId];
  const node = workflow.definition?.nodes?.find((item) => item.id === nodeId);
  if (!editor || !node || node.kind !== "action") return;

  const titleId = `actionTitle-${nodeId}`;
  const agentId = `actionAgent-${nodeId}`;
  const promptId = `actionPrompt-${nodeId}`;
  const currentHandler = `${node.handler?.name || ""}@${node.handler?.version || ""}`;
  const title = el("input", {
    id: titleId, type: "text", required: "required", maxlength: "80",
    value: node.label || "",
  });
  const agent = el("select", { id: agentId, required: "required" },
    editor.handlers.map((handler) => {
      const value = `${handler.name}@${handler.version}`;
      return el("option", {
        value, text: handler.name.replace(/^agent\./, ""),
        ...(value === currentHandler ? { selected: "selected" } : {}),
      });
    }));
  const prompt = el("textarea", {
    id: promptId, required: "required", maxlength: "4000",
    text: typeof node.config?.prompt === "string" ? node.config.prompt : "",
  });
  const save = el("button", {
    type: "submit", class: "button primary", text: i18n.t("workflows.saveAction"),
  });
  const dialog = el("dialog", {
    class: "action-editor-dialog",
    "aria-label": i18n.t("workflows.editActionNamed", { name: readableNodeName(node) }),
  });
  const form = el("form", {}, [
    el("h2", { text: i18n.t("workflows.editAction") }),
    el("div", { class: "field" }, [
      el("label", { for: titleId, text: i18n.t("workflows.actionTitle") }), title,
    ]),
    el("div", { class: "field" }, [
      el("label", { for: agentId, text: i18n.t("workflows.actionAgent") }), agent,
    ]),
    el("div", { class: "field" }, [
      el("label", { for: promptId, text: i18n.t("workflows.actionPrompt") }), prompt,
    ]),
    el("div", { class: "actions" }, [
      el("button", {
        type: "button", class: "button", text: i18n.t("action.cancel"),
        onclick: () => dialog.close(),
      }),
      save,
    ]),
  ]);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!form.reportValidity()) return;
    const selected = editor.handlers.find(
      (handler) => `${handler.name}@${handler.version}` === agent.value,
    );
    if (!selected) return;
    save.disabled = true;
    try {
      await api.execute(editor.allowed_command, {
        label: title.value.trim(), handler: selected, prompt: prompt.value.trim(),
      }, `workflow.action.update:${workflow.workflow_id}:${nodeId}:${workflow.latest_version}`);
      dialog.close();
      await onSaved();
    } catch (error) {
      save.disabled = false;
      reportError(error);
    }
  });
  dialog.append(form);
  dialog.addEventListener("close", () => dialog.remove(), { once: true });
  document.body.append(dialog);
  dialog.showModal();
  title.focus();
}

function stepPrompt(config, extraClass = "") {
  const prompt = typeof config?.prompt === "string" ? config.prompt.trim() : "";
  if (!prompt) return null;
  return el("div", {
    class: `muted step-prompt${extraClass ? ` ${extraClass}` : ""}`,
    title: prompt, text: `prompt: ${prompt}`,
  });
}

// The same definition read two ways: the drawing answers "what shape is
// this", the list answers "what exactly is in it". Tabs keep both a click
// away without stacking two long blocks in one panel. The state is local:
// which tab you are on is not worth a URL when the selected workflow is not
// in one either. A draft the server could not lay out has no drawing, so
// there is nothing to tab between and the list stands alone.
function workflowDefinitionTabs(
  graph, definition, definitionKey = "workflows.definition",
  actionEditors = {}, onEditAction = null,
) {
  if (!graph) return definitionList(
    definition, null, actionEditors, onEditAction,
  );
  let graphZoom = 1;
  const graphPane = () => {
    const drawing = workflowGraphView(graph, actionEditors, onEditAction);
    const level = el("output", {
      class: "graph-zoom-level mono", "aria-live": "polite",
    });
    const zoomOut = el("button", {
      type: "button", class: "button graph-zoom-button",
      "aria-label": i18n.t("workflows.zoomOut"),
      title: i18n.t("workflows.zoomOut"), text: "−",
    });
    const zoomIn = el("button", {
      type: "button", class: "button graph-zoom-button",
      "aria-label": i18n.t("workflows.zoomIn"),
      title: i18n.t("workflows.zoomIn"), text: "+",
    });
    const applyZoom = () => {
      const width = Number(drawing.dataset.canvasWidth);
      const height = Number(drawing.dataset.canvasHeight);
      drawing.setAttribute("width", String(Math.round(width * graphZoom)));
      drawing.setAttribute("height", String(Math.round(height * graphZoom)));
      level.textContent = i18n.t("workflows.zoomLevel", {
        percent: i18n.number(Math.round(graphZoom * 100)),
      });
      zoomOut.disabled = graphZoom <= 0.5;
      zoomIn.disabled = graphZoom >= 1.5;
    };
    zoomOut.addEventListener("click", () => {
      graphZoom = Math.max(0.5, Number((graphZoom - 0.1).toFixed(1)));
      applyZoom();
    });
    zoomIn.addEventListener("click", () => {
      graphZoom = Math.min(1.5, Number((graphZoom + 0.1).toFixed(1)));
      applyZoom();
    });
    applyZoom();
    return el("div", { class: "workflow-graph-pane" }, [
      el("div", {
        class: "graph-zoom-controls",
        "aria-label": i18n.t("workflows.zoomControls"),
      }, [zoomOut, level, zoomIn]),
      el("div", { class: "workflow-graph-scroll" }, [drawing]),
    ]);
  };
  const panes = {
    graph: graphPane,
    definition: () => definitionList(
      definition, graph, actionEditors, onEditAction,
    ),
  };
  const content = el("section", { class: "run-tab-content workflow-tab-content" });
  const tabs = el("nav", {
    class: "run-tabs", "aria-label": i18n.t("workflows.definitionTabs"),
  });
  const show = (name) => {
    for (const button of tabs.children) {
      const active = button.dataset.workflowTab === name;
      button.classList.toggle("active", active);
      if (active) button.setAttribute("aria-current", "page");
      else button.removeAttribute("aria-current");
    }
    content.dataset.activeTab = name;
    content.replaceChildren(panes[name]());
  };
  for (const name of ["graph", "definition"]) {
    tabs.append(el("button", {
      class: "run-tab", "data-workflow-tab": name,
      text: i18n.t(name === "graph" ? "workflows.graph" : definitionKey),
      onclick: () => show(name),
    }));
  }
  show("graph");
  return el("div", { class: "workflow-tabs" }, [tabs, content]);
}

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
    el("span", { class: "custom-select-chevron", "aria-hidden": "true" }),
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
  const context = {
    api, el, i18n, reportError,
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




function workflowStartCommand(entry) {
  return (entry?.allowed_commands || []).find((item) => item.command === "run.start");
}

function prepareSimplifiedComposer(summary, entries) {
  if (summary && simplifiedComposerState.runId !== summary.run_id) {
    simplifiedComposerState.runId = summary.run_id;
    simplifiedComposerState.workflowId = summary.workflow_id;
    simplifiedComposerState.goal = summary.goal || "";
  } else if (!summary && simplifiedComposerState.runId) {
    simplifiedComposerState.runId = null;
    simplifiedComposerState.workflowId = "";
    simplifiedComposerState.goal = "";
  }
  if (!entries.some((item) => item.workflow_id === simplifiedComposerState.workflowId)) {
    const ready = entries.find(
      (item) => item.goal_readiness === "ready" && workflowStartCommand(item),
    );
    simplifiedComposerState.workflowId = ready?.workflow_id || entries[0]?.workflow_id || "";
  }
}

function renderSimplifiedComposer(root, entries, summary) {
  prepareSimplifiedComposer(summary, entries);
  const locked = Boolean(summary);
  const selectedEntry = () => entries.find(
    (item) => item.workflow_id === simplifiedComposerState.workflowId,
  );
  const workflow = el("select", {
    id: "simplifiedWorkflow", disabled: locked ? "disabled" : null,
    "aria-label": i18n.t("newRun.workflow"),
    onchange: (event) => {
      simplifiedComposerState.workflowId = event.target.value;
      render();
    },
  });
  if (summary && !entries.some((item) => item.workflow_id === summary.workflow_id)) {
    workflow.append(el("option", {
      value: summary.workflow_id, text: summary.workflow_id,
      selected: simplifiedComposerState.workflowId === summary.workflow_id ? "selected" : null,
    }));
  }
  for (const entry of entries) {
    const readiness = entry.goal_readiness === "ready"
      ? "" : ` · ${i18n.t(`workflows.readiness.${entry.goal_readiness}`)}`;
    const nodes = i18n.t("workflows.nodeCount", {
      count: i18n.number(entry.summary.node_count),
    });
    workflow.append(el("option", {
      value: entry.workflow_id,
      text: `${entry.name} · ${nodes}${readiness}`,
      selected: entry.workflow_id === simplifiedComposerState.workflowId ? "selected" : null,
    }));
  }
  if (!entries.length) workflow.append(el("option", {
    value: "", text: i18n.t("workflows.empty"), selected: "selected",
  }));
  const goal = el("textarea", {
    id: "simplifiedGoal", required: "required",
    disabled: locked ? "disabled" : null,
    placeholder: i18n.t("simplified.start.placeholder"),
    text: simplifiedComposerState.goal,
    oninput: (event) => { simplifiedComposerState.goal = event.target.value; },
  });
  const problem = el("div", {
    class: "banner error simplified-composer-problem", hidden: "hidden",
  });
  const chosen = selectedEntry();
  const allowed = chosen?.goal_readiness === "ready" ? workflowStartCommand(chosen) : null;
  const start = el("button", {
    class: "button primary", type: "submit", id: "newGoalStart",
    disabled: locked || !allowed ? "disabled" : null,
    text: locked
      ? i18n.t("simplified.run.inProgress")
      : i18n.t("newRun.submit"),
  });
  const form = el("form", {
    class: "panel simplified-workspace-composer",
    onsubmit: async (event) => {
      event.preventDefault();
      if (locked || !goal.value.trim() || !goal.reportValidity()) return;
      start.disabled = true;
      problem.hidden = true;
      try {
        const fresh = (await api.workflowCatalog()).data.workflows.find(
          (item) => item.workflow_id === simplifiedComposerState.workflowId,
        );
        if (!fresh || fresh.goal_readiness !== "ready") {
          problem.textContent = i18n.t("newRun.workflow.unavailable");
          problem.hidden = false;
          return;
        }
        if (chosen && fresh.latest_version !== chosen.latest_version) {
          announce(i18n.t("newRun.workflow.changed"), "error");
          await render();
          return;
        }
        const command = workflowStartCommand(fresh);
        if (!command) {
          problem.textContent = i18n.t("newRun.workflow.forbidden");
          problem.hidden = false;
          return;
        }
        const goalText = goal.value.trim();
        simplifiedComposerState.goal = goalText;
        const started = await api.execute(command, {
          workflow_id: fresh.workflow_id,
          workflow_version: fresh.latest_version,
          goal: goalText,
          input: bindGoalInput(fresh, goalText, {}),
        }, `run.start:${crypto.randomUUID ? crypto.randomUUID() : Date.now()}`);
        simplifiedComposerState.runId = started.data.run_id;
        navigate({ view: "run", runId: started.data.run_id });
      } catch (error) {
        if (error instanceof ApiError && error.code === "active_goal_exists") {
          const active = error.details.active_goal;
          announce(i18n.t("newRun.active.exists", {
            goal: active?.display_name || active?.run_id || "",
          }), "info");
          if (active?.run_id) navigate({ view: "run", runId: active.run_id });
        } else if (error instanceof ApiError && error.code === "handler_unavailable") {
          announce(i18n.t("newRun.handler.unavailable"), "error");
          navigate({
            view: "workflow", workflowId: simplifiedComposerState.workflowId, runId: null,
          });
        } else {
          problem.textContent = error instanceof ApiError
            ? i18n.t(error.messageKey, { message: error.message })
            : i18n.t("error.generic");
          problem.hidden = false;
          reportError(error);
        }
      } finally {
        if (start.isConnected) {
          const current = selectedEntry();
          start.disabled = locked
            || current?.goal_readiness !== "ready" || !workflowStartCommand(current);
        }
      }
    },
  }, [
    el("div", { class: "simplified-composer-fields" }, [
      el("div", { class: "field" }, [
        el("label", { for: "simplifiedWorkflow", text: i18n.t("newRun.workflow") }),
        el("div", { class: "simplified-workflow-picker" }, [
          workflow,
          el("button", {
            class: "button", type: "button", id: "homeGenerateWorkflow",
            text: i18n.t("generate.action"),
            onclick: () => navigate({ view: "workflows", runId: null }),
          }),
        ]),
      ]),
      el("div", { class: "field simplified-goal-field" }, [
        el("label", { for: "simplifiedGoal", text: i18n.t("newRun.goal") }),
        goal,
      ]),
      el("div", { class: "actions simplified-composer-actions" }, [start]),
      chosen && !allowed ? el("div", {
        class: "banner warn", text: i18n.t("simplified.workflow.unavailable"),
      }) : null,
      problem,
    ]),
  ]);
  root.append(form);
  if (focusSimplifiedGoalOnRender && !locked) {
    focusSimplifiedGoalOnRender = false;
    setTimeout(() => goal.focus(), 0);
  }
}

async function renderSimplifiedWorkspace(root, selectedRunId = null) {
  const [dashboardResponse, catalogResponse] = await Promise.all([
    api.dashboard(), api.workflowCatalog(),
  ]);
  const runId = selectedRunId || dashboardResponse.data.active_goal?.run_id || null;
  const entries = catalogResponse.data.workflows;
  const summary = runId ? (await api.runSummary(runId)).data : null;
  const historicalDetail = Boolean(
    selectedRunId && summary && TERMINAL_RUN_STATUSES.has(summary.status),
  );
  if (!historicalDetail) renderSimplifiedComposer(root, entries, summary);
  if (runId && summary) await renderSimplifiedRun(root, runId, summary);
}

function historyDayKey(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "unknown";
  const parts = [date.getFullYear(), date.getMonth() + 1, date.getDate()]
    .map((part) => String(part).padStart(2, "0"));
  return parts.join("-");
}

function historyDayLabel(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return i18n.t("history.date.unknown");
  const today = new Date();
  const yesterday = new Date(today);
  yesterday.setDate(today.getDate() - 1);
  const key = historyDayKey(value);
  if (key === historyDayKey(today)) return i18n.t("history.date.today");
  if (key === historyDayKey(yesterday)) return i18n.t("history.date.yesterday");
  return new Intl.DateTimeFormat(i18n.locale, { dateStyle: "long" }).format(date);
}

function historyTime(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value || "";
  return new Intl.DateTimeFormat(i18n.locale, { timeStyle: "short" }).format(date);
}

function historyDuration(run) {
  const started = new Date(run.created_at).getTime();
  const finished = new Date(run.updated_at).getTime();
  if (!Number.isFinite(started) || !Number.isFinite(finished) || finished < started) return "";
  const minutes = Math.floor((finished - started) / 60_000);
  if (minutes < 1) return i18n.t("history.duration.short");
  if (minutes < 60) return i18n.t("history.duration.minutes", {
    count: i18n.number(minutes),
  });
  return i18n.t("history.duration.hours", {
    hours: i18n.number(Math.floor(minutes / 60)),
    minutes: i18n.number(minutes % 60),
  });
}

function historyArtifactCount(count) {
  if (!count) return i18n.t("history.artifacts.zero");
  return i18n.t(count === 1 ? "history.artifacts.one" : "history.artifacts.many", {
    count: i18n.number(count),
  });
}

function historyGoalRow(run, workflowNames) {
  const duration = historyDuration(run);
  const metadata = [
    workflowNames.get(run.workflow_id) || run.workflow_id,
    historyTime(run.updated_at),
    duration,
  ].filter(Boolean).join(" · ");
  return el("button", {
    class: "history-goal-row",
    onclick: () => navigate({ view: "run", runId: run.run_id }),
  }, [
    el("span", { class: "history-goal-copy" }, [
      el("strong", { class: "history-goal-title", text: runName(run) }),
      el("span", { class: "history-goal-meta muted", text: metadata }),
      el("span", {
        class: `history-goal-artifacts${run.artifact_count ? " available" : ""}`,
        text: historyArtifactCount(run.artifact_count),
      }),
    ]),
    el("span", { class: "history-goal-tail" }, [
      pill(run.status),
      el("span", { class: "history-goal-chevron", "aria-hidden": "true", text: "›" }),
    ]),
  ]);
}

function appendHistoryRuns(list, runs, workflowNames) {
  for (const run of runs) {
    const key = historyDayKey(run.updated_at);
    let group = [...list.children].find((item) => item.dataset.historyDay === key);
    if (!group) {
      group = el("section", { class: "history-day-group", "data-history-day": key }, [
        el("h3", { class: "history-day-heading", text: historyDayLabel(run.updated_at) }),
        el("div", { class: "history-day-rows" }),
      ]);
      list.append(group);
    }
    group.querySelector(".history-day-rows").append(historyGoalRow(run, workflowNames));
  }
}

async function renderHistory(root, selectedRunId = null) {
  root.append(el("header", { class: "view-intro" }, [
    el("div", {}, [
      el("h2", { text: i18n.t("history.eyebrow") }),
      el("p", { class: "muted", text: i18n.t("history.description") }),
    ]),
  ]));
  const search = el("input", {
    type: "search", value: goalFilters.q,
    placeholder: i18n.t("goals.search.placeholder"),
    "aria-label": i18n.t("goals.search.label"),
  });
  root.append(el("form", {
    class: "filter-bar history-filter-bar",
    onsubmit: (event) => {
      event.preventDefault();
      goalFilters.q = search.value.trim();
      render();
    },
  }, [
    search,
    el("button", { class: "button", type: "submit", text: i18n.t("action.search") }),
    goalFilters.q ? el("button", {
      class: "button", type: "button", text: i18n.t("action.clear"),
      onclick: () => {
        goalFilters.q = "";
        render();
      },
    }) : null,
  ]));
  root.append(el("div", {
    class: "history-status-filters", role: "group",
    "aria-label": i18n.t("goals.filter.status"),
  }, [
    ["", "history.status.all"],
    ["succeeded", "status.succeeded"],
    ["failed", "status.failed"],
    ["cancelled", "status.cancelled"],
  ].map(([status, label]) => el("button", {
    class: `button history-status-filter${goalFilters.status === status ? " active" : ""}`,
    type: "button", "aria-pressed": String(goalFilters.status === status),
    text: i18n.t(label),
    onclick: () => {
      goalFilters.status = status;
      render();
    },
  }))));
  const [response, catalogResponse] = await Promise.all([
    api.listRuns({
      limit: 25, q: goalFilters.q, status: goalFilters.status, terminalOnly: true,
    }),
    api.workflowCatalog(),
  ]);
  const workflowNames = new Map(
    catalogResponse.data.workflows.map((item) => [item.workflow_id, item.name]),
  );
  const runs = response.data.runs;
  if (!runs.length) {
    root.append(el("div", {
      class: "empty panel",
      text: goalFilters.q || goalFilters.status
        ? i18n.t("history.noMatches") : i18n.t("history.empty"),
    }));
    return;
  }
  const list = el("section", { class: "history-goal-list panel" });
  appendHistoryRuns(list, runs, workflowNames);
  root.append(list);
  let loadedCount = runs.length;
  let nextCursor = response.next_cursor;
  const count = el("p", { class: "history-result-count muted" });
  const updateCount = () => {
    count.textContent = i18n.t(
      nextCursor ? "history.resultCount.more" : "history.resultCount",
      { count: i18n.number(loadedCount) },
    );
  };
  const loadMore = el("button", {
    class: "button", hidden: nextCursor ? null : "hidden",
    text: i18n.t("action.loadMore"),
    onclick: async () => {
      loadMore.disabled = true;
      try {
        const next = await api.listRuns({
          cursor: nextCursor, limit: 25, q: goalFilters.q,
          status: goalFilters.status, terminalOnly: true,
        });
        appendHistoryRuns(list, next.data.runs, workflowNames);
        loadedCount += next.data.runs.length;
        nextCursor = next.next_cursor;
        loadMore.hidden = !nextCursor;
        updateCount();
      } catch (error) {
        reportError(error);
      } finally {
        loadMore.disabled = false;
      }
    },
  });
  updateCount();
  root.append(el("footer", { class: "history-list-footer" }, [count, loadMore]));
}


function simplifiedStepName(node) {
  const label = node?.label;
  if (typeof label === "string" && label.trim()) return label.trim();
  const nodeId = typeof node?.node_id === "string" ? node.node_id.trim() : "";
  return nodeId ? nodeId.replace(/[_-]+/g, " ") : i18n.t("simplified.workflow.step");
}

function simplifiedStepRunner(node) {
  const handler = typeof node?.handler_name === "string" ? node.handler_name.trim() : "";
  if (handler.startsWith("agent.")) {
    return {
      kind: "agent",
      text: i18n.t("simplified.execution.agent", { name: handler.slice("agent.".length) }),
    };
  }
  if (handler) {
    return {
      kind: "tool",
      text: i18n.t("simplified.execution.tool", { name: handler }),
    };
  }
  if (node?.kind === "human") {
    return { kind: "human", text: i18n.t("simplified.execution.human") };
  }
  return null;
}

function simplifiedStepOutput(runId, nodeRunId, { live }) {
  const log = el("pre", {
    class: "console-log simplified-step-output-log", role: "log", tabindex: "0", hidden: true,
  });
  const state = el("span", { class: "muted", text: i18n.t("run.console.empty") });
  const details = el("details", { class: "simplified-step-output" }, [
    el("summary", { text: i18n.t("simplified.execution.output") }),
    el("div", { class: "simplified-step-output-body" }, [state, log]),
  ]);
  let after = 0;
  let timer = null;
  let loading = false;
  let stopped = false;

  const draw = (chunks) => {
    for (const chunk of chunks) {
      log.append(el("span", {
        class: `console-chunk ${chunk.stream}`, text: chunk.text,
      }));
    }
    if (log.childElementCount) log.hidden = false;
  };

  const poll = async () => {
    if (stopped || loading || !details.open) return;
    loading = true;
    if (!log.childElementCount) state.textContent = i18n.t("simplified.execution.output.loading");
    try {
      const response = await api.runOutput(runId, after, 200, nodeRunId);
      after = response.data.after;
      draw(response.data.chunks);
      state.textContent = log.childElementCount
        ? i18n.t(live ? "run.console.following" : "run.console.finished")
        : i18n.t("run.console.empty");
      if (response.data.has_more) timer = setTimeout(poll, 0);
      else if (live) timer = setTimeout(poll, 2000);
    } catch (error) {
      stopped = true;
      state.textContent = error instanceof ApiError && error.status === 403
        ? i18n.t("run.console.forbidden")
        : i18n.t("run.console.unavailable");
    } finally {
      loading = false;
    }
  };

  details.addEventListener("toggle", () => {
    if (details.open) poll();
    else if (timer) {
      clearTimeout(timer);
      timer = null;
    }
  });

  const previous = activeViewCleanup;
  activeViewCleanup = () => {
    stopped = true;
    if (timer) clearTimeout(timer);
    if (previous) previous();
  };
  return details;
}

function simplifiedExecutionPanel(graph, summary, reload) {
  const header = () => el("div", { class: "panel-head simplified-execution-head" }, [
    el("div", {}, [
      el("div", { class: "panel-title", text: i18n.t("simplified.execution") }),
      el("div", { class: "panel-subtitle", text: TERMINAL_RUN_STATUSES.has(summary.status)
        ? i18n.t(`simplified.run.${summary.status}`)
        : summary.current_step?.label || i18n.t("simplified.run.inProgress") }),
    ]),
    el("div", { class: "actions" }, [
      pill(summary.status),
      ...commandButtons(summary.allowed_commands || [], reload),
    ]),
  ]);
  if (!graph) {
    const status = TERMINAL_RUN_STATUSES.has(summary.status) ? summary.status : "running";
    const mark = status === "succeeded" ? "✓"
      : ["failed", "cancelled"].includes(status) ? "×" : "●";
    return el("section", { class: "panel simplified-execution simplified-run-hero" }, [
      header(),
      el("div", { class: "panel-body" }, [
        el("div", { class: `simplified-step-row ${status}` }, [
          el("span", { class: "simplified-step-mark", text: mark }),
          el("div", { class: "simplified-step-copy" }, [
            el("strong", { text: summary.current_step?.label || i18n.t("simplified.execution.preparing") }),
            el("span", { class: "muted", text: TERMINAL_RUN_STATUSES.has(summary.status)
              ? i18n.t(`simplified.run.${summary.status}`)
              : i18n.t("simplified.run.inProgress") }),
          ]),
          pill(status),
        ]),
      ]),
    ]);
  }
  if (graph.error) {
    return el("section", { class: "panel simplified-execution simplified-run-hero" }, [
      header(),
      el("div", { class: "panel-body" }, [
        dataState(el, i18n, "error", { onRetry: reload }),
      ]),
    ]);
  }
  const definition = graph.definition;
  const overlay = graph.runtime_overlay;
  const statuses = new Map();
  for (const node of overlay.nodes) {
    const current = statuses.get(node.node_id);
    if (!current || node.generation >= current.generation) statuses.set(node.node_id, node);
  }
  const positions = new Map(
    definition.layout.positions.map((item) => [item.node_id, item]),
  );
  const executableNodes = definition.nodes.filter((node) => node.kind !== "terminal");
  const nodes = (executableNodes.length ? executableNodes : definition.nodes).slice().sort((left, right) => {
    const leftPosition = positions.get(left.node_id) || { depth: 0, lane: 0 };
    const rightPosition = positions.get(right.node_id) || { depth: 0, lane: 0 };
    return leftPosition.depth - rightPosition.depth || leftPosition.lane - rightPosition.lane;
  });
  const rows = nodes.map((node, index) => {
    const runtime = statuses.get(node.node_id);
    const runner = simplifiedStepRunner(node);
    const status = runtime?.status || "pending";
    const mark = status === "succeeded" ? "✓"
      : ["failed", "cancelled"].includes(status) ? "×"
        : status === "running" ? "●" : String(index + 1);
    return el("div", { class: `simplified-step-row ${status}` }, [
      el("span", { class: "simplified-step-mark", text: mark }),
      el("div", { class: "simplified-step-copy" }, [
        el("strong", { text: simplifiedStepName(node) }),
        stepPrompt(node.config, "simplified-step-prompt"),
        el("div", { class: "simplified-step-meta" }, [
          runner ? el("span", {
            class: `simplified-step-runner ${runner.kind}`, text: runner.text,
          }) : null,
          runtime ? el("span", { class: "muted", text: i18n.t("simplified.execution.attempts", {
            count: i18n.number(runtime.attempts),
          }) }) : el("span", { class: "muted", text: i18n.t("simplified.execution.waiting") }),
        ]),
      ]),
      pill(status),
      runtime?.node_run_id && node.handler_name
        ? simplifiedStepOutput(summary.run_id, runtime.node_run_id, { live: status === "running" })
        : null,
    ]);
  });
  return el("section", { class: "panel simplified-execution simplified-run-hero" }, [
    header(),
    el("div", { class: "panel-body simplified-step-list" }, rows),
  ]);
}

function simplifiedResultBody(outcome) {
  if (outcome.state !== "available") {
    return el("div", { class: "muted", text: i18n.t(
      `simplified.result.state.${outcome.state}`,
    ) });
  }
  if (!outcome.content_visible) {
    return el("div", { class: "muted", text: i18n.t(
      "simplified.result.contentHidden",
    ) });
  }
  if (outcome.kind === "text") {
    return el("div", { class: "simplified-result-value", text: outcome.value });
  }
  if (outcome.kind === "json") {
    return el("pre", {
      class: "code-block simplified-result-value",
      text: JSON.stringify(outcome.value, null, 2),
    });
  }
  return null;
}

async function renderSimplifiedRun(root, runId, summary) {
  const graphPromise = summary.plan_version
    ? api.graph(runId).then((response) => response.data).catch((error) => ({ error }))
    : Promise.resolve(null);
  const [responsibilityResponse, artifactResponse, outcomeResponse, graph] = await Promise.all([
    api.responsibilities(runId),
    api.artifacts({ runId, limit: 25 }),
    api.outcome(runId),
    graphPromise,
  ]);
  const responsibilities = responsibilityResponse.data.responsibilities;
  const artifacts = artifactResponse.data.artifacts;
  const outcome = outcomeResponse.data.result;
  const reload = () => navigate({ view: "run", runId });
  const terminal = TERMINAL_RUN_STATUSES.has(summary.status);

  root.append(simplifiedExecutionPanel(graph, summary, reload));

  if (responsibilities.length) {
    root.append(el("section", { class: "panel simplified-attention" }, [
      el("div", { class: "panel-head" }, [
        el("div", { class: "panel-title", text: i18n.t("simplified.attention") }),
      ]),
      el("div", { class: "panel-body responsibility-list" }, responsibilities.map((item) =>
        el("div", { class: "responsibility-row" }, [
          el("div", {}, [
            el("strong", { text: item.label }),
            item.detail ? el("div", { class: "muted", text: item.detail }) : null,
          ]),
          pill(item.status),
          el("div", { class: "actions" }, commandButtons(item.allowed_commands || [], reload)),
        ]))),
    ]));
  }

  root.append(el("section", { class: "panel simplified-run-info" }, [
    el("div", { class: "panel-head" }, [
      el("div", { class: "panel-title", text: i18n.t("simplified.runInfo") }),
    ]),
    el("div", { class: "panel-body" }, [
      el("dl", { class: "fact-grid" }, [
        el("div", {}, [
          el("dt", { text: i18n.t("simplified.runInfo.id") }),
          el("dd", { class: "mono", text: summary.run_id }),
        ]),
        el("div", {}, [
          el("dt", { text: i18n.t("simplified.runInfo.workflow") }),
          el("dd", { text: summary.workflow_id }),
        ]),
        el("div", {}, [
          el("dt", { text: i18n.t("simplified.runInfo.version") }),
          el("dd", { text: summary.workflow_version ? `v${i18n.number(summary.workflow_version)}` : "—" }),
        ]),
        el("div", {}, [
          el("dt", { text: i18n.t("simplified.runInfo.started") }),
          el("dd", { text: i18n.dateTime(summary.created_at) }),
        ]),
        el("div", {}, [
          el("dt", { text: i18n.t("simplified.runInfo.updated") }),
          el("dd", { text: i18n.dateTime(summary.updated_at) }),
        ]),
      ]),
    ]),
  ]));

  root.append(el("section", { class: "panel simplified-result" }, [
    el("div", { class: "panel-head" }, [
      el("div", { class: "panel-title", text: i18n.t("simplified.result") }),
    ]),
    el("div", { class: "panel-body" }, [
      pill(summary.status),
      simplifiedResultBody(outcome),
    ]),
  ]));

  root.append(el("section", { class: "simplified-artifacts" }, [
    el("div", { class: "section-heading" }, [
      el("h2", { text: i18n.t("simplified.artifacts") }),
    ]),
    artifacts.length
      ? el("div", { class: "artifact-grid" }, artifacts.map((item) => artifactCard(item)))
      : el("div", {
        class: "empty panel",
        text: i18n.t(terminal
          ? "simplified.artifacts.empty" : "simplified.artifacts.pending"),
      }),
  ]));
}







/** What the Handlers' processes printed, followed while the run is alive.
 *
 * Values in the list below are results; this is the account of getting there.
 * It is the only thing an attempt that ended `unknown_external_result` leaves,
 * which is exactly when an operator most needs to read it.
 */

/** One recorded value, readable rather than merely present.
 *
 * An Agent's answer is prose, and prose crammed into one JSON line at 500
 * characters is data you can prove you stored and cannot actually read.
 */


/** The plan, in three separately-labelled views.
 *
 * Definition, overlay and diff are fetched and rendered apart, and the overlay
 * is only drawn against the plan version it names. Painting last version's
 * statuses onto this version's graph is the bug this shape prevents; showing
 * "no run state for this version" is the correct, honest alternative.
 */





/** A cursor-paged section. Paging is the server's; the UI only carries tokens. */



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


/* The Artifact opens as a modal over whatever page listed it — the Run shows
 * its outputs inline, and the detail is one click away, no route between. */
function openArtifactDialog(artifactId) {
  const dialog = el("dialog", {
    class: "artifact-detail artifact-dialog", "aria-label": i18n.t("artifacts.detail"),
  }, [dataState(el, i18n, "loading")]);
  dialog.addEventListener("close", () => dialog.remove(), { once: true });
  document.body.append(dialog);
  dialog.showModal();
  renderArtifactDetail(dialog, artifactId);
}

/* A page glyph, not a four-letter type code: the card now says what the
 * Artifact is by its title, and the exact media type is one hover away. */
function documentIcon(contentType) {
  return el("span", { class: "file-icon", title: contentType }, [
    svgEl("svg", {
      viewBox: "0 0 24 24", width: "18", height: "18", "aria-hidden": "true",
      fill: "none", stroke: "currentColor", "stroke-width": "1.6",
      "stroke-linecap": "round", "stroke-linejoin": "round",
    }, [
      svgEl("path", { d: "M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z" }),
      svgEl("path", { d: "M14 3v5h5" }),
      svgEl("path", { d: "M9 13h6" }),
      svgEl("path", { d: "M9 17h4" }),
    ]),
  ]);
}

/* An image thumbnail that degrades to the document icon.
 *
 * The <img> fetches the content endpoint itself, so a revoked ACL, a missing
 * Blob or a type the server declines to inline all arrive as a load error
 * rather than a broken picture. */
function artifactThumb(item) {
  const badge = documentIcon(item.content_type);
  if (!item.image_previewable) return badge;
  const image = el("img", {
    class: "artifact-thumb", loading: "lazy", decoding: "async",
    src: api.artifactContentUrl(item.artifact_id), alt: "",
    onerror: () => {
      image.replaceWith(badge);
    },
  });
  return image;
}

function artifactCard(item, selected = false) {
  // The port that produced it is the fallback name: a document with no title
  // of its own must not be given one the file does not contain.
  const name = item.display_name || item.title || item.output_port_id;
  return el("article", { class: `artifact-card panel list-option-card${selected ? " selected" : ""}` }, [
    el("button", {
      class: "artifact-card-main",
      "aria-current": selected ? "true" : null,
      onclick: () => openArtifactDialog(item.artifact_id),
    }, [
      el("span", { class: `artifact-top${item.image_previewable ? " artifact-top-image" : ""}` }, [
        artifactThumb(item),
        el("span", { class: "artifact-size", text: i18n.t("artifacts.size", {
          size: i18n.number(item.size_bytes),
        }) }),
      ]),
      el("strong", { class: "artifact-name", text: name }),
      el("span", { class: "artifact-origin" }, [
        el("span", { class: "artifact-origin-label muted", text: i18n.t("artifacts.goal") }),
        // One line, clipped by CSS; the full title stays reachable on hover.
        el("span", {
          class: "artifact-origin-value", text: item.goal || item.run_id,
          title: item.goal || item.run_id,
        }),
      ]),
      el("span", { class: "artifact-origin" }, [
        el("span", { class: "artifact-origin-label muted", text: i18n.t("artifacts.workflow") }),
        // The `workflow:` prefix is the id's kind, and the row already says
        // which kind this is. The full id stays on hover and on the detail.
        el("span", {
          class: "artifact-origin-value mono", title: item.workflow_id,
          text: item.workflow_id.replace(/^workflow:/, ""),
        }),
      ]),
      // Media type, producer and Artifact id are addressing, not identity:
      // they belong on the detail panel, where one Artifact is already open.
    ]),
  ]);
}

/* The Artifact as itself: the picture, or the text, or an honest note that
 * this one cannot be shown here.
 *
 * The text loads on open rather than behind a button — the dialog exists to
 * read the Artifact — and its own failure stays inside this box so a preview
 * the actor may not read never takes the metadata down with it. */
function artifactContent(item) {
  if (item.image_previewable) {
    const holder = el("div", { class: "artifact-content" });
    const load = () => {
      const image = el("img", {
        // Not lazy: the image is what was asked for, and a deferred load leaves
        // an empty box where the Artifact should be.
        class: "artifact-image", decoding: "async",
        src: api.artifactContentUrl(item.artifact_id),
        alt: item.title || item.output_port_id,
        onerror: () => holder.replaceChildren(dataState(el, i18n, "error", {
          onRetry: load,
        })),
      });
      // The browser starts an image request only once the element participates
      // in the document; installing it here also replaces a prior error state.
      holder.replaceChildren(image);
    };
    load();
    return holder;
  }
  if (!item.previewable) {
    return el("div", { class: "muted", text: i18n.t("artifacts.notPreviewable") });
  }
  const holder = el("div", { class: "artifact-content" }, [dataState(el, i18n, "loading")]);
  const load = () => {
    holder.replaceChildren(dataState(el, i18n, "loading"));
    api.artifactPreview(item.artifact_id).then((text) => {
      holder.replaceChildren(el("pre", { class: "artifact-preview", text }));
    }).catch((error) => {
      holder.replaceChildren(dataState(el, i18n, "error", {
        message: error instanceof ApiError
          ? i18n.t(error.messageKey, { message: error.message }) : null,
        onRetry: load,
      }));
      reportError(error);
    });
  };
  load();
  return holder;
}

async function renderArtifactDetail(panel, artifactId) {
  try {
    const detailResponse = await api.artifact(artifactId);
    const item = detailResponse.data;
    panel.replaceChildren(
      el("div", { class: "panel-head" }, [
        el("div", {}, [
          el("div", { class: "eyebrow", text: i18n.t("artifacts.detail") }),
          el("div", { class: "panel-title", text: item.display_name || item.output_port_id }),
        ]),
        el("div", { class: "actions" }, [
          el("a", {
            class: "button", text: i18n.t("artifacts.download"),
            href: api.artifactDownloadUrl(item.artifact_id),
          }),
          el("button", {
            class: "button", text: i18n.t("artifacts.copyId"),
            onclick: async () => {
              await navigator.clipboard.writeText(item.artifact_id);
              announce(i18n.t("artifacts.idCopied"));
            },
          }),
          el("button", {
            class: "button", text: i18n.t("action.close"),
            onclick: () => panel.close(),
          }),
        ]),
      ]),
      el("div", { class: "panel-body" }, [
        // The Artifact itself is what someone opened this for: the picture or
        // the text first, nothing about it in front of it.
        artifactContent(item),
      ]),
    );
  } catch (error) {
    panel.replaceChildren(dataState(el, i18n, "error", {
      message: error instanceof ApiError
        ? i18n.t(error.messageKey, { message: error.message }) : null,
      onRetry: () => renderArtifactDetail(panel, artifactId),
    }));
    reportError(error);
  }
}


function refreshSeconds() {
  const value = Number(localStorage.getItem("orbit.refreshSeconds") || 15);
  return Number.isFinite(value) && value >= 5 && value <= 300 ? value : 15;
}

const REFRESH_INTERVAL_SECONDS = [5, 15, 30, 60, 300];

function syncRefreshIntervalSelect(interval) {
  const current = String(refreshSeconds());
  if (!interval.options.length) {
    for (const seconds of REFRESH_INTERVAL_SECONDS) interval.append(el("option", {
      value: String(seconds), text: i18n.t("settings.seconds", { count: seconds }),
    }));
  } else {
    for (const option of interval.options) {
      option.textContent = i18n.t("settings.seconds", { count: Number(option.value) });
    }
  }
  interval.value = current;
  const label = i18n.t("settings.refresh");
  interval.setAttribute("aria-label", label);
  const wrapper = interval.closest(".custom-select");
  if (wrapper) {
    for (const option of wrapper.querySelectorAll(".custom-select-option")) {
      const nativeOption = [...interval.options].find(
        (item) => item.value === option.dataset.value,
      );
      if (nativeOption) option.textContent = nativeOption.textContent;
    }
    wrapper.querySelector(".custom-select-options")?.setAttribute("aria-label", label);
  }
  syncCustomSelect(interval);
}

function saveRefreshInterval(interval) {
  localStorage.setItem("orbit.refreshSeconds", interval.value);
  scheduleLivePolling();
  announce(i18n.t("settings.saved"));
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


/* ------------------------------------------------ workflow catalog / wizard */

/** Order the catalog the way the author is looking for it.
 *
 * A definition that has never run has no "last used" — an empty timestamp is
 * not the oldest one, so those sort last rather than first.
 */
function sortWorkflows(entries, sort) {
  const byName = (left, right) => left.name.localeCompare(right.name, i18n.locale);
  const newest = (left, right) => String(right || "").localeCompare(String(left || ""));
  return entries.slice().sort((left, right) => {
    if (sort === "name") return byName(left, right);
    if (sort === "recentPublish") {
      return newest(left.created_at, right.created_at) || byName(left, right);
    }
    if (!left.last_run_at !== !right.last_run_at) return left.last_run_at ? -1 : 1;
    return newest(left.last_run_at, right.last_run_at) || byName(left, right);
  });
}

/** Say when a workflow's Agents have moved out from under it.
 *
 * A published plan pins the exact Handler build it was compiled against, and
 * an Agent's build is its CLI version — so upgrading a CLI retires every
 * binding to the old one and the run refuses to start. The server states the
 * drift; the UI shows which node moved and offers the one-click recompile it
 * advertised, when every stale binding has a newer version to land on. */
function handlerDriftNotice(value, redraw) {
  const drift = value.handler_drift || [];
  if (!drift.length) return null;
  const rebind = (value.allowed_commands || []).find(
    (item) => item.command === "workflow.rebind",
  );
  const rows = drift.map((binding) =>
    el("li", {}, [
      el("span", { class: "mono", text: binding.handler_name }),
      el("span", {
        class: "muted mono",
        text: binding.status === "missing"
          ? i18n.t("workflows.drift.missing", { pinned: binding.pinned_version })
          : i18n.t("workflows.drift.changed", {
              pinned: binding.pinned_version, available: binding.available_version,
            }),
      }),
    ]));
  return el("div", { class: "banner warn workflow-drift" }, [
    el("div", { class: "eyebrow", text: i18n.t("workflows.drift.title") }),
    el("ul", { class: "workflow-drift-list" }, rows),
    rebind
      ? el("button", {
          class: "button", id: "rebindWorkflow", text: i18n.t("workflows.drift.rebind"),
          onclick: async (event) => {
            event.currentTarget.disabled = true;
            try {
              await api.execute(rebind, {}, `workflow.rebind:${value.workflow_id}`);
              announce(i18n.t("workflows.drift.rebound"));
              await redraw();
            } catch (error) {
              event.currentTarget.disabled = false;
              reportError(error);
            }
          },
        })
      : el("div", { class: "muted", text: i18n.t("workflows.drift.republish") }),
  ]);
}

async function renderWorkflows(root) {
  let catalog = (await api.workflowCatalog()).data;
  let entries = catalog.workflows;
  // Generation appears only when the server advertised it: capability off or
  // read-only actor simply means the button does not exist.
  let generateCommand = (catalog.allowed_commands || []).find(
    (item) => item.command === "workflow.generate",
  );
  const activeGeneration = generateCommand
    ? (await api.authoringJobs({ active: true, type: "generate" })).data.jobs[0]
    : null;
  if (activeGeneration) simplifiedWorkflowGenerationPending = true;
  if (simplifiedWorkflowGenerationPending && !activeGeneration) {
    catalog = (await api.workflowCatalog()).data;
    entries = catalog.workflows;
    generateCommand = (catalog.allowed_commands || []).find(
      (item) => item.command === "workflow.generate",
    );
    simplifiedWorkflowGenerationPending = false;
  }

  {
    const instruction = el("textarea", {
      id: "generateInstruction", required: "required", maxlength: "4000",
      disabled: activeGeneration ? "disabled" : null,
      placeholder: i18n.t("generate.instructionPh"),
      text: activeGeneration?.prompt || "",
    });
    const problem = el("div", {
      class: "banner error simplified-workflow-generation-problem", hidden: "hidden",
    });
    const submit = el("button", {
      class: "button primary", type: "submit", id: "generateWorkflow",
      disabled: !generateCommand || activeGeneration ? "disabled" : null,
      text: activeGeneration
        ? i18n.t(`authoring.job.${activeGeneration.status}`)
        : i18n.t("generate.action"),
    });
    root.append(el("section", { class: "panel simplified-workflow-generator" }, [
      el("div", { class: "simplified-workflow-generator-copy" }, [
        el("h2", { text: i18n.t("generate.title") }),
        el("p", { class: "muted", text: i18n.t("generate.hint") }),
      ]),
      el("form", {
        class: "simplified-workflow-generator-form",
        onsubmit: async (event) => {
          event.preventDefault();
          if (!generateCommand || activeGeneration
              || !instruction.value.trim() || !instruction.reportValidity()) return;
          submit.disabled = true;
          problem.hidden = true;
          try {
            await api.execute(
              generateCommand,
              { prompt: instruction.value.trim(), display_language: i18n.locale },
              `workflow.generate:${Date.now()}`,
            );
            simplifiedWorkflowGenerationPending = true;
            render();
          } catch (error) {
            problem.textContent = error instanceof ApiError
              ? i18n.t(error.messageKey, { message: error.message })
              : i18n.t("error.generic");
            problem.hidden = false;
            reportError(error);
            submit.disabled = false;
          }
        },
      }, [
        el("div", { class: "field" }, [
          el("label", { for: "generateInstruction", text: i18n.t("generate.instruction") }),
          instruction,
        ]),
        el("div", { class: "actions simplified-workflow-generator-actions" }, [submit]),
        activeGeneration ? el("div", { class: `authoring-job-state ${activeGeneration.status}` }, [
          el("span", { class: "live-dot", "aria-hidden": "true" }),
          el("strong", { text: i18n.t(`authoring.job.${activeGeneration.status}`) }),
        ]) : null,
        problem,
      ]),
    ]));
    root.append(el("header", { class: "view-intro simplified-workflow-list-heading" }, [
      el("div", {}, [
        el("div", { class: "eyebrow", text: i18n.t("workflows.generated.eyebrow") }),
        el("h2", { text: i18n.t("workflows.generated.heading") }),
        el("p", { class: "muted", text: i18n.t("workflows.generated.description") }),
      ]),
    ]));
    if (activeGeneration) {
      const timer = setTimeout(() => render(), 800);
      activeViewCleanup = () => clearTimeout(timer);
    }
  }
  const cards = el("section", { class: "workflow-grid", "aria-label": i18n.t("workflows.list") });

  for (const entry of sortWorkflows(entries, "recentPublish")) {
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
    const cardActions = [];
    if (entry.editing_available) cardActions.push(el("button", {
      class: `button${entry.goal_readiness === "needs_upgrade" ? " upgrade-workflow" : " edit-workflow"}`,
      text: i18n.t(entry.goal_readiness === "needs_upgrade"
        ? "workflows.upgrade" : "workflows.editWorkflow"),
      onclick: () => navigate({
        view: "workflowEdit", workflowId: entry.workflow_id, runId: null,
      }),
    }));
    if ((entry.allowed_commands || []).some((item) => item.command === "run.start")) {
      cardActions.push(el("button", {
        class: "button", text: i18n.t("action.newGoal"),
        onclick: () => newRunDialog(entry.workflow_id),
      }));
    } else if (entry.goal_readiness === "needs_migration" && generateCommand) {
      cardActions.push(el("button", {
        class: "button", text: i18n.t("generate.action"),
        onclick: () => generateWorkflowDialog(generateCommand),
      }));
    }
    const card = el("article", {
      class: "workflow-card panel",
      "data-workflow-id": entry.workflow_id,
    }, [
      el("button", { class: "workflow-card-main" }, [
        el("span", { class: "workflow-visual", "aria-hidden": "true" }, visualNodes),
        el("span", { class: "eyebrow", text: entry.workflow_id.replace(/^workflow:/i, "") }),
        el("span", { class: "workflow-card-heading" }, [
          el("strong", { text: entry.name }),
          entry.goal_readiness !== "ready" ? el("span", {
            class: `pill ${entry.goal_readiness === "needs_upgrade" ? "waiting" : "failed"}`,
            text: i18n.t(`workflows.readiness.${entry.goal_readiness}`),
          }) : null,
        ]),
        entry.description ? el("span", { class: "muted", text: entry.description }) : null,
        entry.goal_readiness !== "ready" ? el("span", {
          class: "muted",
          // A definition that cannot be upgraded is normally answered by
          // generating a replacement. Where this deployment has no generating
          // Agent that answer does not exist, so the card says what is true
          // instead of pointing at a button nobody can press.
          text: i18n.t(
            entry.goal_readiness === "needs_migration" && !generateCommand
              ? "workflows.readiness.needs_migration.noAgent"
              : `workflows.readiness.${entry.goal_readiness}.description`,
          ),
        }) : null,
        el("span", { class: "workflow-meta", text: i18n.t("workflows.summary", {
          nodes: i18n.number(entry.summary.node_count), inputs: i18n.number(entry.inputs.length),
        }) }),
        // Which version is current, and whether anyone has run it: the two
        // facts that tell two similarly named workflows apart.
        el("span", { class: "workflow-card-facts", text: entry.last_run_at
          ? i18n.t("workflows.lastRun", { when: i18n.dateTime(entry.last_run_at) })
          : i18n.t("workflows.neverRun") }),
      ]),
      cardActions.length
        ? el("div", { class: "workflow-card-actions" }, cardActions) : null,
    ]);
    card.querySelector(".workflow-card-main").addEventListener("click", () => navigate({
      view: "workflow", workflowId: entry.workflow_id, runId: null,
    }));
    cards.append(card);
  }
  if (!entries.length) {
    cards.append(el("div", { class: "empty panel", text: i18n.t("workflows.empty") }));
  }
  root.append(cards);
}

/* Registered Agent handlers: identity and durable-attempt facts from the
 * catalog. The Runtime collects no heartbeats, so "registered" is the only
 * status honestly on offer. */
const AGENT_HUES = [
  "hue-blue", "hue-purple", "hue-emerald", "hue-indigo",
  "hue-amber", "hue-cyan", "hue-rose", "hue-pink",
];
/* A handler family shares one hue: hash the name up to the first dash, so
 * hermes-* all land on the same color and colors survive new registrations.
 * A few handlers get a hand-picked hue that reads as their identity. */
const AGENT_HUE_OVERRIDE = { claude: "hue-amber" };
function agentHue(shortName) {
  if (AGENT_HUE_OVERRIDE[shortName]) return AGENT_HUE_OVERRIDE[shortName];
  const base = shortName.split("-")[0];
  let hash = 5381;
  for (const char of base) hash = (hash * 33 + char.codePointAt(0)) >>> 0;
  return AGENT_HUES[hash % AGENT_HUES.length];
}

async function renderAgents(root) {
  const catalog = (await api.handlerCatalog()).data;
  const agents = catalog.handlers.filter((handler) => handler.name.startsWith("agent."));
  root.append(el("header", { class: "view-intro" }, [
    el("div", {}, [
      el("h2", { text: i18n.t("agents.handlers") }),
      el("p", { class: "muted", text: i18n.t("agents.subtitle") }),
    ]),
    el("div", { class: "agents-online" }, [
      el("span", { class: "agents-online-dot", "aria-hidden": "true" }),
      el("span", { text: i18n.t("agents.online", { count: i18n.number(agents.length) }) }),
    ]),
  ]));
  root.append(el("div", { class: "agents-grid" }, agents.length
    ? agents.map((handler) => {
      const shortName = handler.name.replace(/^agent\./, "");
      return el("article", { class: "agent-card" }, [
        el("div", { class: "agent-head" }, [
          el("span", {
            class: `agent-avatar ${agentHue(shortName)}`,
            "aria-hidden": "true", text: shortName.slice(0, 2).toUpperCase(),
          }),
          el("div", { class: "agent-id" }, [
            el("h3", { class: "agent-name", text: shortName, title: handler.name }),
            el("div", { class: "mono agent-version", text: handler.version }),
          ]),
        ]),
        el("div", { class: "agent-stat" }, [
          el("span", { class: "agent-stat-label", text: i18n.t("agents.runCountLabel") }),
          el("span", {
            class: "agent-stat-pill",
            text: i18n.t("agents.runCount", {
              count: i18n.number(handler.attempt_count ?? 0),
            }),
          }),
        ]),
        handler.failed_count > 0 ? el("div", { class: "agent-stat" }, [
          el("span", { class: "agent-stat-label", text: i18n.t("agents.failedLabel") }),
          el("span", {
            class: "agent-stat-pill err",
            text: i18n.t("agents.failedTimes", {
              count: i18n.number(handler.failed_count),
            }),
          }),
        ]) : null,
      ]);
    })
    : [el("div", { class: "muted", text: i18n.t("agents.empty") })]));
}

/* The detail reads as a centred modal over the catalog, not a page under it.
 *
 * `#/workflows/{id}` stays the address, so the modal is opened by the route
 * and dismissing it navigates back — Escape, the scrim and the Close button
 * all end in the same place. The catalog behind it stays rendered (dimmed),
 * so a dismissal is one gesture instead of one fetch. The root lives one
 * layer under the sticky topbar, leaving the bar live and un-dimmed. */
async function openWorkflowModal(workflowId) {
  const panel = el("div", {
    class: "workflow-modal-panel", role: "dialog", "aria-modal": "true",
    "aria-label": i18n.t("workflows.detail"), tabindex: "-1",
  }, [dataState(el, i18n, "loading")]);
  const scrim = el("div", { class: "workflow-modal-scrim" });
  const stage = el("div", { class: "workflow-modal" }, [panel]);
  const root = el("div", { class: "workflow-modal-root" }, [scrim, stage]);

  // The scrim starts below the topbar; measure it once and on resize so a
  // wrapping bar never leaves a gap or eats the modal's top edge.
  const topbar = document.querySelector(".topbar");
  const syncTopbar = () => root.style.setProperty(
    "--topbar-h", `${topbar ? topbar.offsetHeight : 0}px`,
  );

  let settled = false;
  const teardown = () => {
    document.removeEventListener("keydown", onKeydown);
    window.removeEventListener("resize", syncTopbar);
    document.body.style.overflow = previousOverflow;
  };
  const dismiss = () => {
    if (settled) return;
    settled = true;
    teardown();
    root.classList.add("closing");
    const finish = () => {
      if (root.isConnected) root.remove();
      // Only walk back if this Workflow is still what the address names: a
      // modal closed by a navigation must not undo that navigation.
      if (route.view === "workflow" && route.workflowId === workflowId) {
        navigate({ view: "workflows", runId: null });
      }
    };
    panel.addEventListener("transitionend", (event) => {
      if (event.target === panel) finish();
    }, { once: true });
    setTimeout(finish, 300);
  };
  const onKeydown = (event) => {
    if (event.key !== "Escape") return;
    // A stacked <dialog> (the modify flow) owns Escape while it is open; the
    // modal only answers once the top layer is empty again.
    if (document.querySelector("dialog[open]")) return;
    event.preventDefault();
    dismiss();
  };

  const previousOverflow = document.body.style.overflow;
  document.body.style.overflow = "hidden";
  document.addEventListener("keydown", onKeydown);
  window.addEventListener("resize", syncTopbar);
  // Clicking the dimmed surround (not the panel) closes, same as the scrim.
  stage.addEventListener("click", (event) => {
    if (event.target === stage) dismiss();
  });
  syncTopbar();
  const previous = activeViewCleanup;
  activeViewCleanup = () => {
    if (!settled) {
      settled = true;
      teardown();
    }
    root.remove();
    if (previous) previous();
  };

  document.body.append(root);
  // One frame closed, then open: the transition needs a starting pose.
  void root.offsetWidth;
  root.classList.add("open");
  panel.focus();
  await renderWorkflowDetail(panel, workflowId, dismiss);
}

/** One published definition, at its own address.
 *
 * The catalog is for finding a workflow; the dialog is for reading one, so it
 * is reachable by link and survives a reload.
 */
async function renderWorkflowDetail(root, workflowId, dismiss = null) {
  const panel = el("section", { class: "workflow-detail" });
  // replaceChildren clears any loading placeholder; append would stack beside it.
  root.replaceChildren(panel);

  const draw = async () => {
    panel.replaceChildren(dataState(el, i18n, "loading"));
    try {
      const value = (await api.workflowDetail(workflowId)).data;
      const definition = value.definition;
      // replaceChildren stringifies a bare null argument into a "null" text
      // node, so the optional drift notice is filtered out of the child list.
      panel.replaceChildren(...[
        el("header", { class: "workflow-detail-head" }, [
          el("div", { class: "workflow-detail-headline" }, [
            el("div", { class: "eyebrow workflow-detail-id", text: value.workflow_id }),
            el("h1", { class: "workflow-detail-name", text: value.name }),
            value.description
              ? el("p", { class: "workflow-detail-desc", text: value.description })
              : null,
          ]),
          el("button", {
            class: "button workflow-detail-back", id: "backToWorkflows",
            text: i18n.t(dismiss ? "action.close" : "action.back"),
            onclick: () => dismiss
              ? dismiss()
              : navigate({ view: "workflows", runId: null }),
          }),
        ]),
        handlerDriftNotice(value, draw),
        // The drawing answers "what shape is this", the definition list
        // answers "what exactly is in it" — one tabbed surface for both.
        workflowDefinitionTabs(
          value.graph, definition, "workflows.definition",
        ),
      ].filter(Boolean));
    } catch (error) {
      panel.replaceChildren(dataState(el, i18n, "error", { onRetry: draw }));
      reportError(error);
    }
  };

  await draw();
}

async function renderWorkflowEdit(root, workflowId) {
  const page = el("section", { class: "panel workflow-edit-page" });
  root.replaceChildren(page);

  const draw = async () => {
    page.replaceChildren(dataState(el, i18n, "loading"));
    try {
      const value = (await api.workflowDetail(workflowId)).data;
      const modify = value.allowed_commands.find(
        (item) => item.command === "workflow.modify",
      );
      const editors = value.action_editors || {};
      if (!modify && !Object.keys(editors).length) {
        navigate({ view: "workflow", workflowId, runId: null });
        return;
      }
      const canvas = el("div", { class: "panel-body workflow-edit-canvas" });
      const editingVersion = el("span", {
        class: "workflow-editing-version",
        text: i18n.t("workflows.editingVersion", {
          version: i18n.number(value.latest_version),
        }),
      });
      const drawDefinition = (current) => {
        const currentEditors = current.action_editors || {};
        editingVersion.textContent = i18n.t("workflows.editingVersion", {
          version: i18n.number(current.latest_version),
        });
        canvas.replaceChildren(...[
          handlerDriftNotice(current, refreshPublished),
          workflowDefinitionTabs(
            current.graph, current.definition, "workflows.definition",
            currentEditors,
            (nodeId) => openActionEditorDialog(current, nodeId, refreshPublished),
          ),
        ].filter(Boolean));
      };
      const refreshPublished = async () => {
        const current = (await api.workflowDetail(workflowId)).data;
        drawDefinition(current);
      };
      page.replaceChildren(...[
        el("div", { class: "panel-head workflow-edit-head" }, [
          el("div", { class: "workflow-edit-heading" }, [
            el("h1", {
              class: "workflow-edit-title", text: i18n.t("workflows.editWorkflow"),
            }),
            el("div", { class: "eyebrow workflow-edit-id", text: value.workflow_id }),
          ]),
          el("div", { class: "actions" }, [
            el("span", {
              class: "workflow-edit-status",
            }, [
              el("span", { text: i18n.t("editor.state.awaitingPrompt") }),
              editingVersion,
            ]),
            el("button", {
              class: "button", id: "closeWorkflowEditor",
              text: i18n.t("action.close"),
              onclick: () => navigate({ view: "workflows", runId: null }),
            }),
          ]),
        ]),
        // The revise panel is the editing surface — lead with it, above the
        // diagram, so the primary "change this workflow" action comes first.
        modify ? workflowEditorPanel(modify, value, draw, refreshPublished) : null,
        canvas,
      ].filter(Boolean));
      drawDefinition(value);
    } catch (error) {
      page.replaceChildren(dataState(el, i18n, "error", { onRetry: draw }));
      reportError(error);
    }
  };

  await draw();
}

/* --------------------------------------------------------- workflow editor */

/** Agent-only Workflow Editor.
 *
 * The published source is a read-only fact. The only operation that can
 * replace it is the server-advertised revise command, whose output has already
 * passed the production compiler before this view receives it.
 */
async function renderWorkflowEditor(root, draftId) {
  let draft = (await api.workflowDraft(draftId)).data;
  let busy = false;
  let discardArmed = false;
  let instructionText = "";
  // The Agent chosen to write the revision, kept across redraws.
  let writerAgent = defaultGenerationAgent();
  let revisionDiagnostics = [];
  // The Agent call is a durable job, so the editor polls until it settles
  // rather than holding a request open. A reload re-enters here and picks the
  // same job back up.
  let pollTimer = null;

  const panel = el("section", { class: "panel workflow-editor-panel agent-workflow-editor" });
  const command = (name) =>
    (draft.allowed_commands || []).find((item) => item.command === name);

  const semanticDiffView = (candidate) => {
    const diff = semanticWorkflowDiff(candidate.previous_source, candidate.source);
    if (!diff) return null;
    const groups = [
      ["editor.diff.addedNodes", diff.addedNodes, "added"],
      ["editor.diff.removedNodes", diff.removedNodes, "removed"],
      ["editor.diff.changedNodes", diff.changedNodes.map((item) =>
        `${item.id} · ${item.fields.join(", ")}`), "changed"],
      ["editor.diff.addedEdges", diff.addedEdges, "added"],
      ["editor.diff.removedEdges", diff.removedEdges, "removed"],
      ["editor.diff.changedEdges", diff.changedEdges.map((item) =>
        `${item.id} · ${item.fields.join(", ")}`), "changed"],
      ["editor.diff.workflowFields", diff.workflowFields, "changed"],
    ].filter(([, items]) => items.length);
    return el("div", { class: "agent-editor-semantic-diff", "data-semantic-diff": "true" }, [
      el("div", { class: "actions" }, [
        el("div", { class: "panel-title", text: i18n.t("editor.diff.title") }),
        el("span", { class: "pill", text: i18n.t("editor.diff.changeCount", {
          count: i18n.number(diff.changeCount),
        }) }),
      ]),
      groups.length ? el("div", { class: "semantic-diff-grid" }, groups.map(([key, items, tone]) =>
        el("section", { class: `semantic-diff-group ${tone}` }, [
          el("div", { class: "eyebrow", text: i18n.t(key, { count: i18n.number(items.length) }) }),
          el("div", { class: "semantic-diff-items" }, items.map((item) =>
            el("span", { class: "mono", text: item }))),
        ]))) : el("p", { class: "muted", text: i18n.t("editor.diff.identical") }),
    ]);
  };

  const execute = async (name, payload, intent) => {
    const allowed = command(name);
    if (!allowed || busy) return null;
    busy = true;
    draw();
    try {
      const response = await api.execute(allowed, payload, intent);
      draft = response.data;
      if (name === "workflow.draft.revise") revisionDiagnostics = [];
      return response;
    } catch (error) {
      reportError(error);
      if (name === "workflow.draft.revise") {
        revisionDiagnostics = error.details?.diagnostics || [];
      }
      if (error instanceof ApiError && error.requiresRefresh) {
        draft = (await api.workflowDraft(draft.draft_id)).data;
      }
      return null;
    } finally {
      busy = false;
      draw();
    }
  };

  const draw = () => {
    const candidate = draft.pending_revision;
    const inFlight = Boolean(candidate?.in_flight);
    const lastFailure = (draft.revision_history || []).find(
      (item) => item.status === "failed" || item.status === "cancelled",
    );
    const previewSource = candidate?.source || draft.source;
    // Drawn by the same server layout the published workflow uses, so the
    // draft and the version it becomes are the same picture.
    const previewGraph = candidate ? candidate.graph : draft.graph;
    let definition = null;
    try { definition = JSON.parse(previewSource); } catch { /* read-only fallback below */ }
    const revise = command("workflow.draft.revise");
    const accept = command("workflow.draft.accept");
    const reject = command("workflow.draft.reject");
    const undo = command("workflow.draft.undo");
    const publish = command("workflow.draft.publish");
    const discard = command("workflow.draft.discard");
    const writerField = generationAgentField(
      "draftRevisionAgent", writerAgent, (value) => { writerAgent = value; },
    );
    const instruction = el("textarea", {
      id: "draftRevisionInstruction", maxlength: "4000", required: "required",
      placeholder: i18n.t("editor.agentPromptPlaceholder"),
      disabled: busy || inFlight || !revise ? "disabled" : null,
    });
    instruction.value = instructionText;
    instruction.addEventListener("input", () => { instructionText = instruction.value; });
    const findings = el("div", { class: "editor-diagnostics" });
    if (inFlight) {
      findings.append(el("div", { class: "banner info", id: "revisionProgress" }, [
        el("span", { text: i18n.t(
          candidate.status === "queued"
            ? "editor.revisionQueued" : "editor.revisionRunning",
        ) }),
      ]));
    } else if (lastFailure && !candidate) {
      findings.append(el("div", { class: "banner error", id: "revisionFailed" }, [
        el("div", { text: i18n.t(
          lastFailure.status === "cancelled"
            ? "editor.revisionCancelled" : "editor.revisionFailed",
        ) }),
        lastFailure.error_code
          ? el("div", { class: "mono", text: lastFailure.error_code })
          : null,
      ]));
    }
    if (candidate && !inFlight) {
      findings.append(el("div", {
        class: "banner info", text: i18n.t("editor.candidateValid"),
      }));
    } else if (draft.validation_status === "valid") {
      findings.append(el("div", {
        class: "banner info", text: i18n.t("editor.agentRevisionValid"),
      }));
    } else {
      findings.append(el("div", {
        class: "muted", text: i18n.t("editor.agentPromptRequired"),
      }));
    }
    for (const item of [...(draft.diagnostics || []), ...revisionDiagnostics]) {
      findings.append(el("div", { class: "error-item" }, [
        el("div", { class: "mono", text: `${item.code} ${item.json_path || ""}` }),
        el("div", { text: item.message }),
      ]));
    }

    const actions = el("div", { class: "actions", id: "draftControls" });
    if (revise) actions.append(el("button", {
      type: "button", class: "button primary", id: "draftRevise",
      disabled: busy ? "disabled" : null,
      text: i18n.t(busy ? "editor.agentRevising" : "editor.agentRevise"),
      onclick: async () => {
        const value = instruction.value.trim();
        if (!value) {
          instruction.setCustomValidity(i18n.t("editor.agentPromptRequired"));
          instruction.reportValidity();
          return;
        }
        instruction.setCustomValidity("");
        const response = await execute(
          "workflow.draft.revise",
          { instruction: value, ...(writerAgent ? { agent: writerAgent } : {}) },
          `workflow.draft.revise:${draft.draft_id}:${draft.revision}:${Date.now()}`,
        );
        if (response) {
          instructionText = "";
          draw();
        }
      },
    }));
    const cancelRevision = command("workflow.draft.cancel-revision");
    if (cancelRevision) actions.append(el("button", {
      type: "button", class: "button danger", id: "draftCancelRevision",
      disabled: busy ? "disabled" : null,
      text: i18n.t("editor.cancelRevision"),
      onclick: () => execute(
        "workflow.draft.cancel-revision",
        { revision_id: candidate.revision_id },
        `workflow.draft.cancel-revision:${candidate.revision_id}`,
      ),
    }));
    if (accept) actions.append(el("button", {
      type: "button", class: "button primary", id: "draftAccept",
      disabled: busy ? "disabled" : null, text: i18n.t("editor.acceptRevision"),
      onclick: () => execute(
        "workflow.draft.accept", {},
        `workflow.draft.accept:${candidate.revision_id}`,
      ),
    }));
    if (reject) actions.append(el("button", {
      type: "button", class: "button", id: "draftReject",
      disabled: busy ? "disabled" : null, text: i18n.t("editor.rejectRevision"),
      onclick: () => execute(
        "workflow.draft.reject", {},
        `workflow.draft.reject:${candidate.revision_id}`,
      ),
    }));
    if (undo) actions.append(el("button", {
      type: "button", class: "button", id: "draftUndo",
      disabled: busy ? "disabled" : null, text: i18n.t("editor.undoRevision"),
      onclick: () => execute(
        "workflow.draft.undo", {},
        `workflow.draft.undo:${draft.draft_id}:${draft.revision}`,
      ),
    }));
    if (publish) actions.append(el("button", {
      type: "button", class: "button primary", id: "draftPublish",
      disabled: busy ? "disabled" : null, text: i18n.t("editor.publish"),
      onclick: async () => {
        const response = await execute(
          "workflow.draft.publish", {}, `workflow.draft.publish:${draft.draft_id}`,
        );
        if (response) {
          announce(i18n.t("editor.published", { workflowId: draft.workflow_id }));
          navigate({ view: "workflows", runId: null });
        }
      },
    }));
    if (discard) actions.append(el("button", {
      type: "button", class: "button danger", id: "draftDiscard",
      disabled: busy ? "disabled" : null,
      text: i18n.t(discardArmed ? "editor.discardConfirm" : "editor.discard"),
      onclick: async () => {
        if (!discardArmed) { discardArmed = true; draw(); return; }
        const response = await execute(
          "workflow.draft.discard", {}, `workflow.draft.discard:${draft.draft_id}`,
        );
        if (response) {
          announce(i18n.t("editor.discarded"));
          navigate({ view: "workflows", runId: null });
        }
      },
    }));

    panel.replaceChildren(
      el("div", { class: "panel-head" }, [
        el("div", {}, [
          el("div", { class: "eyebrow", text: draft.workflow_id }),
          el("div", { class: "panel-title", text: i18n.t("editor.agentTitle") }),
        ]),
        el("span", {
          class: `pill ${draft.validation_status === "valid" ? "succeeded" : "waiting"}`,
          text: i18n.t(candidate ? "editor.state.reviewCandidate"
            : draft.validation_status === "valid"
              ? "editor.state.valid" : "editor.state.awaitingPrompt"),
        }),
        actions,
      ]),
      el("div", { class: "panel-body agent-editor-layout" }, [
        // Visible while the job runs so progress and cancel stay on screen;
        // hidden only once a candidate is waiting for accept or reject.
        el("section", {
          class: "agent-editor-prompt",
          hidden: candidate && !inFlight ? "hidden" : null,
        }, [
          el("div", { class: "panel-title", text: i18n.t("editor.agentPromptTitle") }),
          el("p", { class: "muted", text: i18n.t("editor.agentPromptHint") }),
          revise ? writerField : null,
          instruction,
          revise || candidate ? null : el("div", {
            class: "banner error", text: i18n.t("editor.agentUnavailable"),
          }),
        ]),
        // Only a settled candidate has a proposal to review; an in-flight job
        // has no source, hash or attempt count yet.
        candidate && !inFlight ? el("section", { class: "agent-editor-candidate" }, [
          el("div", { class: "panel-title", text: i18n.t("editor.reviewCandidate") }),
          el("p", { text: candidate.instruction }),
          el("div", { class: "muted mono", text: i18n.t("editor.candidateFacts", {
            attempts: i18n.number(candidate.attempts),
            hash: candidate.definition_hash.slice(0, 27),
          }) }),
          semanticDiffView(candidate),
          el("div", { class: "agent-editor-diff" }, [
            el("div", {}, [
              el("div", { class: "eyebrow", text: i18n.t("editor.beforeRevision") }),
              el("pre", { class: "artifact-preview", text: candidate.previous_source }),
            ]),
            el("div", {}, [
              el("div", { class: "eyebrow", text: i18n.t("editor.afterRevision") }),
              el("pre", { class: "artifact-preview", text: candidate.source }),
            ]),
          ]),
        ]) : null,
        el("section", { class: "agent-editor-preview" }, [
          el("div", { class: "panel-title", text: i18n.t(candidate
            ? "editor.candidatePreview" : "editor.agentPreview") }),
          workflowDefinitionTabs(previewGraph, definition, "editor.draftDefinition"),
          el("details", {}, [
            el("summary", { class: "muted", text: i18n.t("editor.sourceReadOnly") }),
            el("pre", { class: "artifact-preview", id: "draftSourcePreview", text: previewSource }),
          ]),
        ]),
        findings,
        draft.revision_history?.length ? el("section", { class: "agent-editor-history" }, [
          el("div", { class: "panel-title", text: i18n.t("editor.revisionHistory") }),
          el("div", { class: "definition-list" }, draft.revision_history.map((item) =>
            el("div", { class: "actions" }, [
              el("span", { class: `pill ${item.status}` , text: i18n.status(item.status) }),
              el("span", { text: item.instruction }),
              el("span", { class: "muted mono", text: i18n.dateTime(item.created_at) }),
            ]))),
        ]) : null,
        el("footer", { class: "editor-facts mono", text: i18n.t("editor.facts", {
          revision: i18n.number(draft.revision), updated: i18n.dateTime(draft.updated_at),
          hash: (draft.validated_definition_hash || draft.source_hash || "—").slice(0, 27),
        }) }),
      ]),
    );
    schedulePoll();
  };

  /* Re-read the draft while the Agent is working. The job lives in the
     database, so this is also what makes a reloaded page pick the same work
     back up instead of showing an idle editor. */
  const schedulePoll = () => {
    if (pollTimer) clearTimeout(pollTimer);
    pollTimer = null;
    if (!draft.pending_revision?.in_flight) return;
    pollTimer = setTimeout(async () => {
      if (!document.getElementById("draftRevisionInstruction")) return;
      try {
        draft = (await api.workflowDraft(draft.draft_id)).data;
        draw();
      } catch (error) {
        reportError(error);
      }
    }, 1500);
  };

  root.append(panel);
  draw();
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
async function generateWorkflowDialog(generateCommand, initialJob = null) {
  const dialog = el("dialog", { "aria-label": i18n.t("generate.title") });
  const form = el("form", { method: "dialog" });
  dialog.append(form);
  let job = initialJob;
  let promptText = initialJob?.prompt || "";
  let timer = null;

  const draw = () => {
    const body = [el("h2", { text: i18n.t("generate.title") })];
    const actions = el("div", { class: "actions" });
    if (!job) {
      const instruction = el("textarea", {
        id: "generateInstruction", required: "required", maxlength: "4000",
        placeholder: i18n.t("generate.instructionPh"), text: promptText,
      });
      body.push(
        el("div", { class: "field" }, [
          el("label", { for: "generateInstruction", text: i18n.t("generate.instruction") }),
          instruction,
          el("small", { class: "muted", text: i18n.t("generate.hint") }),
        ]),
      );
      actions.append(
        el("button", {
          class: "button", value: "cancel", formnovalidate: "formnovalidate",
          text: i18n.t("action.cancel"),
        }),
        el("button", {
        type: "button", class: "button primary", id: "generateSubmit",
        text: i18n.t("generate.action"),
        onclick: async () => {
          if (!instruction.value.trim()) return;
          promptText = instruction.value.trim();
          try {
            const response = await api.execute(
              // Step names are read here, so they are written in this locale
              // rather than in whatever language the prompt happened to use.
              generateCommand, { prompt: promptText, display_language: i18n.locale },
              `workflow.generate:${Date.now()}`,
            );
            job = response.data;
            draw();
            watch();
          } catch (error) {
            reportError(error);
          }
        },
      }));
    } else {
      body.push(
        el("div", { class: `authoring-job-state ${job.status}` }, [
          el("span", { class: "live-dot", "aria-hidden": "true" }),
          el("strong", { text: i18n.t(`authoring.job.${job.status}`) }),
        ]),
        el("p", { class: "muted", text: promptText }),
        job.error ? el("div", { class: "banner error", text: job.error.message }) : null,
      );
      const cancel = (job.allowed_commands || []).find(
        (item) => item.command === "workflow.authoring.cancel",
      );
      if (cancel) {
        actions.append(el("button", {
          type: "button", class: "button", text: i18n.t("action.cancel"),
          onclick: async () => {
            try {
              job = (await api.execute(
                cancel, {}, `workflow.authoring.cancel:${job.job_id}`,
              )).data;
              draw();
            } catch (error) {
              reportError(error);
            }
          },
        }));
      } else {
        actions.append(el("button", {
          type: "button", class: "button", text: job.status === "done"
            ? i18n.t("action.open") : i18n.t("action.close"),
          onclick: () => {
            dialog.close();
            if (job.status === "done" && job.result?.workflow_id) {
              navigate({
                view: "workflow", workflowId: job.result.workflow_id,
              });
            }
          },
        }));
        if (["failed", "cancelled"].includes(job.status)) {
          actions.append(el("button", {
            type: "button", class: "button primary",
            text: i18n.t("action.retry"),
            onclick: () => {
              job = null;
              draw();
            },
          }));
        }
      }
    }
    // replaceChildren has no opinion about null the way el() does: it would
    // render an absent banner as the literal word "null".
    form.replaceChildren(...body.filter(Boolean), actions);
  };

  const watch = async () => {
    if (!job || !["queued", "running"].includes(job.status)) return;
    clearTimeout(timer);
    timer = setTimeout(async () => {
      try {
        job = (await api.get(job.href)).data;
        draw();
        watch();
      } catch (error) {
        reportError(error);
      }
    }, 800);
  };

  dialog.addEventListener("close", () => {
    clearTimeout(timer);
    dialog.remove();
  }, { once: true });
  document.body.append(dialog);
  draw();
  dialog.showModal();
}


/* What the modification changed, as steps rather than counts.
 *
 * The server has already decided which entries are trustworthy and whether
 * they came from the Agent or from the structural diff; this only draws them.
 */
function changeSummaryView(summary) {
  if (!summary) return [];
  const rows = (summary.entries || []).map((entry) => el("li", {
    class: `change-entry ${entry.kind}`,
  }, [
    el("span", { class: "change-mark", "aria-hidden": "true", text:
      entry.kind === "added" ? "+" : entry.kind === "removed" ? "−" : "~" }),
    el("span", { class: "change-copy" }, [
      el("strong", { text: entry.label }),
      entry.detail ? el("span", { class: "muted", text: entry.detail }) : null,
    ]),
  ]));
  const edges = (summary.edges_added || 0) + (summary.edges_removed || 0);
  return [
    rows.length ? el("ul", { class: "change-summary" }, rows) : null,
    !rows.length && !edges
      ? el("p", { class: "muted", text: i18n.t("simplified.workflow.noChanges") })
      : null,
    edges ? el("p", { class: "muted", text: i18n.t("simplified.workflow.edgeChanges", {
      added: i18n.number(summary.edges_added || 0),
      removed: i18n.number(summary.edges_removed || 0),
    }) }) : null,
  ].filter(Boolean);
}

/* The revision editor is the lower band of the dedicated edit page. It stays
 * beside the graph it revises instead of becoming a second floating dialog. */
function workflowEditorPanel(modifyCommand, workflow, onDone, onPublished = null) {
  const upgrading = workflow.goal_readiness === "needs_upgrade";
  const title = i18n.t(upgrading ? "workflows.upgrade" : "editor.edit");
  const section = el("section", { class: "workflow-editor", "aria-label": title });
  const wrap = el("div", { class: "workflow-editor-wrap" }, [section]);
  let job = workflow.active_job || null;
  // An upgrade opens with the instruction already written: the author asked to
  // upgrade, not to compose the sentence that means "upgrade".
  let promptText = upgrading ? i18n.t("workflows.upgrade.prompt") : "";
  let writerAgent = defaultGenerationAgent();
  // Regenerate is the bigger hammer — it may redesign the whole flow — so it
  // stays out of sight until a plain modify has actually failed.
  let regenerateOffered = false;
  let timer = null;
  let collapsed = false;
  let publishedRefreshDone = false;
  // The Agent CLI's console. Created once and kept across redraws so the tail
  // an operator is reading is never thrown away when the job's state changes.
  const consoleLog = el("pre", {
    class: "console-log workflow-authoring-console", role: "log", tabindex: "0",
  });
  const consoleView = el("section", { class: "workflow-authoring-output" }, [
    el("h3", { class: "field-label", text: i18n.t("editor.agentConsole") }),
    consoleLog,
  ]);
  let outputAfter = 0;
  let outputStalled = false;

  /** Append whatever the CLI has printed since the last chunk we showed. */
  const pumpOutput = async () => {
    if (outputStalled || !job || !job.output_href) return;
    try {
      for (let page = 0; page < 20; page += 1) {
        const data = (await api.get(
          `${job.output_href}?after=${outputAfter}`
        )).data;
        for (const chunk of data.chunks) {
          consoleLog.append(el("span", {
            class: `console-chunk ${chunk.stream}`, text: chunk.text,
          }));
          outputAfter = chunk.chunk_id;
        }
        if (!data.has_more) break;
      }
      if (consoleLog.childElementCount) consoleLog.scrollTop = consoleLog.scrollHeight;
    } catch (error) {
      // Reading a console needs the sensitive scope. Where the operator does
      // not have it the panel simply carries no console — the job itself is
      // still reported in full. Transport and server failures are temporary;
      // the next job-status tick retries them.
      outputStalled = error instanceof ApiError
        && (error.status === 401 || error.status === 403);
    }
  };

  const collapse = (refresh) => {
    if (collapsed) return;
    collapsed = true;
    clearTimeout(timer);
    wrap.classList.remove("open");
    const finish = () => {
      if (wrap.isConnected) wrap.remove();
      if (refresh) onDone();
    };
    wrap.addEventListener("transitionend", (event) => {
      if (event.target === wrap) finish();
    }, { once: true });
    setTimeout(finish, 340);
  };

  const submit = async (mode) => {
    try {
      job = (await api.execute(
        modifyCommand, {
          prompt: promptText,
          mode,
          display_language: i18n.locale,
          ...(writerAgent ? { agent: writerAgent } : {}),
        },
        `workflow.${mode}:${workflow.workflow_id}:${Date.now()}`,
      )).data;
      draw();
      watch();
    } catch (error) {
      reportError(error);
    }
  };

  const draw = () => {
    // Same layout language as the draft editor page: a state pill in the
    // head, the instruction in an agent-editor-prompt section beneath it.
    const statePill = el("span", {
      class: `pill ${!job || ["queued", "running"].includes(job.status)
        ? "waiting" : job.status === "done" ? "succeeded" : "failed"}`,
      text: i18n.t(!job ? "editor.state.awaitingPrompt"
        : `authoring.job.${job.status}`),
    });
    const body = [];
    const actions = el("div", { class: "actions" });
    if (!job) {
      const writerField = generationAgentField(
        "workflowModifyAgent", writerAgent, (value) => { writerAgent = value; },
      );
      const prompt = el("textarea", {
        class: "mono workflow-modify-input", required: "required", maxlength: "4000",
        placeholder: i18n.t("editor.agentPromptPlaceholder"),
        text: promptText,
      });
      // Focus lands in the prompt so a prefilled upgrade is one click from
      // running and still editable in place.
      setTimeout(() => prompt.focus(), 0);
      // An "authored by" section over a titled prompt section whose textarea
      // carries a syntax hint.
      const promptSection = el("section", { class: "workflow-modify-prompt" }, [
        el("h3", { class: "field-label", text: i18n.t("editor.agentPromptTitle") }),
        el("p", { class: "muted", text: i18n.t("editor.agentPromptHint") }),
        el("div", { class: "workflow-modify-input-wrap" }, [
          prompt,
          el("span", {
            class: "workflow-modify-syntax", text: i18n.t("editor.promptSyntaxHint"),
          }),
        ]),
      ]);
      body.push(writerField, promptSection);
      if (regenerateOffered) body.push(el("p", {
        class: "muted", text: i18n.t("simplified.workflow.regenerate.hint"),
      }));
      actions.append(...[
        el("button", {
          type: "button", class: "button primary", text: i18n.t("editor.agentRevise"),
          onclick: () => {
            if (!prompt.value.trim()) return;
            promptText = prompt.value.trim();
            submit("modify");
          },
        }),
        regenerateOffered ? el("button", {
          type: "button", class: "button", id: "regenerateWorkflow",
          text: i18n.t("simplified.workflow.regenerate"),
          onclick: () => {
            if (!prompt.value.trim()) return;
            promptText = prompt.value.trim();
            submit("regenerate");
          },
        }) : null,
      ].filter(Boolean));
    } else {
      body.push(
        ...changeSummaryView(job.result?.change_summary),
        job.error ? el("div", { class: "banner error", text: job.error.message }) : null,
      );
      const cancel = (job.allowed_commands || []).find(
        (item) => item.command === "workflow.authoring.cancel",
      );
      if (cancel) actions.append(el("button", {
        type: "button", class: "button", text: i18n.t("action.cancel"),
        onclick: async () => {
          job = (await api.execute(
            cancel, {}, `workflow.authoring.cancel:${job.job_id}`,
          )).data;
          draw();
        },
      }));
      else {
        // A failed modify is where regenerate earns its place: the author has
        // evidence that keeping the current structure did not work.
        if (job.status === "failed") actions.append(el("button", {
          type: "button", class: "button", id: "retryWorkflowModify",
          text: i18n.t("simplified.workflow.tryAgain"),
          onclick: () => {
            regenerateOffered = true;
            job = null;
            draw();
          },
        }));
        actions.append(el("button", {
          type: "button", class: "button primary", text: i18n.t("action.close"),
          onclick: () => collapse(job.status === "done"),
        }));
      }
    }
    // replaceChildren has no opinion about null the way el() does: it would
    // render an absent banner as the literal word "null".
    // The page header carries the title and the awaiting-state badge (per the
    // design prototype), so the form leads straight with its own sections. A
    // live job state pill only appears once a job is running or settled.
    section.replaceChildren(...[
      job ? el("div", { class: "workflow-editor-head" }, [statePill]) : null,
      ...body.filter(Boolean),
      // What the Agent printed, once there is a job and it has said something.
      job && consoleLog.childElementCount ? consoleView : null,
      actions,
    ].filter(Boolean));
  };
  const watch = () => {
    if (!job || !["queued", "running"].includes(job.status)) return;
    timer = setTimeout(async () => {
      // Leaving the edit page detaches the editor: stop spending requests on it.
      if (!section.isConnected) return;
      try {
        job = (await api.get(job.href)).data;
        // Pull the console before redrawing, so a tick that ends the job still
        // paints the last thing the Agent said.
        await pumpOutput();
        draw();
        if (job.status === "done" && !publishedRefreshDone && onPublished) {
          publishedRefreshDone = true;
          await onPublished();
        }
        watch();
      } catch (error) {
        reportError(error);
      }
    }, 800);
  };

  draw();
  watch();
  // A job that settled before this panel opened is never polled, so its
  // console is fetched once here rather than never.
  if (job) pumpOutput().then(() => { if (section.isConnected) draw(); });
  // Two frames: the grid-row transition needs the zero-height pose committed
  // before the growing class lands, or the opening never animates.
  requestAnimationFrame(() => requestAnimationFrame(() => wrap.classList.add("open")));
  return wrap;
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
  /* No wizard: the home composer is the one place a Goal begins. The catalog
     pre-flight stays — the composer must know which Workflow is runnable and
     the author must hear "no catalog" before typing a goal nobody can run. */
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
  const preselected = catalog.data.workflows.find(
    (item) => item.workflow_id === preselectedWorkflowId,
  );
  simplifiedComposerState.runId = null;
  simplifiedComposerState.workflowId = preselected?.goal_readiness === "ready"
    ? preselected.workflow_id : "";
  simplifiedComposerState.goal = "";
  focusSimplifiedGoalOnRender = true;
  navigate({ view: "home", runId: null });
}

/* ------------------------------------------------------------------- shell */

function navigate(next) {
  if (activeViewLeaveGuard && !activeViewLeaveGuard()) return;
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
  if (activeViewCleanup) {
    activeViewCleanup();
    activeViewCleanup = null;
  }
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
    const fresh = el("div", { class: "content" });
    if (route.view === "home") await renderSimplifiedWorkspace(fresh);
    else if (route.view === "goal") await renderHistory(fresh, route.runId);
    else if (route.view === "goals") await renderHistory(fresh);
    else if (route.view === "workflows") await renderWorkflows(fresh);
    else if (route.view === "workflow") {
      // The catalog stays rendered (dimmed) behind the centred detail modal.
      await renderWorkflows(fresh);
      await openWorkflowModal(route.workflowId);
    }
    else if (route.view === "workflowEdit") {
      await renderWorkflowEdit(fresh, route.workflowId);
    }
    else if (route.view === "run") await renderSimplifiedWorkspace(fresh, route.runId);
    else if (route.view === "agents") await renderAgents(fresh);
    // The workspace is the one place runs are browsed.
    else await renderSimplifiedWorkspace(fresh);
    root.replaceChildren(...fresh.childNodes);
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
}


async function setLocale(locale) {
  i18n = await I18n.load(locale);
  i18n.persist();
  document.getElementById("localeSelect").value = locale;
  syncCustomSelect(document.getElementById("localeSelect"));
  applyStaticText();
  syncRefreshIntervalSelect(document.getElementById("refreshInterval"));
  await render();
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
  const refreshInterval = document.getElementById("refreshInterval");
  syncRefreshIntervalSelect(refreshInterval);
  refreshInterval.addEventListener("change", () => saveRefreshInterval(refreshInterval));
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
  } catch (error) {
    reportError(error);
  }
  // Permissions have been resolved, so every view now renders the command set
  // this actor really has. Views read it; automation waits on it.
  document.documentElement.dataset.shell = "ready";

  document.getElementById("refresh").addEventListener("click", () => render());
  window.addEventListener("orbit:refresh", () => render());
  scheduleLivePolling();
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
  await refreshRuntimeCard();
  await render();
}

boot();
