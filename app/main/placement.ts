// Where the menu bar dropdown opens, and when a click on the menu bar icon closes it.
// No Electron imports, so it can be tested.

export interface Box {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface Point {
  x: number;
  y: number;
}

const GAP = 4;
const EDGE = 8;

/**
 * The top-left corner for the dropdown: under the menu bar icon, kept inside the display.
 * In another app's full-screen space the menu bar is hidden and macOS can report an empty
 * icon box; then the dropdown opens under the pointer, which is where the icon was clicked.
 */
export function menuPosition(icon: Box, area: Box, pointer: Point, width: number): Point {
  const known = icon.width > 0 && icon.height > 0;
  const centre = known ? icon.x + icon.width / 2 : pointer.x;
  const top = known ? icon.y + icon.height + GAP : area.y + GAP;
  return {
    x: Math.round(Math.min(Math.max(centre - width / 2, area.x + EDGE), area.x + area.width - width - EDGE)),
    y: Math.round(Math.max(top, area.y + GAP)),
  };
}

/** How long after the dropdown closed itself a click on the icon still counts as "close". */
export const REOPEN_GUARD_MS = 300;

/**
 * Clicking the icon while the dropdown is open first takes focus away from it (so it hides),
 * then delivers the click. Without this the click would open it again straight away.
 */
export function shouldOpen(visible: boolean, hiddenAt: number, now: number): boolean {
  if (visible) return false;
  return now - hiddenAt > REOPEN_GUARD_MS;
}

/** Ignore a "lost focus" that arrives while the window is still being shown. */
export const BLUR_GUARD_MS = 250;
