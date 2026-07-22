/* Command payload dialogs.
 *
 * These components collect input only. The caller still executes the exact
 * AllowedCommand advertised by the server, so moving them out of app.js does
 * not create a second command table or a second Runtime state machine.
 */

/** Resolve to `collect()` on confirm, or null on every dismissal path. */
export function dialogResult(dialog, collect, validate) {
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
      if (confirmed && validate && !validate()) {
        event.preventDefault();
        return;
      }
      setTimeout(() => settle(confirmed ? collect() : null), 0);
    });
    dialog.addEventListener("close", () => setTimeout(() => settle(null), 0));

    document.body.append(dialog);
    dialog.showModal();
  });
}

export function humanSubmitDialog(context, allowed, siblings = []) {
  const { api, el, i18n, reportError, tokenRequired = true } = context;
  const decision = allowed.command.endsWith("reject")
    ? "reject"
    : allowed.command.endsWith("provide_input") ? "provide_input" : "approve";
  const token = el("input", {
    type: "password", id: "humanToken",
    ...(tokenRequired ? { required: "required" } : {}),
    autocomplete: "off", spellcheck: "false",
  });
  const value = el("textarea", { id: "humanValue" });
  const valueError = el("div", {
    class: "banner error", id: "humanValueError", hidden: "hidden", role: "alert",
  });
  const tokenCommand = tokenRequired
    ? siblings.find((item) => item.command === "human.token")
    : null;
  const fetchToken = tokenCommand
    ? el("button", {
        type: "button",
        class: "button",
        id: "humanTokenFetch",
        text: i18n.t("human.token.fetch"),
        onclick: async () => {
          try {
            const response = await api.execute(tokenCommand, {});
            token.value = response.data.submission_token;
            allowed.expected_version = response.data.expected_version;
            tokenCommand.expected_version = response.data.expected_version;
          } catch (error) {
            reportError(error);
            if (error.requiresRefresh) {
              dialog.close();
              window.dispatchEvent(new Event("orbit:refresh"));
            }
          }
        },
      })
    : null;
  const dialog = el("dialog", { "aria-label": i18n.t("human.title") }, [
    el("form", { method: "dialog" }, [
      el("h2", { text: i18n.t("human.title") }),
      el("p", {
        class: "muted",
        text: `${i18n.t("human.decision")}: ${i18n.t(`human.decision.${decision}`)}`,
      }),
      ...(tokenRequired ? [el("div", { class: "field" }, [
        el("label", { for: "humanToken", text: i18n.t("human.token") }),
        token,
        ...(fetchToken ? [fetchToken] : []),
        el("small", { class: "muted", text: i18n.t("human.token.hint") }),
      ])] : []),
      el("div", { class: "field" }, [
        el("label", { for: "humanValue", text: i18n.t("human.value") }),
        value,
        valueError,
      ]),
      el("div", { class: "actions" }, [
        el("button", { class: "button", value: "cancel", text: i18n.t("action.cancel") }),
        el("button", {
          class: "button primary", value: "confirm", text: i18n.t("action.submit"),
        }),
      ]),
    ]),
  ]);
  let parsedValue = null;
  const validate = () => {
    valueError.hidden = true;
    value.removeAttribute("aria-invalid");
    if (!value.value.trim()) {
      parsedValue = null;
      return true;
    }
    try {
      parsedValue = JSON.parse(value.value);
      return true;
    } catch {
      valueError.textContent = i18n.t("human.value.invalid");
      valueError.hidden = false;
      value.setAttribute("aria-invalid", "true");
      value.focus();
      return false;
    }
  };
  return dialogResult(
    dialog,
    () => ({ submission_token: token.value, decision, value: parsedValue }),
    validate,
  );
}

export function budgetDialog({ el, i18n }) {
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
          class: "button primary", value: "confirm", text: i18n.t("action.submit"),
        }),
      ]),
    ]),
  ]);
  return dialogResult(dialog, () => ({ amount_microunits: Number(amount.value) }));
}

export function cancelRunDialog({ el, i18n }) {
  const reason = el("textarea", { id: "cancelReason", required: "required" });
  reason.value = i18n.t("cancel.defaultReason");
  const dialog = el("dialog", { "aria-label": i18n.t("cancel.title") }, [
    el("form", { method: "dialog" }, [
      el("h2", { text: i18n.t("cancel.title") }),
      el("p", { class: "muted", text: i18n.t("cancel.confirm") }),
      el("div", { class: "field" }, [
        el("label", { for: "cancelReason", text: i18n.t("cancel.reason") }),
        reason,
      ]),
      el("div", { class: "actions" }, [
        el("button", { class: "button", value: "cancel", text: i18n.t("action.cancel") }),
        el("button", {
          class: "button danger", value: "confirm", text: i18n.t("command.run.cancel"),
        }),
      ]),
    ]),
  ]);
  return dialogResult(dialog, () => ({ reason: reason.value.trim() }));
}

/** Running a step again may repeat whatever the Agent already did outside the
 *  Runtime, so the operator confirms rather than clicks once. */
export function retryNodeDialog({ el, i18n }, allowed) {
  const dialog = el("dialog", { "aria-label": i18n.t("retry.title") }, [
    el("form", { method: "dialog" }, [
      el("h2", { text: i18n.t("retry.title") }),
      el("p", { text: i18n.t("retry.confirm") }),
      el("div", { class: "mono muted", text: allowed.target_aggregate_id }),
      el("div", { class: "actions" }, [
        el("button", { class: "button", value: "cancel", text: i18n.t("action.cancel") }),
        el("button", {
          class: "button primary", value: "confirm",
          text: i18n.t("command.node.retry"),
        }),
      ]),
    ]),
  ]);
  return dialogResult(dialog, () => ({}));
}

export function recoveryDialog({ el, i18n }, allowed) {
  const actionId = allowed.action_id || allowed.target_aggregate_id;
  const dialog = el("dialog", { "aria-label": i18n.t("recovery.title") }, [
    el("form", { method: "dialog" }, [
      el("h2", { text: i18n.t("recovery.title") }),
      el("p", { text: i18n.t("recovery.confirm") }),
      el("div", { class: "mono muted", text: actionId }),
      el("div", { class: "actions" }, [
        el("button", { class: "button", value: "cancel", text: i18n.t("action.cancel") }),
        el("button", {
          class: "button primary", value: "confirm", text: i18n.t("action.apply"),
        }),
      ]),
    ]),
  ]);
  return dialogResult(dialog, () => ({ action_ids: [actionId] }));
}
