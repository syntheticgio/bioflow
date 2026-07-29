import { useEffect, useRef, useState } from "react";

export interface MenuItem {
  label: string;
  onSelect: () => void;
  disabled?: boolean;
}

/** A header dropdown: click to open, Escape or outside-click to close,
 *  arrow keys to move between items.
 *
 *  General rather than File-specific because that behavior *is* the component
 *  — View and Help can adopt it without rework. */
export function Menu({ label, items }: { label: string; items: MenuItem[] }) {
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const root = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (!root.current?.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const choose = (item: MenuItem) => {
    if (item.disabled) return;
    setOpen(false);
    item.onSelect();
  };

  return (
    <div ref={root} style={{ position: "relative", display: "inline-block" }}>
      <button
        type="button"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => {
          setOpen((v) => !v);
          setActive(0);
        }}
      >
        {label}
      </button>

      {open && (
        <div
          role="menu"
          className="menu-dropdown"
          onKeyDown={(e) => {
            if (e.key === "ArrowDown") {
              e.preventDefault();
              setActive((i) => (i + 1) % items.length);
            } else if (e.key === "ArrowUp") {
              e.preventDefault();
              setActive((i) => (i - 1 + items.length) % items.length);
            } else if (e.key === "Enter" && items[active]) {
              e.preventDefault();
              choose(items[active]);
            }
          }}
        >
          {items.length === 0 ? (
            <div className="menu-empty">Nothing here yet</div>
          ) : (
            items.map((item, i) => (
              <button
                key={item.label}
                type="button"
                role="menuitem"
                className={i === active ? "menu-item active" : "menu-item"}
                disabled={item.disabled}
                autoFocus={i === 0}
                onMouseEnter={() => setActive(i)}
                onClick={() => choose(item)}
              >
                {item.label}
              </button>
            ))
          )}
        </div>
      )}
    </div>
  );
}
