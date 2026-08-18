import { useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { infoFor, type MetricInfo } from "../lib/metricInfo";

/**
 * The circled "i" beside a reported number, and the card it opens.
 *
 * Every figure this app reports is a measurement someone might put in a
 * methods section, and several of them are not what their label suggests --
 * "Mean quality" is the mean of the per-position means, not the mean over
 * every base, and both come from a 200k-read sample rather than the file.
 * A reader cannot tell that from the number. This is where that gets said.
 *
 * Opens three ways because a tooltip that only opens on hover is decoration
 * on a touch screen and unreachable from the keyboard:
 *
 * - hover, for a mouse
 * - focus, because it is a real <button> in the tab order
 * - click, which *pins* the card open so it survives the pointer leaving --
 *   needed on touch, and for reading a long description without the cursor
 *   drifting off the marker
 *
 * The card is portalled to <body> and positioned from a measured rect rather
 * than being a positioned child. Its natural home -- inside a facts table row
 * or a chart's header -- is inside `overflow` that would clip it.
 */
export function InfoMarker({
  metric,
  info,
  className,
}: {
  /** Registry key. Renders nothing when it has no entry -- see metricInfo. */
  metric?: string;
  /** An explicit card, for the rare thing with no stable registry key. */
  info?: MetricInfo;
  className?: string;
}) {
  const resolved = info ?? (metric ? infoFor(metric) : undefined);
  const [open, setOpen] = useState(false);
  // Distinct from `open`: a pinned card ignores pointer-leave and closes only
  // on Escape, an outside click, or another click on the marker itself.
  const [pinned, setPinned] = useState(false);
  const btnRef = useRef<HTMLButtonElement>(null);
  const cardRef = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState<{ top: number; left: number } | null>(null);
  const cardId = useId();

  // Measured after paint, when the card has a real size to flip against.
  useLayoutEffect(() => {
    if (!open || !btnRef.current || !cardRef.current) return;
    const marker = btnRef.current.getBoundingClientRect();
    const card = cardRef.current.getBoundingClientRect();
    const M = 8;

    // A marker scrolled out of view has no position worth pointing at, and
    // the clamp below would otherwise park its card mid-screen attached to
    // nothing. This happens for real: the detail panel scrolls inside its own
    // container, so a focused marker can leave the viewport without the card
    // ever being told. Close instead of placing it.
    if (marker.bottom < 0 || marker.top > window.innerHeight) {
      setOpen(false);
      setPinned(false);
      return;
    }

    // Prefer below-and-left-aligned, the direction with the most room in a
    // facts table. Flip rather than clamp where there is a choice: a card
    // pinned to the viewport edge covers the row it is describing.
    let top = marker.bottom + 6;
    if (top + card.height > window.innerHeight - M) {
      top = marker.top - card.height - 6;
    }
    let left = marker.left;
    if (left + card.width > window.innerWidth - M) {
      left = window.innerWidth - card.width - M;
    }

    // Then clamp both axes regardless of which way it flipped. The marker's
    // own rect is not guaranteed to be on screen: the detail panel scrolls
    // inside its own container, so a marker can sit below the fold while the
    // page itself has not scrolled. Positioning relative to it alone then
    // puts the card off-screen entirely -- measured at top 723 against a
    // 720px viewport, for a marker whose own top was 981.
    top = Math.min(
      Math.max(M, top),
      Math.max(M, window.innerHeight - card.height - M),
    );
    left = Math.min(
      Math.max(M, left),
      Math.max(M, window.innerWidth - card.width - M),
    );
    setPos({ top, left });
  }, [open]);

  useEffect(() => {
    if (!open) return;

    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      setOpen(false);
      setPinned(false);
      // Focus goes back to the marker rather than to <body>: Escape from a
      // card opened by keyboard should leave the caret where it started.
      btnRef.current?.focus();
    };
    const onDown = (e: MouseEvent) => {
      const t = e.target as Node;
      if (btnRef.current?.contains(t) || cardRef.current?.contains(t)) return;
      setOpen(false);
      setPinned(false);
    };
    // A pinned card measured against the old scroll position points at the
    // wrong row, and there is nothing useful to re-anchor to mid-scroll.
    const onScrollOrResize = () => {
      setOpen(false);
      setPinned(false);
    };

    document.addEventListener("keydown", onKey);
    document.addEventListener("mousedown", onDown);
    window.addEventListener("scroll", onScrollOrResize, true);
    window.addEventListener("resize", onScrollOrResize);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("mousedown", onDown);
      window.removeEventListener("scroll", onScrollOrResize, true);
      window.removeEventListener("resize", onScrollOrResize);
    };
  }, [open]);

  // A marker with nothing to say is worse than no marker: it promises an
  // explanation and then shows an empty card. Coverage is enforced by the
  // exhaustiveness test over the registry, not by rendering a placeholder.
  if (!resolved) return null;

  return (
    <>
      <button
        ref={btnRef}
        type="button"
        className={`info-marker${className ? ` ${className}` : ""}`}
        // The accessible name carries the term, so a screen reader announces
        // "About Lowest position quality" rather than a bare "info".
        aria-label={`About ${resolved.term}`}
        aria-expanded={open}
        aria-describedby={open ? cardId : undefined}
        onMouseEnter={() => setOpen(true)}
        onMouseLeave={() => !pinned && setOpen(false)}
        onFocus={() => setOpen(true)}
        onBlur={() => !pinned && setOpen(false)}
        onClick={() => {
          if (pinned) {
            setPinned(false);
            setOpen(false);
          } else {
            setPinned(true);
            setOpen(true);
          }
        }}
      >
        <svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true">
          <circle
            cx="8"
            cy="8"
            r="6.75"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.3"
          />
          <circle cx="8" cy="4.9" r="0.85" fill="currentColor" />
          <path
            d="M8 7.1v4.4"
            stroke="currentColor"
            strokeWidth="1.3"
            strokeLinecap="round"
          />
        </svg>
      </button>

      {open &&
        createPortal(
          <div
            ref={cardRef}
            id={cardId}
            role="tooltip"
            className="info-card"
            style={{
              top: pos?.top ?? 0,
              left: pos?.left ?? 0,
              // Hidden for the first paint only, while the rect is measured.
              // Without this the card is visibly drawn at 0,0 and then jumps.
              visibility: pos ? "visible" : "hidden",
            }}
          >
            <div className="info-card-term">{resolved.term}</div>
            <p className="info-card-body">{resolved.description}</p>
            {resolved.computed && (
              <p className="info-card-computed">{resolved.computed}</p>
            )}
            {resolved.learnMore && (
              <a className="info-card-more" href={resolved.learnMore}>
                Learn more →
              </a>
            )}
          </div>,
          document.body,
        )}
    </>
  );
}
