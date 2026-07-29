import { el, svgEl } from "../components/dom.js";

const GRAPH_BOX = {
  width: 168, height: 60, gapX: 64, gapY: 40, pad: 16, minHeight: 260,
};

export function createWorkflowDefinitionViews({ api, i18n, reportError }) {
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
    const hasSelfLoop = graph.edges.some((edge) => edge.back_edge && edge.from === edge.to);
    // A self-loop returns through the right side of its card and needs half a
    // column gap beyond the normal graph bounds when it sits in the last column.
    const canvasWidth = pad * 2 + columns * width + (columns - 1) * gapX
      + (hasSelfLoop ? gapX / 2 : 0);
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
      const selfLoop = edge.back_edge && edge.from === edge.to;
      const path = selfLoop
        ? `M${from.x + from.width / 2} ${from.y + from.height}`
          + ` V${from.y + from.height + gapY / 2}`
          + ` H${from.x + from.width + gapX / 2}`
          + ` V${from.y + from.height / 2} H${from.x + from.width}`
        : edge.back_edge
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
  function deleteGlyph() {
    return svgEl("svg", {
      width: "24", height: "24", viewBox: "0 0 24 24", fill: "currentColor",
    }, [
      svgEl("path", {
        d: "M6 19c0 1.1.9 2 2 2h8c1.1 0 2-.9 2-2V7H6v12zm2-10h8v10H8V9zm7.5-5-1-1h-5l-1 1H5v2h14V4z",
      }),
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

  function openWorkflowDeleteDialog(workflow, allowed, onDeleted) {
    const titleId = `deleteWorkflowTitle-${workflow.workflow_id.replace(/[^a-z0-9]/gi, "-")}`;
    const problem = el("div", { class: "banner error", hidden: "hidden" });
    const confirm = el("button", {
      type: "submit", class: "button danger workflow-delete-confirm",
      text: i18n.t("workflows.deleteAction"),
    });
    const cancel = el("button", {
      type: "button", class: "button", text: i18n.t("action.cancel"),
    });
    const dialog = el("dialog", {
      class: "workflow-delete-dialog", "aria-labelledby": titleId,
    });
    cancel.addEventListener("click", () => dialog.close());
    const form = el("form", {}, [
      el("h2", { id: titleId, text: i18n.t("workflows.deleteTitle") }),
      el("p", {
        class: "workflow-delete-warning",
        text: i18n.t("workflows.deleteConfirm", { name: workflow.name }),
      }),
      el("div", { class: "mono workflow-delete-id", text: workflow.workflow_id }),
      problem,
      el("div", { class: "actions" }, [cancel, confirm]),
    ]);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      confirm.disabled = true;
      cancel.disabled = true;
      problem.hidden = true;
      try {
        await api.execute(allowed, {}, `workflow.delete:${workflow.workflow_id}`);
        dialog.close();
        await onDeleted();
      } catch (error) {
        confirm.disabled = false;
        cancel.disabled = false;
        problem.textContent = error instanceof ApiError
          ? i18n.t(error.messageKey, { message: error.message })
          : i18n.t("error.generic");
        problem.hidden = false;
        reportError(error);
      }
    });
    dialog.append(form);
    dialog.addEventListener("close", () => dialog.remove(), { once: true });
    document.body.append(dialog);
    dialog.showModal();
    cancel.focus();
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


  return {
    readableNodeName, workflowGraphView, definitionList,
    openActionEditorDialog, openWorkflowDeleteDialog, stepPrompt,
    workflowDefinitionTabs, deleteGlyph,
  };
}
