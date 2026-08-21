import { el, svgEl } from "../components/dom.js";

/** What the embedded viewer and this page say to each other.
 *
 * Duplicated from `ui/editor/src/catalog-graph.mjs` rather than imported: that
 * module is bundled, this one is served as source, and there is no build step
 * between them. `test_ui_assets` holds the two copies to the same strings.
 */
const VIEWER = {
  ready: "orbit-viewer-ready",
  graph: "orbit-viewer-graph",
  nodeClick: "orbit-viewer-node-click",
  theme: "orbit-viewer-theme",
};

/** The published graph, drawn by the editor's canvas in a frame.
 *
 * Module level rather than inside the factory because two pages draw it: the
 * workflow detail, and a run over the top of the same definition. A second
 * embedding written beside this one would be a second picture of one thing,
 * which is the drift this frame exists to remove.
 *
 * The graph is posted in rather than fetched by the frame: it is already
 * here, an unpublished draft has no id to fetch by, and one request means
 * one answer that both surfaces are describing. `statuses` rides along when
 * the page is drawing a run; without it the frame draws the definition alone.
 */
export function embeddedGraph(graph, {
  viewerUrl, i18n, actionEditors = {}, onEditAction = null, statuses = null,
  bindings = null,
}) {
  const editable = Object.keys(actionEditors || {});
  const frame = el("iframe", {
    class: "workflow-graph-frame",
    src: viewerUrl(),
    title: i18n.t("workflows.graph"),
    loading: "lazy",
  });
  const send = () => frame.contentWindow?.postMessage(
    { type: VIEWER.graph, graph, editable, statuses, bindings },
    window.location.origin,
  );
  // The frame is a document of its own, so it sees the operator's *system*
  // preference and not the theme they picked in this UI — a dark page with a
  // white canvas in the middle of it. Told rather than guessed, and told
  // again whenever it changes, because the choice outlives one drawing.
  const sendTheme = () => frame.contentWindow?.postMessage(
    { type: VIEWER.theme, theme: document.documentElement.dataset.theme || null },
    window.location.origin,
  );
  const watchTheme = new MutationObserver(sendTheme);
  watchTheme.observe(document.documentElement, {
    attributes: true, attributeFilter: ["data-theme"],
  });
  const onMessage = (event) => {
    if (event.origin !== window.location.origin) return;
    if (event.source !== frame.contentWindow) return;
    if (event.data?.type === VIEWER.ready) { sendTheme(); send(); }
    if (event.data?.type === VIEWER.nodeClick && onEditAction) {
      onEditAction(event.data.nodeId);
    }
  };
  window.addEventListener("message", onMessage);
  // The pane is replaced whenever the tab changes or the workflow is
  // redrawn, and a listener per drawing that outlived its frame would
  // answer for a window that is gone.
  const stop = new MutationObserver(() => {
    if (!frame.isConnected) {
      window.removeEventListener("message", onMessage);
      watchTheme.disconnect();
      stop.disconnect();
    }
  });
  stop.observe(document.body, { childList: true, subtree: true });
  const pane = el("div", { class: "workflow-graph-pane" }, [frame]);
  // A run's node statuses change while it is watched. Re-posting them costs
  // one message; rebuilding the pane would reload the bundle and redraw the
  // graph, which is what watching a run used to do every couple of seconds.
  pane.updateStatuses = (next) => { statuses = next; send(); };
  return pane;
}

export function createWorkflowDefinitionViews({
  api, i18n, reportError, viewerUrl = () => null,
}) {
  function readableNodeName(node) {
    const label = node?.label;
    if (typeof label === "string" && label.trim()) return label.trim();
    return i18n.t("simplified.workflow.step");
  }

  /* One colour per node kind, read by both the glyph and the kind pill.
   *
   * Deleted with the second graph renderer and left referenced by the list
   * that still uses it, so every attempt to draw a definition threw a
   * ReferenceError and the pane rendered nothing at all. The values are the
   * ones the stylesheet still carries classes for. */
  const KIND_COLOR = {
    action: "blue", human: "amber", decision: "purple", terminal: "muted",
  };

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

  /** The Handler a step will really use, when that is not the one it names.
   *
   * The catalog says which node moves and where to; a step nobody rebinds is
   * absent from the map and reads exactly as it was published.
   */
  function effective(bindings, nodeId) {
    return (bindings || {})[nodeId]?.name || null;
  }

  function definitionList(
    definition, graph = null, actionEditors = {}, onEditAction = null,
    bindings = null,
  ) {
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
                class: "defn-handler",
                text: node.handler.name.replace(/^agent\./, ""),
                // Marked rather than replaced: this list is a reading of what
                // was published, and rewriting the name in it would leave no
                // way to see what the author chose.
                "data-rebound": effective(bindings, node.id) ? "true" : null,
              }) : null,
              effective(bindings, node.id) ? el("span", {
                class: "defn-handler defn-handler-bound",
                text: effective(bindings, node.id).replace(/^agent\./, ""),
                title: i18n.t("workflows.agentBinding.title"),
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
          label: title.value.trim(), prompt: prompt.value.trim(),
          ...(selected ? { handler: selected } : {}),
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
    actionEditors = {}, onEditAction = null, bindings = null,
  ) {
    // No drawing without a graph, and none without the canvas that draws it.
    // The editor bundle is a build artifact and a source checkout may not
    // carry it; the list says everything the picture does, so that is the
    // whole degradation.
    if (!graph || !viewerUrl()) return definitionList(
      definition, graph, actionEditors, onEditAction, bindings,
    );
    const panes = {
      graph: () => embeddedGraph(graph, {
        viewerUrl, i18n, actionEditors, onEditAction, bindings,
      }),
      definition: () => definitionList(
        definition, graph, actionEditors, onEditAction, bindings,
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


  /** The workflow graph, drawn by the editor's canvas in a frame.
   *
   * There used to be a second renderer here — five hundred lines of hand-laid
   * SVG — drawing the same definitions the editor draws with xyflow. Two
   * pictures of one thing drift: they disagreed about what a decision node
   * looks like and where a back edge goes, and a fix to one was a fix to one.
   *
   * The graph is posted in rather than fetched by the frame: it is already
   * here, an unpublished draft has no id to fetch by, and one request means
   * one answer that both surfaces are describing.
   */
  return {
    readableNodeName, definitionList,
    openActionEditorDialog, openWorkflowDeleteDialog, stepPrompt,
    workflowDefinitionTabs, deleteGlyph,
  };
}
