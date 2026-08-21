/* Asking the reader something, in this application rather than the browser's.
 *
 * `window.confirm` and `window.prompt` are the browser's chrome: they take
 * the theme, the typeface and the button words of the platform rather than
 * this UI, they cannot say which run or which step is being asked about
 * beyond one line of plain text, and they block the page while they are up.
 * Every other question this UI asks is a `<dialog>`; these are too.
 *
 * Both resolve rather than return, because the answer arrives when the reader
 * gives it. Dismissing — Escape, the backdrop, the cancel button — resolves
 * with the same answer as declining, so a caller has one thing to check.
 */

function shell(el, { titleId, title, body, actions }) {
  const dialog = el("dialog", { class: "app-dialog", "aria-labelledby": titleId });
  const form = el("form", { method: "dialog" }, [
    el("h2", { id: titleId, text: title }),
    ...body,
    el("div", { class: "actions" }, actions),
  ]);
  dialog.append(form);
  dialog.addEventListener("close", () => dialog.remove(), { once: true });
  document.body.append(dialog);
  return { dialog, form };
}

/** Yes or no, resolving false on every way of declining. */
export function askConfirm(el, i18n, {
  title, message, confirmLabel, danger = false,
}) {
  return new Promise((resolve) => {
    let answer = false;
    const confirm = el("button", {
      type: "submit", class: `button ${danger ? "danger" : "primary"}`,
      text: confirmLabel,
    });
    const cancel = el("button", {
      type: "button", class: "button", text: i18n.t("action.cancel"),
    });
    const { dialog, form } = shell(el, {
      titleId: "appDialogTitle", title,
      body: [el("p", { text: message })],
      actions: [cancel, confirm],
    });
    cancel.addEventListener("click", () => dialog.close());
    form.addEventListener("submit", () => { answer = true; });
    dialog.addEventListener("close", () => resolve(answer), { once: true });
    dialog.showModal();
    cancel.focus();
  });
}

/** Some text, resolving null on every way of declining. */
export function askText(el, i18n, {
  title, label, value = "", confirmLabel, multiline = true,
}) {
  return new Promise((resolve) => {
    const fieldId = "appDialogField";
    const input = multiline
      ? el("textarea", { id: fieldId, rows: "6", text: value })
      : el("input", { id: fieldId, type: "text", value });
    let answer = null;
    const confirm = el("button", {
      type: "submit", class: "button primary", text: confirmLabel,
    });
    const cancel = el("button", {
      type: "button", class: "button", text: i18n.t("action.cancel"),
    });
    const { dialog, form } = shell(el, {
      titleId: "appDialogTitle", title,
      body: [el("div", { class: "field" }, [
        el("label", { for: fieldId, text: label }), input,
      ])],
      actions: [cancel, confirm],
    });
    cancel.addEventListener("click", () => dialog.close());
    form.addEventListener("submit", () => { answer = input.value; });
    dialog.addEventListener("close", () => resolve(answer), { once: true });
    dialog.showModal();
    input.focus();
  });
}
