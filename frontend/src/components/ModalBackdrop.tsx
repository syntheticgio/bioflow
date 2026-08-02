import type { KeyboardEvent, ReactNode } from "react";
import { createPortal } from "react-dom";

/**
 * The `.modal-backdrop` div, portaled to `document.body`.
 *
 * Every dialog in the app renders this element deep inside whichever panel
 * opened it. That's fine when the opener is the detail panel, but dialogs
 * opened from the project/file tree render inside `.panel-left`, which the
 * Broadsheet theme makes `position: sticky` -- and `position: sticky`
 * establishes a stacking context of its own. A `position: fixed` backdrop
 * still escapes that ancestor's *clipping*, but not its *paint order*: the
 * detail panel's sticky tab strip is a later sibling stacking context, so it
 * painted over the backdrop instead of under it, leaving the tab strip
 * looking un-dimmed while the rest of the page darkened. Portaling to
 * `document.body` puts the backdrop in the root stacking context, the same
 * place every other themed ancestor already resolves to `auto`.
 */
export function ModalBackdrop({
  onClick,
  onKeyDown,
  children,
}: {
  onClick?: () => void;
  onKeyDown?: (e: KeyboardEvent<HTMLDivElement>) => void;
  children: ReactNode;
}) {
  return createPortal(
    <div className="modal-backdrop" onClick={onClick} onKeyDown={onKeyDown}>
      {children}
    </div>,
    document.body,
  );
}
