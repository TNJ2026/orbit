/** Pure, persisted geometry for the Orbit shell-overlay panel.
 *
 * Kept free of React and of the DOM so the rules can be read and tested as
 * rules. Everything here answers one question — where does the panel sit, and
 * how big is it — for a window whose size the panel does not control.
 */

export type PanelMode = 'docked' | 'floating'

/** What the person arranged, carried between browser sessions. */
export interface PanelLayout {
  readonly mode: PanelMode
  readonly collapsed: boolean
  /**
   * Put away entirely — no panel and no badge.
   *
   * Distinct from `collapsed`, which is the panel folded down to its mark and
   * still watching. This is a person saying they are done with Orbit here: the
   * close button sets it after stopping the Runtime, because a badge still
   * sitting there would be an offer to reopen a page about a service that is
   * no longer running. `/orbit` brings it back.
   */
  readonly dismissed: boolean
  readonly x: number
  readonly y: number
  readonly width: number
  readonly height: number
}

/** The overlay box the panel is placed inside. */
export interface PanelBounds {
  readonly width: number
  readonly height: number
}

export const PANEL_STORAGE_KEY = 'orbit:panel:v1'
export const PANEL_DEFAULT_WIDTH = 400
export const PANEL_DEFAULT_HEIGHT = 420
export const PANEL_MIN_WIDTH = 320
export const PANEL_MAX_WIDTH = 720
export const PANEL_MIN_HEIGHT = 280
export const PANEL_DOCK_TOP = 64
export const PANEL_DOCK_RIGHT = 18
export const PANEL_DOCK_BOTTOM = 24
export const PANEL_FLOAT_MARGIN = 12
/** Below this the overlay is too narrow to float anything; the panel docks. */
export const PANEL_COMPACT_BREAKPOINT = 960

export const DEFAULT_PANEL_LAYOUT: PanelLayout = Object.freeze({
  mode: 'docked',
  collapsed: true,
  dismissed: false,
  x: 0,
  y: PANEL_DOCK_TOP,
  width: PANEL_DEFAULT_WIDTH,
  height: PANEL_DEFAULT_HEIGHT,
})

function clamp(value: number, low: number, high: number): number {
  return value < low ? low : value > high ? high : value
}

function finite(value: unknown, fallback: number): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback
}

/**
 * Read a stored layout, keeping only what is still meaningful.
 *
 * A layout is written by a version of this panel and read by whatever version
 * runs next, so every field is treated as a suggestion: unreadable storage, a
 * renamed mode, or a width from a wider monitor all resolve to something the
 * current window can actually show rather than to a panel nobody can reach.
 */
export function readLayout(raw: string | null): PanelLayout {
  if (!raw) return DEFAULT_PANEL_LAYOUT
  let value: unknown
  try { value = JSON.parse(raw) } catch { return DEFAULT_PANEL_LAYOUT }
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return DEFAULT_PANEL_LAYOUT
  const stored = value as Partial<PanelLayout>
  return {
    mode: stored.mode === 'floating' ? 'floating' : 'docked',
    collapsed: stored.collapsed !== false,
    // Absent means present: a layout written before this existed is one the
    // panel should still appear for, and only an explicit `true` hides it.
    dismissed: stored.dismissed === true,
    x: finite(stored.x, DEFAULT_PANEL_LAYOUT.x),
    y: finite(stored.y, DEFAULT_PANEL_LAYOUT.y),
    width: clamp(finite(stored.width, PANEL_DEFAULT_WIDTH), PANEL_MIN_WIDTH, PANEL_MAX_WIDTH),
    height: Math.max(finite(stored.height, PANEL_DEFAULT_HEIGHT), PANEL_MIN_HEIGHT),
  }
}

/** The CSS box for a layout inside the bounds it has to live in. */
export function placePanel(layout: PanelLayout, bounds: PanelBounds): {
  left: number; top: number; width: number; height: number
} {
  const compact = bounds.width < PANEL_COMPACT_BREAKPOINT
  const maxWidth = Math.max(PANEL_MIN_WIDTH, Math.min(PANEL_MAX_WIDTH, bounds.width - 2 * PANEL_FLOAT_MARGIN))
  const width = compact ? Math.max(PANEL_MIN_WIDTH, bounds.width - 2 * PANEL_FLOAT_MARGIN)
    : clamp(layout.width, PANEL_MIN_WIDTH, maxWidth)
  if (layout.mode === 'docked' || compact) {
    const top = PANEL_DOCK_TOP
    // Docked is a side, not a column: filling to the bottom of the window made
    // a panel of three Runs as tall as the conversation beside it. The height
    // is the arranged one, capped by what the window has.
    const available = Math.max(PANEL_MIN_HEIGHT, bounds.height - top - PANEL_DOCK_BOTTOM)
    return {
      left: Math.max(PANEL_FLOAT_MARGIN, bounds.width - width - PANEL_DOCK_RIGHT),
      top,
      width,
      height: Math.min(Math.max(layout.height, PANEL_MIN_HEIGHT), available),
    }
  }
  const height = clamp(layout.height, PANEL_MIN_HEIGHT, Math.max(PANEL_MIN_HEIGHT, bounds.height - 2 * PANEL_FLOAT_MARGIN))
  return {
    left: clamp(layout.x, PANEL_FLOAT_MARGIN, Math.max(PANEL_FLOAT_MARGIN, bounds.width - width - PANEL_FLOAT_MARGIN)),
    top: clamp(layout.y, PANEL_FLOAT_MARGIN, Math.max(PANEL_FLOAT_MARGIN, bounds.height - height - PANEL_FLOAT_MARGIN)),
    width,
    height,
  }
}

/** Move a floating panel by a drag delta, keeping it inside the overlay. */
export function dragPanel(layout: PanelLayout, dx: number, dy: number, bounds: PanelBounds): PanelLayout {
  const placed = placePanel({ ...layout, mode: 'floating' }, bounds)
  return {
    ...layout,
    mode: 'floating',
    x: clamp(placed.left + dx, PANEL_FLOAT_MARGIN, Math.max(PANEL_FLOAT_MARGIN, bounds.width - placed.width - PANEL_FLOAT_MARGIN)),
    y: clamp(placed.top + dy, PANEL_FLOAT_MARGIN, Math.max(PANEL_FLOAT_MARGIN, bounds.height - placed.height - PANEL_FLOAT_MARGIN)),
  }
}

/** Resize from the panel's left or bottom edge. */
export function resizePanel(layout: PanelLayout, dWidth: number, dHeight: number, bounds: PanelBounds): PanelLayout {
  const maxWidth = Math.max(PANEL_MIN_WIDTH, Math.min(PANEL_MAX_WIDTH, bounds.width - 2 * PANEL_FLOAT_MARGIN))
  return {
    ...layout,
    width: clamp(layout.width + dWidth, PANEL_MIN_WIDTH, maxWidth),
    height: clamp(layout.height + dHeight, PANEL_MIN_HEIGHT, Math.max(PANEL_MIN_HEIGHT, bounds.height - 2 * PANEL_FLOAT_MARGIN)),
  }
}
