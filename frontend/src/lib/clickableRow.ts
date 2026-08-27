// Keyboard access for rows that are really buttons.
//
// Several primary interaction targets -- the file browser row most of all --
// were plain div/tr elements with an onClick and nothing else: no tabIndex, no
// role, no key handler. They could not be reached or activated by keyboard at
// all, and screen readers did not announce them as interactive (#895).
//
// A helper rather than a component, because these rows are div/tr/li in three
// different files and each already carries its own className, style and drag
// handlers. Spreading the props keeps every one of those untouched.

import type { KeyboardEvent } from "react";

export interface ClickableRowProps {
  role: "button";
  tabIndex: 0;
  onKeyDown: (e: KeyboardEvent) => void;
}

/**
 * The role/tabIndex/onKeyDown trio for a row whose whole body activates.
 *
 * Space is handled as well as Enter, and both call preventDefault: Space would
 * otherwise scroll the panel out from under the row the user just activated,
 * which is the behaviour a real `<button>` suppresses for the same reason.
 *
 * `onActivate` should be the same callback the row's `onClick` uses, so the two
 * paths cannot drift.
 */
export function clickableRow(onActivate: () => void): ClickableRowProps {
  return {
    role: "button",
    tabIndex: 0,
    onKeyDown: (e: KeyboardEvent) => {
      if (isActivationKey(e.key)) {
        e.preventDefault();
        onActivate();
      }
    },
  };
}

/**
 * Whether a key event on a clickable row should activate it.
 *
 * Exported separately so the rule is testable without a DOM -- this repo has no
 * jsdom, so `clickableRow`'s handler cannot be exercised directly.
 */
export function isActivationKey(key: string): boolean {
  return key === "Enter" || key === " ";
}
