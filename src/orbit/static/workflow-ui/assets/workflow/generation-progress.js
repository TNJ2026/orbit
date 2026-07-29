import { ApiError } from "../api.js";
import { el } from "../components/dom.js";

/** Render and own the polling lifecycle for one workflow generation job. */
export function workflowGenerationProgress(initialJob, onSettled, context) {
  const {
    api, i18n, reportError, defaultGenerationAgent, installCleanup,
  } = context;
  const refreshIntervalMs = 3000;
  let job = initialJob;
  let after = 0;
  let timer = null;
  let stopped = false;
  let outputUnavailable = false;
  let progressStage = job.status === "queued" ? "preparing" : "generating";
  let progressAttempt = null;
  let progressMaximum = null;
  const progressSteps = ["preparing", "generating", "validating", "publishing"];
  const progressItems = progressSteps.map((stage, index) => el("li", {
    class: "workflow-generation-step pending",
  }, [
    el("span", { class: "workflow-generation-step-marker", text: String(index + 1) }),
    el("span", { class: "workflow-generation-step-label", text: i18n.t(`generate.progress.${stage}`) }),
  ]));
  const progressDetail = el("span", { class: "muted workflow-generation-progress-detail" });

  const updateProgress = (stage, attempt = null, maximum = null, failed = false) => {
    progressStage = stage || progressStage;
    progressAttempt = attempt ?? progressAttempt;
    progressMaximum = maximum ?? progressMaximum;
    const normalized = progressStage === "repairing"
      || (progressStage === "generating" && progressAttempt > 1)
      ? "validating"
      : progressStage === "validated" ? "publishing" : progressStage;
    const current = Math.max(0, progressSteps.indexOf(normalized));
    progressItems.forEach((item, index) => {
      item.className = `workflow-generation-step ${index < current ? "completed"
        : index === current ? (failed ? "failed" : "current") : "pending"}`;
    });
    progressDetail.textContent = progressAttempt && progressMaximum
      ? i18n.t("generate.progress.attempt", { current: progressAttempt, total: progressMaximum })
      : "";
  };
  updateProgress(progressStage);
  const state = el("strong", { text: i18n.t(`authoring.job.${job.status}`) });
  const jobState = el("div", { class: `authoring-job-state ${job.status}` }, [
    el("span", { class: "live-dot", "aria-hidden": "true" }), state,
  ]);
  const empty = el("span", {
    class: "muted workflow-generation-output-empty",
    text: i18n.t("generate.outputWaiting"),
  });
  const log = el("pre", {
    class: "console-log workflow-generation-console", role: "log", tabindex: "0",
    "aria-label": i18n.t("editor.agentConsole"),
  });
  const failure = el("div", {
    class: "banner error workflow-generation-failure", hidden: "hidden",
  });
  const retry = el("button", {
    type: "button", class: "button primary", hidden: "hidden",
    text: i18n.t("generate.tryAgain"), onclick: () => onSettled(),
  });
  const cancelGeneration = el("button", {
    type: "button", class: "button workflow-generation-cancel",
    text: i18n.t("action.cancel"),
    hidden: !(job.allowed_commands || []).some(
      (item) => item.command === "workflow.authoring.cancel",
    ),
    onclick: async (event) => {
      const cancel = (job.allowed_commands || []).find(
        (item) => item.command === "workflow.authoring.cancel",
      );
      if (!cancel) return;
      event.currentTarget.disabled = true;
      try {
        job = (await api.execute(
          cancel, {}, `workflow.authoring.cancel:${job.job_id}`,
        )).data;
        stopped = true;
        if (timer) clearTimeout(timer);
        await onSettled();
      } catch (error) {
        event.currentTarget.disabled = false;
        reportError(error);
      }
    },
  });

  const updateCancel = () => {
    cancelGeneration.hidden = !(job.allowed_commands || []).some(
      (item) => item.command === "workflow.authoring.cancel",
    );
  };

  const pumpOutput = async () => {
    if (outputUnavailable || !job.output_href) return;
    try {
      for (let page = 0; page < 20; page += 1) {
        const payload = (await api.get(`${job.output_href}?after=${after}`)).data;
        for (const chunk of payload.chunks) {
          if (chunk.text.startsWith("\x1eorbit-progress:")) {
            try {
              const event = JSON.parse(chunk.text.slice("\x1eorbit-progress:".length));
              updateProgress(event.stage, event.attempt, event.max_attempts);
            } catch (_) {
              // A malformed diagnostic event must not hide the Agent's output.
            }
            after = chunk.chunk_id;
            continue;
          }
          log.append(el("span", {
            class: `console-chunk ${chunk.stream}`, text: chunk.text,
          }));
          after = chunk.chunk_id;
        }
        if (!payload.has_more) break;
      }
      empty.hidden = Boolean(log.childElementCount);
    } catch (error) {
      if (error instanceof ApiError && [401, 403].includes(error.status)) {
        outputUnavailable = true;
        empty.textContent = i18n.t("generate.outputUnavailable");
      }
    }
  };

  const poll = async () => {
    if (stopped) return;
    try {
      job = (await api.get(job.href)).data;
      state.textContent = i18n.t(`authoring.job.${job.status}`);
      jobState.className = `authoring-job-state ${job.status}`;
      updateCancel();
      await pumpOutput();
      if (["queued", "running"].includes(job.status)) {
        timer = setTimeout(poll, refreshIntervalMs);
      } else if (job.status === "done") {
        updateProgress("publishing");
        progressItems.forEach((item) => { item.className = "workflow-generation-step completed"; });
        stopped = true;
        await onSettled();
      } else {
        stopped = true;
        updateProgress(progressStage, progressAttempt, progressMaximum, true);
        failure.textContent = job.error?.message
          || i18n.t(`authoring.job.${job.status}`);
        failure.hidden = false;
        retry.hidden = false;
      }
    } catch (error) {
      if (!(error instanceof ApiError)) throw error;
      timer = setTimeout(poll, refreshIntervalMs);
    }
  };

  installCleanup(() => {
    stopped = true;
    if (timer) clearTimeout(timer);
  });
  pumpOutput().then(() => {
    if (!stopped) timer = setTimeout(poll, refreshIntervalMs);
  });

  return el("section", { class: "workflow-generation-progress" }, [
    el("div", { class: "workflow-generation-request" }, [
      el("div", { class: "workflow-generation-prompt" }, [
        el("div", { class: "field-label", text: i18n.t("generate.instruction") }),
        el("div", { class: "workflow-generation-prompt-value", text: job.prompt }),
      ]),
      el("div", { class: "workflow-generation-agent" }, [
        el("div", { class: "field-label", text: i18n.t("generate.writtenBy") }),
        el("div", {
          class: "agent-choice-static mono",
          text: job.requested_agent || defaultGenerationAgent(),
        }),
      ]),
    ]),
    el("section", { class: "workflow-generation-stages", "aria-label": i18n.t("generate.progress.title") }, [
      el("div", { class: "workflow-generation-stages-head" }, [
        el("h3", { text: i18n.t("generate.progress.title") }),
        progressDetail,
      ]),
      el("ol", { class: "workflow-generation-step-list" }, progressItems),
    ]),
    el("section", { class: "workflow-generation-output" }, [
      el("div", { class: "workflow-generation-output-head" }, [
        el("h3", { text: i18n.t("editor.agentConsole") }),
        el("div", { class: "workflow-generation-output-actions" }, [
          jobState, cancelGeneration,
        ]),
      ]),
      empty,
      log,
      failure,
      el("div", { class: "actions workflow-generation-failure-actions" }, [retry]),
    ]),
  ]);
}
