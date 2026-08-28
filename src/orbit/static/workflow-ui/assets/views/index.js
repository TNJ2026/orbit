import { ApiError } from "../api.js";
import { dataState } from "../components/data-state.js";
import { el, svgEl } from "../components/dom.js";
import { syncCustomSelect } from "../components/custom-select.js";
import { semanticWorkflowDiff } from "../workflow-diff.js";
import { resumeActions } from "../run-resume.js";
import { workflowGenerationProgress } from "../workflow/generation-progress.js";
import { embeddedGraph } from "../workflow/definition-views.js";
import { askConfirm, askText } from "../components/dialog.js";

export function createViews(context) {
  const { api, render, navigate, announce, reportError, commandButtons,
    promptAndExecute, pill, statusDot, defaultGenerationAgent,
    generationAgentField, workflowViews, runtimeState, isRendering,
    TERMINAL_RUN_STATUSES, TERMINAL_LANGGRAPH_STATUSES,
    DATA_TEXT_LIMIT } = context;
  let i18n = context.i18n;
  let shellFacts = context.shellFacts;
  let mayStartRun = context.mayStartRun;
  let liveCursor = null;
  let refreshTimer = null;
  let activeViewCleanup = null;
  let activeViewLeaveGuard = null;
  const goalFilters = { q: "", status: "" };
  // The shell's current route, threaded through update(): a modal dismisses
  // back only when the address still names it, and knowing that is the
  // shell's fact, not something the views module may read off a global.
  let currentRoute = null;
  // A goal modal opened by a click carries no address of its own, so a
  // background render that tears down the page would lose it for good. The
  // id of the run being read keeps the modal alive across re-renders the way
  // the route keeps a route-opened one alive; dismissing clears it.
  let goalModalRunId = null;
  const simplifiedComposerState = {
    runId: null, workflowId: "", templateId: "", goal: "",
  };
  // One glyph per step state, so a row reads as far along without its
  // colour. Up here with the module's own state because both the first
  // render and the live patch that follows it reach for the same table.
  const STEP_MARKS = {
    succeeded: "✓", failed: "✕", unknown: "?", running: "●", waiting: "◔",
    answered: "✓", cancelled: "✕", not_reached: "○",
  };

  // The run this page is watching, while it is still running. Cleared with
  // the view, so a tick after navigating away redraws rather than patching a
  // page that is gone.
  let liveRun = null;
  // Whether the view on screen owns something a background redraw would
  // destroy — an instruction half typed, or a finished revision whose account
  // nobody has said they have read yet. The comment on the live tick has
  // claimed for a long time that an editor is never torn down by a projection
  // change; nothing implemented it, so publishing a revision tore down the
  // very account of it that had just been put on screen.
  let viewHolds = null;
  let focusSimplifiedGoalOnRender = false;
  let simplifiedWorkflowGenerationPending = false;

  function installViewCleanup(cleanup) {
    const previous = activeViewCleanup;
    activeViewCleanup = () => { cleanup(); if (previous) previous(); };
  }

  function runName(run) {
    // `display_name` used to lead this list. No payload has ever carried one
    // for a run — it was the deleted projection's word — so the goal a person
    // typed is the name, and the id is what is left when there was none.
    return run.goal || run.run_id;
  }




  function workflowStartCommand(entry) {
    return (entry?.allowed_commands || [])
      .find((item) => item.command === "langgraph_run.start") || null;
  }

  /** Whether this composer can start `entry` from a goal.
   *
   * Two independent questions, and only one of them used to be asked.
   * `langgraph_compatibility` says the definition compiles under the engine;
   * `goal_readiness` says a single goal can be bound to its inputs at all,
   * and is `needs_upgrade`/`needs_migration` for reasons that have nothing to
   * do with node kinds — no goal binding, a required input with no default,
   * an ambiguous result.
   *
   * A workflow can compile perfectly and have nowhere to put the goal. Start
   * was enabled for those, `bindGoalInput` returned the input unchanged
   * because there was no binding to write into, and the run was refused by
   * the server for an input the person had in fact typed.
   */
  function workflowRunnable(entry) {
    if (entry?.goal_readiness !== "ready") return false;
    return entry?.langgraph_compatibility?.compatible === true;
  }

  /** Why the engine is withholding start, in its own words.
   *
   * `goal_readiness` has always explained itself on the card. The engine's
   * half never did: the answer carries a `reason` and a `detail` written for
   * a person, and no view read either — so a workflow the engine refused
   * showed a card that said `ready`, a binding that said `current`, no
   * banner, and no Start button, with the explanation sitting unread in the
   * payload that drew it.
   *
   * The reason is a word this UI knows and translates. The detail is the
   * Runtime's own sentence — which Handler, which port — and is passed
   * through the way a run's `error` already is, because inventing a
   * translation for it would mean dropping the specifics that make it
   * actionable.
   */
  function engineRefusal(entry) {
    const answer = entry?.langgraph_compatibility;
    if (!answer || answer.compatible === true) return null;
    const known = [
      "agent_rebind_failed", "service_not_configured",
      "unsupported_workflow", "workflow_not_found",
    ].includes(answer.reason);
    return {
      summary: i18n.t(`workflows.blocked.${known ? answer.reason : "unknown"}`),
      detail: answer.detail || "",
    };
  }

  /** Which Handler each step will really use, by node, or null for none.
   *
   * Read off the catalog's own answer rather than derived here: the server
   * decides what gets rebound and already reports it per node, and a second
   * opinion computed in the browser could disagree with the run that follows.
   */
  function effectiveBindings(entry) {
    const bound = (entry?.handler_bindings || [])
      .filter((binding) => binding.status === "rebound" && binding.rebound_to);
    if (!bound.length) return null;
    return Object.fromEntries(bound.map((binding) => [
      binding.node_id,
      { name: binding.rebound_to, version: binding.available_version },
    ]));
  }

  function engineRefusalNotice(entry) {
    const refusal = engineRefusal(entry);
    if (!refusal) return null;
    return el("div", { class: "banner warn workflow-blocked" }, [
      el("div", { class: "eyebrow", text: i18n.t("workflows.blocked.title") }),
      el("p", { text: refusal.summary }),
      refusal.detail
        ? el("p", { class: "muted mono", text: refusal.detail })
        : null,
    ].filter(Boolean));
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
        (item) => workflowRunnable(item) && workflowStartCommand(item),
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
      // The server refuses beyond this; the box says so first, so a long
      // paste is stopped where it is typed rather than after a round trip.
      maxlength: "4000",
      disabled: locked ? "disabled" : null,
      placeholder: i18n.t("simplified.start.placeholder"),
      text: simplifiedComposerState.goal,
      oninput: (event) => { simplifiedComposerState.goal = event.target.value; },
      onkeydown: (event) => {
        if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
          event.preventDefault();
          event.currentTarget.form?.requestSubmit();
        }
      },
    });
    const problem = el("div", {
      class: "banner error simplified-composer-problem", hidden: "hidden",
    });
    const chosen = selectedEntry();
    const allowed = workflowRunnable(chosen) ? workflowStartCommand(chosen) : null;
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
          if (!fresh || !workflowRunnable(fresh)) {
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
            // The run, not its outcome. Waiting for the whole thing before
            // being told the id is what kept this page from ever showing a
            // goal in progress: by the time it could navigate, there was
            // nothing left to watch.
            wait: false,
          }, `run.start:${crypto.randomUUID ? crypto.randomUUID() : Date.now()}`);
          const startedRun = started.data.run || started.data;
          simplifiedComposerState.runId = startedRun.run_id;
          announce(command.label, "success");
          navigate({ view: "run", runId: startedRun.run_id });
        } catch (error) {
          if (error instanceof ApiError && error.code === "active_goal_exists") {
            const active = error.details.active_goal;
            announce(i18n.t("newRun.active.exists", {
              goal: active ? runName(active) : "",
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
              || !workflowRunnable(current) || !workflowStartCommand(current);
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
          el("span", {
            class: "simplified-goal-shortcut muted",
            text: i18n.t("simplified.start.shortcut"),
          }),
        ]),
        el("div", { class: "actions simplified-composer-actions" }, [start]),
        chosen && !allowed ? (
          engineRefusalNotice(chosen) || el("div", {
            class: "banner warn", text: i18n.t("simplified.workflow.unavailable"),
          })
        ) : null,
        problem,
      ]),
    ]);
    root.append(el("section", {
      class: "simplified-goal-page", "aria-labelledby": "simplifiedGoalTitle",
    }, [
      el("header", { class: "simplified-goal-intro" }, [
        el("h1", {
          id: "simplifiedGoalTitle", text: i18n.t("simplified.start.title"),
        }),
        el("p", {
          class: "muted", text: i18n.t("simplified.start.description"),
        }),
      ]),
      form,
    ]));
    if (focusSimplifiedGoalOnRender && !locked) {
      focusSimplifiedGoalOnRender = false;
      setTimeout(() => goal.focus(), 0);
    }
  }

  async function renderSimplifiedWorkspace(root, selectedRunId = null) {
    // The published catalog, in both products. Single-agent mode used to
    // choose from built-in templates instead, which meant the workflow its own
    // Agent had just generated was not among the things it could start.
    const [runsResponse, catalogResponse] = await Promise.all([
      api.langGraphRuns({ limit: 25 }),
      api.workflowCatalog(),
    ]);
    const runs = runsResponse.data.runs || [];
    const active = runs.find((item) => ["running", "waiting", "interrupted"].includes(item.status));
    const runId = selectedRunId || active?.run_id || null;
    const summary = runId ? (await api.langGraphRun(runId)).data : null;
    // A LangGraph run, so a LangGraph status. The composer is withheld once
    // the selected run is over — it would otherwise render locked, because a
    // summary is present, and read as a run still in flight.
    const historicalDetail = Boolean(
      selectedRunId && summary
      && TERMINAL_LANGGRAPH_STATUSES.has(summary.status),
    );
    if (!historicalDetail) {
      renderSimplifiedComposer(root, catalogResponse.data.workflows, summary);
    }
    if (runId && summary) await renderLangGraphRun(root, summary);
  }

  /* The run's own commands, as buttons. The workspace hero and the History
   * detail draw them in different frames, but a run that can be resumed or
   * cancelled must offer the same acts in both. */
  function runActionButtons(run) {
    const commands = run.allowed_commands || [];
    const resume = commands.find((item) => item.command === "langgraph_run.resume");
    const cancel = commands.find((item) => item.command === "langgraph_run.cancel");
    const actions = [];
    if (resume) {
      // The label is this UI's, not the command's. A Runtime names its
      // commands for every client at once — "Resume LangGraph workflow" says
      // which engine and which surface — and a person looking at their own
      // run needs the verb, in their own language.
      const actions_ = resumeActions(run, resume, i18n.t("run.resume"));
      for (const action of actions_) actions.push(el("button", {
        class: "button primary", text: action.label,
        onclick: async () => {
          const raw = await askText(el, i18n, {
            title: i18n.t("run.resume.title"),
            label: action.nodeId
              ? i18n.t("run.resume.labelNamed", { node: action.nodeId })
              : i18n.t("run.resume.label"),
            value: JSON.stringify(action.payload.value, null, 2),
            confirmLabel: i18n.t("run.resume.submit"),
          });
          if (raw === null) return;
          try {
            await api.execute(action.command, {
              ...action.payload, value: JSON.parse(raw),
            });
            await render();
          } catch (error) { reportError(error); }
        },
      }));
    }
    if (cancel) actions.push(el("button", {
      class: "button danger", text: i18n.t("run.cancel"),
      onclick: async () => {
        // `confirmation` is the contract's word for *that* confirmation is
        // required, not the sentence to ask. Shown as the prompt, it asked
        // the operator to confirm the word "explicit".
        const stop = await askConfirm(el, i18n, {
          title: i18n.t("run.cancel.title"),
          message: i18n.t("run.cancel.confirm"),
          confirmLabel: i18n.t("run.cancel"),
          danger: true,
        });
        if (!stop) return;
        try {
          await api.execute(cancel, {}, `cancel:${run.run_id}`);
          await render();
        } catch (error) { reportError(error); }
      },
    }));
    return actions;
  }

  async function renderLangGraphRun(root, run, { branches = true } = {}) {
    const actions = runActionButtons(run);
    root.append(el("section", { class: "panel simplified-run-hero" }, [
      // Identity and fate, not the goal. A goal is frequently a paragraph and
      // it pushed everything a watcher is here for — which run this is, how
      // it is going, how to stop it — below the fold. It is read in full on
      // the goal page, which is the page for reading it.
      el("div", { class: "simplified-run-identity" }, [
        el("div", { class: "simplified-run-ids" }, [
          el("p", { class: "mono muted", text: run.workflow_id }),
          el("p", { class: "mono muted", text: run.run_id }),
          // Who actually did the work, when that was not who the definition
          // names. Recorded on the run rather than read from the current
          // binding: the answer for a run that happened last week is the
          // Agent that ran it, not the one that would run it now.
          run.agent_binding ? el("p", {
            class: "muted run-agent-binding",
            text: i18n.t("run.agentBinding", {
              agent: run.agent_binding.replace(/^agent\./, "").replace(/@.*$/, ""),
            }),
          }) : null,
        ].filter(Boolean)),
        // Beside the status rather than under the card: stopping a run is
        // what a watcher comes here to do, and it was below everything the
        // run had produced so far.
        //
        // The way out sits in the same place once there is nothing left to
        // stop. A finished run is a page a reader is done with, and the only
        // exits were the browser's back button and the nav — neither of which
        // says that starting the next goal is what happens next.
        el("div", { class: "simplified-run-state" }, [
          pill(run.status),
          ...actions,
          TERMINAL_LANGGRAPH_STATUSES.has(run.status) ? el("button", {
            class: "button", text: i18n.t("run.close"),
            onclick: () => navigate({ view: "home", runId: null }),
          }) : null,
        ].filter(Boolean)),
      ]),
      run.interrupts?.length ? el("pre", {
        class: "code-block", text: JSON.stringify(run.interrupts, null, 2),
      }) : null,
      run.result !== null && run.result !== undefined ? el("pre", {
        class: "code-block", text: JSON.stringify(run.result, null, 2),
      }) : null,
      run.error ? el("div", { class: "banner error", text: run.error }) : null,
    ]));
    const live = !TERMINAL_LANGGRAPH_STATUSES.has(run.status);
    const drawn = await renderRunSteps(root, run.run_id, { branches, live });
    // Watching a run used to mean redrawing the page around it every couple
    // of seconds: the fold somebody had opened closed, the graph frame
    // reloaded its bundle and redrew, and the scroll position moved. What
    // actually changes on a tick is which steps have got where, so that is
    // what a tick changes.
    if (live && drawn) {
      liveRun = {
        runId: run.run_id,
        status: run.status,
        interrupts: run.interrupts?.length || 0,
        patch: async () => {
          const steps = (await api.runSteps(run.run_id)).data.steps || [];
          for (const step of steps) {
            const handle = drawn.rows.get(step.node_id);
            if (!handle) continue;
            handle.row.className = `step-row ${step.status}`;
            handle.mark.textContent = STEP_MARKS[step.status] || "○";
            handle.status.className = `pill ${step.status}`;
            handle.status.textContent = i18n.t(
              `simplified.steps.status.${step.status}`,
            );
          }
          drawn.canvas?.updateStatuses?.(Object.fromEntries(
            steps.map((step) => [step.node_id, step.status]),
          ));
        },
      };
      installViewCleanup(() => { liveRun = null; });
    }
    await appendRunArtifacts(root, run.run_id);
  }

  /* The same picture as the definition page, with the run drawn on it.
   *
   * A second view rather than a replacement: a list says how far along and
   * how many times, and a graph says which branches there were and which one
   * was taken. Neither is the other's summary, so the graph is a card of its
   * own beside the steps, drawn as soon as the page is — no fold between the
   * reader and half the answer.
   *
   * Drawn from the run rather than from its workflow. The catalog serves the
   * latest version only, so once a workflow was republished this drew a graph
   * the run never executed, next to a step list derived from the definition
   * it really used. A template run had nothing to draw at all.
   */
  function runCanvas(runId, statuses) {
    const url = shellFacts?.capabilities?.workflow_viewer?.available
      ? (shellFacts.capabilities.workflow_viewer.url || "/viewer/")
      : null;
    if (!url) return null;
    const body = el("div", { class: "run-canvas-body" });
    const section = el("section", { class: "panel run-canvas" }, [
      el("div", { class: "panel-head" }, [
        el("div", { class: "panel-title", text: i18n.t("workflows.graph") }),
      ]),
      body,
    ]);
    // The frame fetches on its own, like an Artifact thumbnail: the card is
    // on the page immediately and a slow viewer never delays the steps.
    (async () => {
      try {
        const response = await api.runGraph(runId);
        const pane = embeddedGraph(response.data.graph, {
          viewerUrl: () => url, i18n, statuses,
        });
        body.append(pane);
        section.updateStatuses = pane.updateStatuses;
      } catch (error) {
        body.append(el("p", { class: "muted", text: error?.messageKey
          ? i18n.t(error.messageKey, { message: error.message })
          : i18n.t("simplified.steps.unavailable") }));
      }
    })();
    return section;
  }

  /* Where the run got to, one row per step of its definition.
   *
   * Drawn from the definition rather than from what has happened, so the
   * steps still to come are on the page from the first render: a list that
   * grew as the run progressed would give no sense of how much is left.
   *
   * Returns handles on what it drew rather than nothing, so a live tick can
   * move the rows it already put on the page. Re-rendering the panel each
   * time the cursor moved would collapse a console somebody had opened
   * underneath it and reload the graph frame beside it.
   */
  async function renderRunSteps(root, runId, { branches = true, live = false } = {}) {
    const panel = el("section", { class: "panel simplified-steps" }, [
      el("div", { class: "panel-head" }, [
        el("div", { class: "panel-title", text: i18n.t("simplified.steps") }),
      ]),
    ]);
    root.append(panel);
    let steps;
    try {
      steps = (await api.runSteps(runId)).data.steps || [];
    } catch (error) {
      panel.append(el("p", { class: "muted", text: error?.messageKey
        ? i18n.t(error.messageKey, { message: error.message })
        : i18n.t("simplified.steps.unavailable") }));
      return;
    }
    if (!steps.length) {
      panel.append(el("p", { class: "muted", text: i18n.t("simplified.steps.empty") }));
      return;
    }
    const canvas = runCanvas(runId, Object.fromEntries(
      steps.map((step) => [step.node_id, step.status]),
    ));
    const rows = new Map();
    panel.append(el("ol", { class: "step-list" }, steps.map((step) => {
      const mark = el("span", { class: "step-mark", "aria-hidden": "true",
        text: STEP_MARKS[step.status] || "○" });
      const status = el("span", { class: `pill ${step.status}`,
        text: i18n.t(`simplified.steps.status.${step.status}`) });
      const row = el(
      "li", { class: `step-row ${step.status}` }, [
        mark,
        el("div", { class: "step-copy" }, [
          el("strong", { class: "step-label", text: step.label }),
          el("span", { class: "step-detail muted", text: [
            step.handler ? step.handler.name : step.kind,
            // A loop is one row that says how many times, not a row per
            // visit that would read as several different steps.
            step.runs > 1
              ? i18n.t("simplified.steps.repeated", { count: i18n.number(step.runs) })
              : null,
          ].filter(Boolean).join(" · ") }),
        ]),
        // Its own word list rather than the run's: a step is "working" or
        // "not started", and a run is never either.
        status,
        step.status !== "not_reached"
          // A step that printed nothing has no log to view, and a fold that
          // opens on "nothing yet" is a promise the row could have kept to
          // itself. The goal detail already withheld it; this row did not.
          ? runConsole(runId, {
            live, nodeId: step.node_id, prompt: step.prompt,
            hideWhenEmpty: true,
          }) : null,
      ]);
      rows.set(step.node_id, { row, mark, status });
      return row;
    })));
    if (canvas) root.append(canvas);
    if (branches) await renderRunBranches(panel, runId, steps);
    return { rows, canvas };
  }

  /* Which way the run went at each fork.
   *
   * Only forks: a node with one outgoing edge decided nothing, and listing it
   * would bury the nodes that did. Collapsed, and absent entirely when the
   * definition has no fork — most runs have nothing to explain, and the ones
   * that went somewhere surprising are the reason this is here at all.
   */
  async function renderRunBranches(panel, runId, steps) {
    let edges;
    try {
      edges = (await api.runEdges(runId)).data.edges || [];
    } catch (error) {
      // Quietly: the steps above are the answer to the question that was
      // asked, and this is a footnote to them.
      return;
    }
    const labels = Object.fromEntries(
      steps.map((step) => [step.node_id, step.label || step.node_id]),
    );
    const forks = new Map();
    for (const edge of edges) {
      if (!forks.has(edge.source_node)) forks.set(edge.source_node, []);
      forks.get(edge.source_node).push(edge);
    }
    const branching = [...forks.entries()].filter(
      ([, items]) => items.length > 1,
    );
    if (!branching.length) return;
    panel.append(el("details", { class: "run-branches" }, [
      el("summary", { text: i18n.t("simplified.branches") }),
      el("div", { class: "run-branches-body" }, branching.map(
        ([sourceId, items]) => el("div", { class: "run-branch-group" }, [
          el("strong", { class: "run-branch-source",
            text: labels[sourceId] || sourceId }),
          el("ul", { class: "run-branch-list" }, items.map((edge) => el(
            "li", { class: `run-branch ${edge.status}` }, [
              el("span", { class: "run-branch-target",
                text: labels[edge.target_node] || edge.target_node }),
              el("span", { class: `pill ${edge.status}`, text: i18n.t(
                `simplified.branches.status.${edge.status}`,
              ) }),
              edge.default
                ? el("span", { class: "muted",
                  text: i18n.t("simplified.branches.default") })
                : null,
            ].filter(Boolean),
          ))),
        ]),
      )),
    ]));
  }

  /* The Handler console, followed rather than paged.
   *
   * Loaded only when opened: an Agent's output is the largest thing on the
   * page and most visits do not want it. While the run is live it keeps
   * asking; once it is over it reads to the end and stops.
   */
  function runConsole(runId, {
    live, nodeId, prompt = null, hideWhenEmpty = false, showState = true,
  }) {
    const log = el("div", {
      class: "console-log simplified-step-output-log", role: "log",
      tabindex: "0", hidden: true,
    });
    // History reads runs that are over: a line saying the stream ended
    // states nothing the status pill above has not already said, so the
    // drawer withholds it while the run page keeps it.
    const state = showState
      ? el("span", { class: "muted", text: i18n.t("run.console.empty") }) : null;
    const setState = (text) => { if (state) state.textContent = text; };
    // The fold says what is behind it. A step that printed nothing but was
    // given an instruction still has something to read, and offering it as
    // "view the run log" promises a log that does not exist.
    const summary = el("summary", {
      text: i18n.t(prompt ? "simplified.execution.prompt"
        : "simplified.execution.output"),
    });
    const details = el("details", { class: "simplified-step-output" }, [
      summary,
      el("div", { class: "simplified-step-output-body" }, [
        // What this step was asked, above what it said back. A log without
        // the instruction that produced it is half a transcript: the reader
        // opened this to find out what happened, and what was asked is the
        // other half of that.
        workflowViews().stepPrompt({ prompt }, "simplified-step-prompt"),
        state, log,
      ].filter(Boolean)),
    ]);
    // A step that said nothing and was asked nothing offers nothing to read:
    // the fold stays out of the way until output exists, probing while the
    // run is still live. An instruction is already something to read, so a
    // step carrying one is never hidden.
    if (hideWhenEmpty && !prompt) details.hidden = true;
    let after = 0;
    let timer = null;
    let loading = false;
    let stopped = false;
    // Whether output has been seen, which is what the probe is asking. It
    // used to ask `details.hidden` — the same question while the fold was
    // hidden precisely until output arrived, and the wrong one once a step
    // with an instruction started out visible: the probe returned at once
    // and the summary went on offering a prompt over a step that had a log.
    let seenOutput = false;

    /* Each stream is a titled card of its own, so stdout and stderr read as
     * sections rather than one undifferentiated scroll. */
    let lastStream = null;
    let blockBody = null;
    const draw = (chunks) => {
      for (const chunk of chunks) {
        if (chunk.stream !== lastStream) {
          lastStream = chunk.stream;
          blockBody = el("pre", { class: "console-stream-body" });
          log.append(el("div", { class: `console-stream-block ${chunk.stream}` }, [
            el("div", {
              class: "console-stream-title",
              text: i18n.t(chunk.stream === "stderr"
                ? "run.console.stderr" : "run.console.stdout"),
            }),
            blockBody,
          ]));
        }
        blockBody.append(el("span", {
          class: `console-chunk ${chunk.stream}`, text: chunk.text,
        }));
      }
      if (log.childElementCount) log.hidden = false;
    };

    const poll = async () => {
      if (stopped || loading || !details.open) return;
      loading = true;
      if (!log.childElementCount) {
        setState(i18n.t("simplified.execution.output.loading"));
      }
      try {
        const response = await api.runOutput(runId, after, 200, nodeId);
        after = response.data.after;
        draw(response.data.chunks);
        setState(log.childElementCount
          ? i18n.t(live ? "run.console.following" : "run.console.finished")
          : i18n.t("run.console.empty"));
        if (response.data.has_more) timer = setTimeout(poll, 0);
        else if (live) timer = setTimeout(poll, 2000);
      } catch (error) {
        stopped = true;
        setState(error instanceof ApiError && error.status === 403
          ? i18n.t("run.console.forbidden")
          : i18n.t("run.console.unavailable"));
      } finally {
        loading = false;
      }
    };

    /* While the fold is hidden nobody can open it, and the only question is
     * whether the step said anything at all: probe until it does or the run
     * dies, then hand the cadence back to the open-driven poll. A probe that
     * cannot read the output keeps the fold hidden rather than offering a
     * banner nobody asked for.
     */
    const probe = async () => {
      if (stopped || loading || seenOutput) return;
      loading = true;
      try {
        // One chunk: this is asking *whether* the step said anything, and
        // the answer is the same for one as for two hundred. Every executed
        // step on the page asks it — that is what withholding an empty fold
        // costs — so it must not be the whole log each time. What it draws
        // is kept, and opening the fold carries on from `after`.
        const response = await api.runOutput(runId, after, 1, nodeId);
        after = response.data.after;
        draw(response.data.chunks);
        if (log.childElementCount) {
          seenOutput = true;
          details.hidden = false;
          summary.textContent = i18n.t("simplified.execution.output");
          setState(i18n.t(
            live ? "run.console.following" : "run.console.finished",
          ));
        } else if (live) timer = setTimeout(probe, 2000);
      } catch (error) {
        stopped = true;
      } finally {
        loading = false;
      }
    };
    if (hideWhenEmpty || prompt) probe();

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

  /* What the run produced, and a way to open it.
   *
   * A failure here is reported in place rather than thrown: an Artifact store
   * that cannot be read must not blank out the run the person came to see.
   */
  async function appendRunArtifacts(root, runId) {
    let artifacts = [];
    try {
      artifacts = (await api.artifacts({ runId, limit: 25 })).data.artifacts || [];
    } catch (error) {
      root.append(el("section", { class: "panel" }, [
        el("div", { class: "panel-head" }, [
          el("div", { class: "panel-title", text: i18n.t("simplified.artifacts") }),
        ]),
        el("p", { class: "muted", text: error?.messageKey
          ? i18n.t(error.messageKey, { message: error.message })
          : String(error?.message || error) }),
      ]));
      return;
    }
    if (!artifacts.length) return;
    root.append(el("section", { class: "panel simplified-artifacts" }, [
      el("div", { class: "panel-head" }, [
        el("div", { class: "panel-title", text: i18n.t("simplified.artifacts") }),
      ]),
      el("div", { class: "artifact-grid" }, artifacts.map((item) => artifactCard(item))),
    ]));
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
    // The id a person needs to find this run again — in a log, in the search
    // box above the list, or in a question to somebody else. Left off only
    // when the run had no goal, because then the title already is the id.
    const identity = run.goal ? run.run_id : "";
    return el("button", {
      class: "history-goal-row",
      // The detail opens where the list is: a modal, no address change.
      // `#/goals/{id}` still addresses a run for people who paste one.
      onclick: () => openGoalModal(run.run_id),
    }, [
      el("span", { class: "history-goal-copy" }, [
        el("strong", { class: "history-goal-title", text: runName(run) }),
        el("span", { class: "history-goal-meta muted" }, [
          el("span", { text: metadata }),
          identity ? el("span", {
            class: "history-goal-id", text: identity,
          }) : null,
        ]),
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

  async function renderHistory(root) {
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
    const requestedStatus = goalFilters.status === "succeeded"
      ? "completed" : goalFilters.status;
    const [response, catalogResponse] = await Promise.all([
      api.langGraphRuns({
        limit: 25, status: requestedStatus, q: goalFilters.q,
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
          const next = await api.langGraphRuns({
            cursor: nextCursor, limit: 25, status: requestedStatus,
            q: goalFilters.q,
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

  async function refreshRuntimeCard() {
    const dot = document.getElementById("runtimeDot");
    const card = document.getElementById("runtimeCard");
    let health = null;
    try {
      health = await api.health();
    } catch {
      health = null;
    }
    const ready = Boolean(health && health.ok && health.status === "ready");
    dot.classList.toggle("degraded", !ready);
    card.setAttribute(
      "aria-label",
      i18n.t(ready ? "shell.runtime.healthy" : "shell.runtime.degraded"),
    );
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
  function isImage(item) {
    return item.image_previewable
      ?? String(item.content_type || "").startsWith("image/");
  }

  function artifactThumb(item) {
    const badge = documentIcon(item.content_type);
    if (!isImage(item)) return badge;
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
    // The port that produced it is the fallback name: a document with no
    // filename of its own must not be given one the file does not contain.
    const name = item.filename || item.port_id;
    return el("article", { class: `artifact-card panel list-option-card${selected ? " selected" : ""}` }, [
      el("button", {
        class: "artifact-card-main",
        "aria-current": selected ? "true" : null,
        onclick: () => openArtifactDialog(item.artifact_id),
      }, [
        el("span", { class: `artifact-top${isImage(item) ? " artifact-top-image" : ""}` }, [
          artifactThumb(item),
          el("span", { class: "artifact-size", text: i18n.t("artifacts.size", {
            size: i18n.number(item.size_bytes),
          }) }),
        ]),
        el("strong", { class: "artifact-name", text: name }),
        // What produced it, which is the step. The card is always read inside
        // the run it belongs to, so repeating the run here said nothing — and
        // said it under the word "Goal", against an id.
        item.goal ? el("span", { class: "artifact-origin" }, [
          el("span", { class: "artifact-origin-label muted", text: i18n.t("artifacts.goal") }),
          // One line, clipped by CSS; the full title stays reachable on hover.
          el("span", {
            class: "artifact-origin-value", text: item.goal, title: item.goal,
          }),
        ]) : null,
        item.node_id ? el("span", { class: "artifact-origin" }, [
          el("span", { class: "artifact-origin-label muted", text: i18n.t("artifacts.node") }),
          el("span", {
            class: "artifact-origin-value mono",
            title: item.node_id, text: item.node_id,
          }),
        ]) : null,
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
    if (isImage(item)) {
      const holder = el("div", { class: "artifact-content" });
      const load = () => {
        const image = el("img", {
          // Not lazy: the image is what was asked for, and a deferred load leaves
          // an empty box where the Artifact should be.
          class: "artifact-image", decoding: "async",
          src: api.artifactContentUrl(item.artifact_id),
          alt: item.filename || item.port_id,
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
    // The engine's projection does not pre-judge previewability, so text is
    // attempted and its own failure stays inside this box.
    const textual = item.previewable ?? String(item.content_type || "").startsWith("text/");
    if (!textual) {
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
            el("div", { class: "panel-title", text: item.filename || item.port_id }),
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

  /* The watched run's own progress, without redrawing the page around it.
   *
   * False when the page has to be redrawn instead: the run settled, it began
   * waiting on somebody, or it could not be read at all. The caller falls
   * back, so a wrong answer here costs a redraw rather than a stale page.
   */
  async function patchLiveRun() {
    if (!liveRun) return false;
    let run;
    try {
      run = (await api.langGraphRun(liveRun.runId)).data;
    } catch (error) {
      return false;
    }
    if (
      run.status !== liveRun.status
      || (run.interrupts?.length || 0) !== liveRun.interrupts
    ) return false;
    await liveRun.patch();
    return true;
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
      if (runtimeState.stopped) return;
      try {
        // Inside the try, and the reschedule inside a finally: this guard once
        // read an identifier that was never in scope, and because it threw
        // *before* the chain was extended, one ReferenceError silently stopped
        // every background refresh in the shell until the page was reloaded.
        // Nothing evaluated here may be able to end the chain.
        if (!document.hidden && !isRendering() && !document.querySelector("dialog[open]")) {
          const live = (await api.live(liveCursor)).data;
          liveCursor = live.cursor;
          failures = 0;
          // An Editor owns unsaved local text. Background projection changes
          // must never tear down that view; explicit Draft commands redraw it.
          // A change of fate changes the page's shape — the composer
          // unlocks, the commands change, a result appears — so that one is
          // still a redraw. It happens once, at the end, rather than on
          // every tick of the run it ends.
          if (live.changed && !viewHolds?.()) {
            const patched = liveRun ? await patchLiveRun() : false;
            if (!patched) await render();
          }
        }
      } catch (error) {
        failures += 1;
        // Programming errors stay loud; only transport failures back off
        // quietly. Neither is allowed to be the last tick.
        if (!(error instanceof ApiError)) console.error(error);
        if (failures === 1 || !(error instanceof ApiError)) reportError(error);
      } finally {
        refreshTimer = setTimeout(tick, delaySeconds() * 1000);
      }
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
  /** Say which Agent will actually run this workflow's Agent steps.
   *
   * A step whose Agent is not on this machine is carried to one that is,
   * and the definition on this very page goes on naming the other one,
   * node by node. Without this the page shows a workflow bound to an Agent
   * nobody has and says nothing about the substitution that makes it
   * startable.
   *
   * Driven by what the server says it will do, never by the mode switch in
   * the top bar: that switch is a local presentation override, and a notice
   * that followed it would promise a rebinding this Runtime is not doing. */
  function agentBindingNotice(value) {
    const bound = value.langgraph_compatibility?.agent_binding;
    if (!bound) return null;
    const steps = (value.handler_bindings || []).filter(
      (binding) => binding.status === "rebound",
    );
    if (!steps.length) return null;
    // Named from the steps themselves, not from the engine's one-line summary
    // of them. A substitution is decided per step, so a graph can land on two
    // Agents — and reading the first name out of `"a@1, b@2"` reported one of
    // them as the answer for all of it.
    const agents = [...new Set(
      steps.map((binding) => binding.rebound_to).filter(Boolean),
    )].map((name) => name.replace(/^agent\./, ""));
    return el("div", { class: "banner info workflow-agent-binding" }, [
      el("div", { class: "eyebrow", text: i18n.t("workflows.agentBinding.title") }),
      el("p", {
        // One step is a sentence about *it*, not about a count of one: the
        // shared string read "1 steps name an Agent", which is the kind of
        // wrong that makes a reader distrust the rest of the notice.
        text: i18n.t(
          steps.length === 1
            ? "workflows.agentBinding.description.one"
            : "workflows.agentBinding.description",
          { agent: i18n.list(agents), count: i18n.number(steps.length) },
        ),
      }),
    ]);
  }

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
    const page = el("div", { class: "workflows-page" });
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
      // Several writers may be available; the author can choose one and the
      // Runtime's default is selected initially.
      let writerAgent = defaultGenerationAgent();
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
      }, [
        el("span", { class: "generate-spark", "aria-hidden": "true", text: "✦" }),
        el("span", { text: activeGeneration
          ? i18n.t(`authoring.job.${activeGeneration.status}`)
          : i18n.t("generate.action") }),
      ]);
      const form = el("form", {
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
              {
                prompt: instruction.value.trim(),
                display_language: i18n.locale,
                ...(writerAgent ? { agent: writerAgent } : {}),
              },
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
        el("div", { class: "simplified-workflow-generator-agent" }, [
          generationAgentField(
            "workflowGenerateAgent", writerAgent,
            (value) => { writerAgent = value; },
          ),
        ]),
        el("div", { class: "field" }, [
          el("label", {
            class: "sr-only", for: "generateInstruction",
            text: i18n.t("generate.instruction"),
          }),
          instruction,
        ]),
        el("div", { class: "actions simplified-workflow-generator-actions" }, [submit]),
        problem,
      ]);
      page.append(el("header", {
        class: "view-intro simplified-workflow-generator-copy",
      }, [
        el("div", {}, [
          el("h2", { text: i18n.t("generate.title") }),
          el("p", { class: "muted", text: i18n.t("generate.hint") }),
        ]),
      ]));
      page.append(el("section", { class: "panel simplified-workflow-generator" }, [
        el("div", { class: "simplified-workflow-generator-inner" }, [
        activeGeneration
          ? workflowGenerationProgress(activeGeneration, render, {
              api, i18n, reportError, defaultGenerationAgent,
              installCleanup: installViewCleanup,
            }, {
              // A generation that did not land leaves nothing to keep, so the
              // only thing on offer is the form it came from. Success needs no
              // entry here: `render` has already replaced the whole page.
              renderOutcome: (job) => job.status === "done" ? null : el("div", {
                class: "actions",
              }, [el("button", {
                type: "button", class: "button primary",
                text: i18n.t("generate.tryAgain"), onclick: () => render(),
              })]),
            })
          : form,
        ]),
      ]));
      page.append(el("header", { class: "view-intro simplified-workflow-list-heading" }, [
        el("div", {}, [
          el("h2", { text: i18n.t("workflows.generated.heading") }),
          el("p", { class: "muted", text: i18n.t("workflows.generated.description") }),
        ]),
      ]));
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
      // The same predicate the composer uses. Offering the button on the
      // command alone meant a workflow that compiles but has nowhere to put
      // a goal advertised "New goal", and the dialog it opened then declined
      // to preselect it — the person clicked a named workflow and got an
      // empty picker, with nothing saying why.
      if (workflowRunnable(entry) && workflowStartCommand(entry)) {
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
      const deleteCommand = (entry.allowed_commands || []).find(
        (item) => item.command === "workflow.delete",
      );
      const deleteButton = deleteCommand ? el("button", {
        class: "workflow-delete-icon delete-workflow",
        "aria-label": i18n.t("workflows.delete"), title: i18n.t("workflows.delete"),
        onclick: () => workflowViews().openWorkflowDeleteDialog(entry, deleteCommand, render),
      }, [workflowViews().deleteGlyph()]) : null;
      const card = el("article", {
        class: "workflow-card panel",
        "data-workflow-id": entry.workflow_id,
        "data-workflow-slug": entry.slug || "",
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
          // The card's own silent case: a goal binds perfectly well and the
          // engine still will not run it. Said here rather than only in the
          // dialog, because the card is where the missing Start button is.
          entry.goal_readiness === "ready" && engineRefusal(entry)
            ? el("span", {
              class: "muted workflow-card-blocked",
              text: engineRefusal(entry).summary,
            }) : null,
          el("span", { class: "workflow-meta workflow-stats" }, [
            el("span", { text: i18n.t("workflows.nodeCount", {
              count: i18n.number(entry.summary.node_count),
            }) }),
            el("span", { text: i18n.t("workflows.inputCount", {
              count: i18n.number(entry.inputs.length),
            }) }),
          ]),
          // Which version is current, and whether anyone has run it: the two
          // facts that tell two similarly named workflows apart.
          el("span", { class: "workflow-card-facts", text: entry.last_run_at
            ? i18n.t("workflows.lastRun", { when: i18n.dateTime(entry.last_run_at) })
            : i18n.t("workflows.neverRun") }),
        ]),
        cardActions.length || deleteButton
          ? el("div", { class: "workflow-card-actions" }, [
            el("div", { class: "workflow-card-primary-actions" }, cardActions),
            deleteButton,
          ]) : null,
      ]);
      card.querySelector(".workflow-card-main").addEventListener("click", () => navigate({
        view: "workflow", workflowId: entry.workflow_id, runId: null,
      }));
      cards.append(card);
    }
    if (!entries.length) {
      cards.append(el("div", { class: "empty panel", text: i18n.t("workflows.empty") }));
    }
    page.append(cards);
    root.append(page);
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

    root.append(agentFinder(agents, catalog.allowed_commands || []));
  }

  /* Ask an installed Agent what to look for; let the server do the looking.
   *
   * The button produces a patch and never a registration, which is the whole
   * reason this is allowed to exist on a page: the allowlist it proposes into
   * is the thing standing between a workflow step and arbitrary execution, and
   * it grows by review or not at all. So the result is text, the proposed spec
   * carries no invocation, and the copy below says both out loud rather than
   * leaving somebody to assume the button did more than it did. */
  function agentFinder(installed, allowedCommands) {
    const allowed = allowedCommands.find(
      (item) => item.command === "agent.proposal.probe",
    );
    // No command, no form. Whether this may be reached is the server's answer,
    // and a box that posts nowhere is worse than an absent one.
    if (!allowed) return el("div", { hidden: "hidden" });
    const names = installed.map((handler) => handler.name.replace(/^agent\./, ""));
    const chooser = el("select", { class: "input", id: "agentFinderAgent" },
      names.map((name) => el("option", { value: name, text: name })));
    const prompt = el("textarea", {
      class: "input", id: "agentFinderPrompt", required: "required",
      // The server refuses beyond this; saying so here stops a long paste
      // where it is typed instead of after a round trip.
      maxlength: "2000",
      placeholder: i18n.t("agents.find.placeholder"),
    });
    const submit = el("button", {
      class: "button primary", type: "submit",
      text: i18n.t("agents.find.submit"),
      disabled: names.length ? null : "disabled",
    });
    const problem = el("div", { class: "banner error", hidden: "hidden" });
    const results = el("div", { class: "agent-finder-results" });

    const form = el("form", { class: "panel agent-finder" }, [
      el("h3", { text: i18n.t("agents.find.title") }),
      el("p", { class: "muted", text: i18n.t("agents.find.hint") }),
      problem, prompt,
      el("div", { class: "agent-finder-actions" }, [chooser, submit]),
      results,
    ]);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!prompt.value.trim() || !prompt.reportValidity()) return;
      submit.disabled = true;
      problem.hidden = true;
      results.replaceChildren(el("div", {
        class: "muted",
        text: i18n.t("agents.find.searching", { agent: chooser.value }),
      }));
      try {
        const { data } = await api.execute(
          allowed,
          { prompt: prompt.value.trim(), agent: chooser.value },
          // Each press is a fresh look, so each gets its own key rather than
          // replaying the previous answer.
          `agent.proposal.probe:${Date.now()}`,
        );
        results.replaceChildren(...agentFinderResults(data));
      } catch (error) {
        results.replaceChildren();
        problem.hidden = false;
        problem.textContent = error?.message || String(error);
      } finally {
        submit.disabled = false;
      }
    });
    return form;
  }

  function agentFinderResults(data) {
    const out = [];
    if (data.asked_agent) {
      out.push(el("p", {
        class: "muted",
        text: i18n.t("agents.find.asked", { agent: data.asked_agent }),
      }));
    }
    if (!data.proposals.length) {
      out.push(el("div", { class: "muted", text: i18n.t("agents.find.noCandidates") }));
      return out;
    }
    out.push(el("ul", { class: "agent-finder-list" }, data.proposals.map((item) =>
      el("li", { class: `agent-finder-item verdict-${item.verdict}` }, [
        el("span", { class: "mono", text: item.executable }),
        el("span", {
          class: "agent-stat-pill",
          text: i18n.t(`agents.verdict.${item.verdict}`),
        }),
        el("span", { class: "muted", text: item.version || "" }),
      ]))));
    if (data.patch) {
      const copy = el("button", {
        class: "button", type: "button", text: i18n.t("agents.find.copy"),
        onclick: async () => {
          await navigator.clipboard?.writeText(data.patch);
          copy.textContent = i18n.t("agents.find.copied");
        },
      });
      out.push(el("div", { class: "agent-finder-patch" }, [
        el("h4", { text: i18n.t("agents.find.patchTitle") }),
        el("p", { class: "muted", text: i18n.t("agents.find.patchHint") }),
        copy,
        el("pre", { class: "mono agent-finder-diff" }, [el("code", { text: data.patch })]),
      ]));
    }
    return out;
  }

  /* A detail reads as a centred modal over the page that listed it, not a
   * page under it.
   *
   * The owning route stays the address, so the modal is opened by the route
   * and dismissing it navigates back — Escape, the scrim and the Close button
   * all end in the same place. The page behind it stays rendered (dimmed), so
   * a dismissal is one gesture instead of one fetch. The root lives one layer
   * under the sticky topbar, leaving the bar live and un-dimmed.
   *
   * `back.matches(currentRoute)` guards the walk home: a modal closed by a
   * navigation must not undo that navigation. */
  function openPageModal({ label, back, onDismiss = null, placement = "modal" }) {
    const isDrawer = placement === "drawer";
    const panel = el("div", {
      class: `page-modal-panel${isDrawer ? " page-drawer-panel" : ""}`,
      role: "dialog", "aria-modal": "true",
      "aria-label": label, tabindex: "-1",
    }, [dataState(el, i18n, "loading")]);
    const scrim = el("div", { class: "page-modal-scrim" });
    const stage = el("div", { class: "page-modal" }, [panel]);
    const root = el("div", {
      class: `page-modal-root${isDrawer ? " page-drawer-root" : ""}`,
    }, [scrim, stage]);

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
      if (onDismiss) onDismiss();
      teardown();
      root.classList.add("closing");
      // The timeout is a fallback for a transitionend that never fires; both
      // race, so the guard makes the dismissal one act — a second finish
      // landing after another modal has opened would match its route and
      // navigate away from it.
      let finished = false;
      const finish = () => {
        if (finished) return;
        finished = true;
        if (root.isConnected) root.remove();
        if (back.matches(currentRoute)) navigate(back.route);
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
    return { panel, dismiss };
  }

  async function openWorkflowModal(workflowId) {
    const { panel, dismiss } = openPageModal({
      label: i18n.t("workflows.detail"),
      back: {
        matches: (route) => route?.view === "workflow" && route.workflowId === workflowId,
        route: { view: "workflows", runId: null },
      },
    });
    await renderWorkflowDetail(panel, workflowId, dismiss);
  }

  /* A goal from History opens in a right-hand drawer: the list stays rendered
   * under the scrim, while the full-height detail has room to be read like a
   * page. `#/goals/{id}` remains the shareable address. */
  async function openGoalModal(runId) {
    goalModalRunId = runId;
    const { panel, dismiss } = openPageModal({
      label: i18n.t("history.detail"),
      placement: "drawer",
      onDismiss: () => { goalModalRunId = null; },
      back: {
        matches: (route) => route?.view === "goal" && route.runId === runId,
        route: { view: "goals", runId: null },
      },
    });
    await renderGoalDetail(panel, runId, dismiss);
  }

  /* A click-opened modal owns no address, so a background render that
   * rebuilds the page would drop it; re-attaching it afterwards keeps the
   * History reading position the way a run-page selection survives. */
  async function reopenGoalModal() {
    if (goalModalRunId) await openGoalModal(goalModalRunId);
  }

  /* The goal detail follows the History prototype (prototypes/goal-detail/code.html):
   * four titled sections stacked in the drawer — the Goal itself, the Result,
   * the Steps and the flow diagram — each heading outside its card. History
   * is for reading a run back, not starting the next one, so the composer
   * stays out; resume and cancel still surface when the run offers them.
   */

  /* One circle glyph per step fate: the ring scans, the mark inside it says
   * which way, and the colour is never the only carrier of the meaning. */
  function goalStepGlyph(status) {
    const svg = (children) => svgEl("svg", {
      viewBox: "0 0 24 24", width: "20", height: "20", "aria-hidden": "true",
      fill: "none", stroke: "currentColor", "stroke-width": "1.8",
      "stroke-linecap": "round", "stroke-linejoin": "round",
    }, children);
    if (status === "succeeded" || status === "answered") {
      return svg([
        svgEl("circle", { cx: "12", cy: "12", r: "9" }),
        svgEl("path", { d: "M8.5 12.5l2.5 2.5 4.5-5" }),
      ]);
    }
    if (status === "failed" || status === "cancelled") {
      return svg([
        svgEl("circle", { cx: "12", cy: "12", r: "9" }),
        svgEl("path", { d: "M9.5 9.5l5 5" }),
        svgEl("path", { d: "M14.5 9.5l-5 5" }),
      ]);
    }
    if (status === "running" || status === "waiting") {
      return svg([
        svgEl("circle", { cx: "12", cy: "12", r: "9" }),
        svgEl("path", { d: "M12 7.5V12l3 2" }),
      ]);
    }
    if (status === "unknown") {
      return svg([
        svgEl("circle", { cx: "12", cy: "12", r: "9" }),
        svgEl("path", { d: "M9.8 9.6a2.3 2.3 0 014.4.9c0 1.5-2.2 1.9-2.2 3" }),
        svgEl("path", { d: "M12 16.6v.2" }),
      ]);
    }
    return svg([svgEl("circle", { cx: "12", cy: "12", r: "9" })]);
  }

  function goalStatusPill(status) {
    return el("span", { class: `goal-status-pill ${status}` }, [
      goalStepGlyph(status === "completed" ? "succeeded" : status),
      el("span", { text: i18n.status(status) }),
    ]);
  }

  /* The Goal as the reader wrote it: text, not a title.
   *
   * A goal is frequently a paragraph — the facts of an incident, the
   * constraints on an answer — and a heading renders it as one long line
   * with its newlines collapsed. So it keeps its line breaks, folds at 120px
   * and unfolds in place, and the block never grows taller than the reader
   * asked for.
   *
   * Shared by the history detail and the page a run is watched on, because
   * it is the same goal: the run page drew it as a heading, and the same
   * paste that read properly in history became a wall of text there.
   */
  function goalTextBlock(run, tag = "h1") {
    const clamp = el("div", { class: "goal-text-clamp" }, [
      el(tag, { class: "goal-text", text: runName(run) }),
    ]);
    const label = el("span", { text: i18n.t("goal.showAll") });
    const toggle = el("button", {
      class: "goal-expand-toggle", type: "button",
      onclick: () => {
        const expanded = clamp.classList.toggle("expanded");
        label.textContent = i18n.t(expanded ? "goal.collapse" : "goal.showAll");
        toggle.classList.toggle("expanded", expanded);
      },
    }, [
      label,
      svgEl("svg", {
        viewBox: "0 0 24 24", width: "18", height: "18", "aria-hidden": "true",
        fill: "none", stroke: "currentColor", "stroke-width": "2",
        "stroke-linecap": "round", "stroke-linejoin": "round",
      }, [svgEl("path", { d: "M6 9l6 6 6-6" })]),
    ]);
    // The toggle earns its place only when the text actually overflows —
    // which cannot be answered until the block has been laid out, and one
    // frame is not that moment: a block built before its page is attached
    // measures 0 against 0 and hides a toggle the reader needs. A resize
    // observer answers on first layout and again whenever the answer could
    // have changed, which a narrowing window does by rewrapping the text.
    const measure = new ResizeObserver(() => {
      if (!clamp.isConnected) return;
      toggle.hidden = clamp.scrollHeight <= clamp.clientHeight;
    });
    measure.observe(clamp);
    return [clamp, toggle];
  }

  function goalTextCard(run) {
    return el(
      "section", { class: "goal-card goal-text-card" }, goalTextBlock(run),
    );
  }

  /* The run's facts as the engine reports them: identity lines, the final
   * status, and the result document under one code block. */
  function goalResultCard(run) {
    const lines = [run.workflow_id, run.run_id, "", run.status];
    if (run.result !== null && run.result !== undefined) {
      lines.push(JSON.stringify(run.result, null, 2));
    }
    const actions = runActionButtons(run);
    return el("section", { class: "goal-card goal-result-card" }, [
      goalStatusPill(run.status),
      el("pre", { class: "goal-result-code", text: lines.join("\n") }),
      run.interrupts?.length ? el("pre", {
        class: "goal-result-code", text: JSON.stringify(run.interrupts, null, 2),
      }) : null,
      run.error ? el("div", { class: "banner error", text: run.error }) : null,
      actions.length ? el("div", { class: "actions" }, actions) : null,
    ]);
  }

  function goalStepsCard(run, steps) {
    const card = el("section", { class: "goal-card goal-steps-card" });
    if (steps === null) {
      card.append(el("p", {
        class: "muted goal-steps-note", text: i18n.t("simplified.steps.unavailable"),
      }));
      return card;
    }
    if (!steps.length) {
      card.append(el("p", {
        class: "muted goal-steps-note", text: i18n.t("simplified.steps.empty"),
      }));
      return card;
    }
    const live = !TERMINAL_LANGGRAPH_STATUSES.has(run.status);
    for (const step of steps) {
      card.append(el("div", { class: `goal-step-row ${step.status}` }, [
        el("span", { class: "goal-step-glyph" }, [goalStepGlyph(step.status)]),
        el("div", { class: "goal-step-copy" }, [
          el("div", { class: "goal-step-line" }, [
            el("span", { class: "goal-step-label", text: step.label }),
            el("span", { class: `pill ${step.status}`,
              text: i18n.t(`simplified.steps.status.${step.status}`) }),
          ]),
          el("span", { class: "goal-step-handler muted", text: [
            step.handler ? step.handler.name : step.kind,
            step.runs > 1
              ? i18n.t("simplified.steps.repeated", { count: i18n.number(step.runs) })
              : null,
          ].filter(Boolean).join(" · ") }),
          step.status !== "not_reached"
            ? runConsole(run.run_id, {
              live, nodeId: step.node_id, prompt: step.prompt,
              hideWhenEmpty: true, showState: false,
            }) : null,
        ]),
      ]));
    }
    return card;
  }

  /* The diagram under its own heading, on the dotted canvas the prototype
   * draws it on. The frame fetches on its own, like an Artifact thumbnail:
   * the card is on the page immediately and a slow viewer never delays the
   * steps above it. */
  function goalCanvasSection(body, run, steps) {
    const url = shellFacts?.capabilities?.workflow_viewer?.available
      ? (shellFacts.capabilities.workflow_viewer.url || "/viewer/")
      : null;
    if (!url) return;
    body.append(
      el("h2", { class: "goal-section-title", text: i18n.t("workflows.graph") }),
    );
    const pane = el("div", { class: "goal-canvas-body" });
    body.append(el("section", { class: "goal-card goal-canvas-card" }, [pane]));
    (async () => {
      try {
        const response = await api.runGraph(run.run_id);
        pane.append(embeddedGraph(response.data.graph, {
          viewerUrl: () => url, i18n,
          statuses: Object.fromEntries(steps.map((step) => [step.node_id, step.status])),
        }));
      } catch (error) {
        pane.append(el("p", { class: "muted", text: error?.messageKey
          ? i18n.t(error.messageKey, { message: error.message })
          : i18n.t("simplified.steps.unavailable") }));
      }
    })();
  }

  async function renderGoalDetail(panel, runId, dismiss) {
    const draw = async () => {
      panel.replaceChildren(dataState(el, i18n, "loading"));
      try {
        const run = (await api.langGraphRun(runId)).data;
        let steps = [];
        try {
          steps = (await api.runSteps(runId)).data.steps || [];
        } catch (error) {
          // The sections above and below still answer; the step card says
          // so in place rather than taking the whole detail down.
          steps = null;
        }
        const body = el("div", { class: "goal-modal-body goal-detail" });
        panel.replaceChildren(
          el("div", { class: "goal-modal-head" }, [
            el("h2", {
              class: "goal-section-title", text: i18n.t("goal.heading.goal"),
            }),
            el("button", {
              class: "goal-modal-close", type: "button",
              "aria-label": i18n.t("action.close"),
              title: i18n.t("action.close"),
              onclick: () => dismiss(),
            }, [
              svgEl("svg", {
                viewBox: "0 0 24 24", width: "16", height: "16",
                "aria-hidden": "true", fill: "none", stroke: "currentColor",
                "stroke-width": "1.8", "stroke-linecap": "round",
              }, [
                svgEl("path", { d: "M6 6l12 12" }),
                svgEl("path", { d: "M18 6L6 18" }),
              ]),
            ]),
          ]),
          body,
        );
        body.append(
          goalTextCard(run),
          el("h2", {
            class: "goal-section-title", text: i18n.t("goal.heading.result"),
          }),
          goalResultCard(run),
          el("h2", { class: "goal-section-title", text: i18n.t("simplified.steps") }),
          goalStepsCard(run, steps),
        );
        goalCanvasSection(body, run, steps || []);
        await appendRunArtifacts(body, run.run_id);
      } catch (error) {
        panel.replaceChildren(dataState(el, i18n, "error", { onRetry: draw }));
        reportError(error);
      }
    };
    await draw();
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
          engineRefusalNotice(value),
          agentBindingNotice(value),
          handlerDriftNotice(value, draw),
          // The drawing answers "what shape is this", the definition list
          // answers "what exactly is in it" — one tabbed surface for both.
          workflowViews().workflowDefinitionTabs(
            value.graph, definition, "workflows.definition",
            {}, null, effectiveBindings(value),
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
            agentBindingNotice(current),
            handlerDriftNotice(current, refreshPublished),
            workflowViews().workflowDefinitionTabs(
              current.graph, current.definition, "workflows.definition",
              currentEditors,
              (nodeId) => workflowViews().openActionEditorDialog(
                current, nodeId, refreshPublished,
              ),
              effectiveBindings(current),
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
              // Which version is being edited, and nothing about how the work
              // is going: the panel below owns that, and a header saying
              // "awaiting an instruction" while the panel says the Agent is
              // working was two answers to one question.
              editingVersion,
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
        await renderBranchFindings(page, workflowId);
      } catch (error) {
        page.replaceChildren(dataState(el, i18n, "error", { onRetry: draw }));
        reportError(error);
      }
    };

    await draw();
  }

  /* Branches the runs have never entered.
   *
   * The run page says which way one run went; it cannot call a branch dead,
   * because on any single run exactly one branch of a fork is taken. Only the
   * tally can, so the finding belongs to the definition and appears where the
   * definition is edited.
   *
   * Only the finding is drawn — a branch decided and never entered. The rest
   * of the tally is a fact about normal operation and would bury it.
   */
  async function renderBranchFindings(page, workflowId) {
    let report;
    try {
      report = (await api.workflowBranches(workflowId)).data;
    } catch (error) {
      return;
    }
    const findings = (report.edges || []).filter(
      (edge) => edge.verdict === "never_taken",
    );
    if (!findings.length) return;
    page.append(el("section", { class: "panel workflow-branch-findings" }, [
      el("div", { class: "panel-head" }, [
        el("div", { class: "panel-title",
          text: i18n.t("workflows.branchFindings") }),
      ]),
      el("p", { class: "muted", text: i18n.t("workflows.branchFindings.hint", {
        runs: i18n.number(report.runs),
      }) }),
      el("ul", { class: "workflow-branch-list" }, findings.map((edge) => el(
        "li", { class: "workflow-branch-finding" }, [
          el("span", { class: "run-branch-target",
            text: `${edge.source_node} → ${edge.target_node}` }),
          el("span", { class: "muted", text: i18n.t(
            "workflows.branchFindings.count",
            { decided: i18n.number(edge.decided) },
          ) }),
        ],
      ))),
    ]));
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
              ? "authoring.job.modify.queued" : "authoring.job.modify.running",
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
            workflowViews().workflowDefinitionTabs(
              previewGraph, definition, "editor.draftDefinition",
            ),
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
          ...authoringFailureView(job.error),
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


  /* A failed authoring job: what it said, and what the compiler refused.
   *
   * The message alone reads as "it did not work". The finding codes are what
   * tells an operator — and whoever tunes the prompt — which rule was broken.
   */
  function authoringFailureView(error) {
    if (!error) return [];
    return [
      el("div", { class: "banner error", text: error.message }),
      ...authoringFailureCodes(error),
    ];
  }

  /* The codes on their own, for a surface that already shows the message.
   *
   * The progress panel prints `error.message` itself, so a caller there wants
   * only the half it does not already have — two error banners saying the same
   * sentence is worse than one. */
  function authoringFailureCodes(error) {
    const codes = [...new Set(
      (error?.diagnostics || []).map((item) => item.code).filter(Boolean)
    )];
    return codes.length ? [el("p", {
      class: "muted mono authoring-failure-codes", text: codes.join(" · "),
    })] : [];
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
    // The progress panel owns a timer; a redraw that swaps it out must stop it.
    let stopProgress = null;

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
      } catch (error) {
        reportError(error);
      }
    };

    /* What is on offer once the Agent has stopped.
     *
     * The record above it stays either way. A settled job is gone from the
     * `active_job` the page is built from, so nothing could fetch it back —
     * what the Agent was asked, who ran it and what it printed exist only
     * here, and throwing them away the moment it finishes would answer "what
     * just changed?" with an empty form. */
    const outcomeNodes = (settled) => settled.status === "done"
      ? [
        ...changeSummaryView(settled.result?.change_summary),
        el("div", { class: "actions" }, [el("button", {
          type: "button", class: "button primary", id: "confirmRevision",
          text: i18n.t("editor.revision.confirm"),
          // The diagram is already current; this is the reader saying they
          // are done with the account, and a full re-fetch is how the panel
          // gets back to an empty instruction box.
          onclick: () => onDone(),
        })]),
      ]
      : [
        // The panel already prints the Agent's message; this adds the half it
        // does not have — which compiler rule was broken.
        ...authoringFailureCodes(settled.error),
        el("div", { class: "actions" }, [el("button", {
          type: "button", class: "button", id: "retryWorkflowModify",
          text: i18n.t("simplified.workflow.tryAgain"),
          // Local, not a page redraw: `regenerateOffered` is the memory that a
          // plain modify has already been tried, and a re-fetch would lose it.
          onclick: () => { regenerateOffered = true; job = null; draw(); },
        })]),
      ];

    const formPose = () => {
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
      const start = (mode) => () => {
        if (!prompt.value.trim()) return;
        promptText = prompt.value.trim();
        submit(mode);
      };
      return [
        writerField,
        // An "authored by" section over a titled prompt section whose textarea
        // carries a syntax hint.
        el("section", { class: "workflow-modify-prompt" }, [
          el("h3", { class: "field-label", text: i18n.t("editor.agentPromptTitle") }),
          el("p", { class: "muted", text: i18n.t("editor.agentPromptHint") }),
          el("div", { class: "workflow-modify-input-wrap" }, [
            prompt,
            el("span", {
              class: "workflow-modify-syntax", text: i18n.t("editor.promptSyntaxHint"),
            }),
          ]),
        ]),
        regenerateOffered ? el("p", {
          class: "muted", text: i18n.t("simplified.workflow.regenerate.hint"),
        }) : null,
        el("div", { class: "actions" }, [
          el("button", {
            type: "button", class: "button primary",
            text: i18n.t("editor.agentRevise"), onclick: start("modify"),
          }),
          regenerateOffered ? el("button", {
            type: "button", class: "button", id: "regenerateWorkflow",
            text: i18n.t("simplified.workflow.regenerate"), onclick: start("regenerate"),
          }) : null,
        ].filter(Boolean)),
      ].filter(Boolean);
    };

    const draw = () => {
      if (stopProgress) { stopProgress(); stopProgress = null; }
      if (!job) {
        section.replaceChildren(...formPose());
        return;
      }
      // The same panel the catalog page uses to watch a generation: one job
      // shape, one pipeline, one thing to read. What differs is the wording
      // and what is offered at the end, and those are the arguments.
      section.replaceChildren(workflowGenerationProgress(
        job,
        // The picture catches up straight away — a summary of a change is
        // read against the thing it changed, and leaving the old diagram
        // under a published revision is showing two different answers. What
        // waits is only the way out: the account of what happened stays until
        // it is dismissed.
        async (settled) => {
          job = settled;
          if (onPublished) await onPublished();
        },
        {
          api, i18n, reportError, defaultGenerationAgent,
          installCleanup: (stop) => { stopProgress = stop; installViewCleanup(stop); },
        },
        {
          statusPrefix: "authoring.job.modify",
          progressTitleKey: "editor.revision.progress.title",
          promptLabelKey: "editor.revision.instruction",
          agentLabelKey: "editor.revisedBy",
          renderOutcome: outcomeNodes,
        },
      ));
    };

    // A job on screen — running, or finished and unread — is this panel's to
    // dismiss, and so is an instruction someone is still typing. A background
    // render would take all three away without being asked.
    viewHolds = () => Boolean(job)
      || Boolean(section.querySelector("textarea")?.value.trim());
    installViewCleanup(() => { viewHolds = null; });

    draw();
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
      const active = (await api.langGraphRuns({ limit: 25 })).data.runs.find(
        (item) => ["running", "waiting", "interrupted"].includes(item.status),
      );
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


  return {
    update(next) {
      i18n = next.i18n; shellFacts = next.shellFacts; mayStartRun = next.mayStartRun;
      if (next.route) currentRoute = next.route;
    },
    canLeave() { return !activeViewLeaveGuard || activeViewLeaveGuard(); },
    cleanup() { if (activeViewCleanup) activeViewCleanup(); activeViewCleanup = null; },
    stopPolling() { if (refreshTimer) clearTimeout(refreshTimer); },
    renderSimplifiedWorkspace, renderHistory, renderWorkflows, openWorkflowModal,
    openGoalModal, reopenGoalModal,
    renderWorkflowEdit, renderAgents, refreshRuntimeCard,
    syncRefreshIntervalSelect, saveRefreshInterval, scheduleLivePolling,
  };
}
