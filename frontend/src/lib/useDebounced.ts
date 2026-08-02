import { useEffect, useState } from "react";

/** Debounces a fast-changing value so a text field can drive a query without
 *  firing one request per keystroke. */
export function useDebounced<T>(value: T, delayMs: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const id = setTimeout(() => setDebounced(value), delayMs);
    return () => clearTimeout(id);
  }, [value, delayMs]);
  return debounced;
}
