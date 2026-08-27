/** Putting the caret where the person is about to type.
 *
 * Picking a Workflow writes a half-finished sentence into the draft — "用
 * 「名字」执行：" — and leaves the goal for the person to add. So the caret
 * belongs at the end of it. It was not going there: the draft is written
 * through the input machine, and the machine has no caret. The composer's own
 * `restoreCaret` is scoped to the cut and paste handlers that already hold the
 * element, and nothing on the plugin-facing facade — `setDraft`,
 * `insertReference`, `insertText`, `track` — carries a caret position. The
 * caret lives in a `<textarea>` in the shell's DOM, and reaching it is the
 * only way there is.
 *
 * Which is a real cost, so the reach is kept small and honest: one attribute
 * selector, a rule that refuses to guess between two candidates, and no
 * assumption that the write has landed by the time we look.
 */

/** The composer's textarea. `data-phase` is the input machine's own phase,
 *  written on the element it belongs to, so it marks a composer rather than
 *  any textarea a page happens to contain. */
export const COMPOSER_SELECTOR = 'textarea[data-phase]'

/** How many frames to wait for the draft to arrive before giving up.
 *
 * The write is asynchronous — machine, store, then React — and a single frame
 * is the shell's own assumption for its own writes, not a guarantee about
 * somebody else's. Six frames is a tenth of a second at 60Hz: long enough to
 * outlast a slow render, short enough that a person who started typing in the
 * meantime is not interrupted, because by then the draft no longer matches.
 */
export const CARET_ATTEMPTS = 6

/**
 * The composer to act on, or nothing.
 *
 * Focus first: with more than one Session rendered, the focused composer is
 * the one the person is looking at. Otherwise the only one, if there is only
 * one. Two unfocused candidates is a genuine ambiguity, and moving the caret
 * in the wrong conversation is worse than leaving it where it was.
 */
export function pickComposer<T extends { readonly ownerDocument?: unknown }>(
  candidates: readonly T[], focused: unknown,
): T | null {
  if (candidates.length === 0) return null
  const active = candidates.find(candidate => candidate === focused)
  if (active !== undefined) return active
  return candidates.length === 1 ? (candidates[0] ?? null) : null
}

/**
 * Whether this element is showing the draft we just wrote.
 *
 * Guards against two races at once: a render that has not happened yet, and a
 * person who has already started typing. The first shows the old draft, the
 * second shows a longer one — and in both cases the answer is to leave the
 * caret alone rather than to move it somewhere that made sense a moment ago.
 */
export function draftHasLanded(value: string, expected: string): boolean {
  return value === expected
}

/**
 * Put the caret at the end of the draft the machine says it has.
 *
 * `expected` comes from the input's own snapshot rather than from rebuilding
 * the string here: inserting a reference splices a placeholder and sometimes a
 * space, and a second opinion about what that produced would be wrong the day
 * the shell changes it.
 *
 * Focus comes with it. The pick happened in a popup, which had focus; leaving
 * the caret correct in an unfocused box would be a caret nobody is typing at.
 */
export function caretToEnd(expected: string, attempts = CARET_ATTEMPTS): void {
  if (typeof document === 'undefined' || typeof requestAnimationFrame !== 'function') return
  const attempt = (left: number): void => {
    const found = [...document.querySelectorAll(COMPOSER_SELECTOR)]
      .filter((node): node is HTMLTextAreaElement => node instanceof HTMLTextAreaElement)
    const composer = pickComposer(found, document.activeElement)
    if (composer !== null && draftHasLanded(composer.value, expected)) {
      const end = composer.value.length
      composer.focus({ preventScroll: true })
      composer.setSelectionRange(end, end)
      return
    }
    if (left > 0) requestAnimationFrame(() => { attempt(left - 1) })
  }
  requestAnimationFrame(() => { attempt(attempts) })
}
