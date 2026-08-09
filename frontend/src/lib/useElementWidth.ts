import { useEffect, useRef, useState } from "react";

/**
 * Live pixel width of a DOM element, via ResizeObserver.
 *
 * Nothing in this codebase measures a specific container's width today --
 * the existing responsive logic (useIsMobile) is viewport-width-based via
 * matchMedia, which is the wrong signal when the thing that needs to react
 * is squeezed by sibling content rather than by the window itself.
 */
export function useElementWidth<T extends HTMLElement>(): [
  React.RefObject<T | null>,
  number,
] {
  const ref = useRef<T | null>(null);
  const [width, setWidth] = useState(0);

  useEffect(() => {
    const el = ref.current;
    if (!el || typeof ResizeObserver === "undefined") return;

    const observer = new ResizeObserver((entries) => {
      const entry = entries[0];
      if (entry) setWidth(entry.contentRect.width);
    });
    observer.observe(el);
    setWidth(el.getBoundingClientRect().width);

    return () => observer.disconnect();
  }, []);

  return [ref, width];
}
