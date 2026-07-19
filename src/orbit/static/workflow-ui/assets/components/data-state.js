/* The five generic data states (delivery plan P1).
 *
 * Every view expresses "nothing to show yet" through one of these, so an
 * empty inbox, a failed fetch and a capability the server does not provide
 * are visually and semantically distinct — never a blank region, and never
 * colour alone (each state carries its text and a distinct border).
 *
 * `error` takes an optional retry callback; `pending` is for a submitted
 * command whose new projection has not confirmed yet; `stale` marks data
 * that failed to refresh but is still on screen.
 */

export function dataState(el, i18n, kind, options = {}) {
  const { message, onRetry, retryLabel } = options;
  const children = [
    el("div", { text: message || i18n.t(`state.${kind}`) }),
  ];
  if (onRetry) {
    children.push(
      el("div", { class: "actions" }, [
        el("button", {
          class: "button",
          text: retryLabel || i18n.t("state.retry"),
          onclick: onRetry,
        }),
      ]),
    );
  }
  return el("div", { class: `data-state ${kind}`, role: kind === "error" ? "alert" : "status" }, children);
}
