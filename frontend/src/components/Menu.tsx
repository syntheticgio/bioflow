import { useEffect, useRef, useState } from "react";

export interface MenuItem {
  label: string;
  onSelect: () => void;
  disabled?: boolean;
  /** Group heading this item falls under. Items without one render in an
   *  unheaded leading group, so callers with no sections keep working
   *  unchanged. */
  section?: string;
}

/** A header dropdown: click to open, Escape or outside-click to close,
 *  arrow keys to move between items.
 *
 *  General rather than File-specific because that behavior *is* the component
 *  — View and Help can adopt it without rework.
 *
 *  Grouping is driven by `item.section`: items are partitioned into groups by
 *  first-seen order, each rendered under its heading, with arrow-key
 *  navigation moving through the flattened list rather than per-group so
 *  Up/Down still just works. */
export function Menu({ label, items }: { label: string; items: MenuItem[] }) {
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const root = useRef<HTMLDivElement>(null);

  const groups: { section: string | undefined; items: MenuItem[] }[] = [];
  for (const item of items) {
    const last = groups[groups.length - 1];
    if (last && last.section === item.section) {
      last.items.push(item);
    } else {
      groups.push({ section: item.section, items: [item] });
    }
  }

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
            (() => {
              let i = -1;
              return groups.map((group) => (
                <div key={group.section ?? `__unheaded_${i}`} className="menu-group">
                  {group.section && (
                    <div className="menu-section-label">{group.section}</div>
                  )}
                  {group.items.map((item) => {
                    i += 1;
                    const idx = i;
                    return (
                      <button
                        key={item.label}
                        type="button"
                        role="menuitem"
                        className={idx === active ? "menu-item active" : "menu-item"}
                        disabled={item.disabled}
                        autoFocus={idx === 0}
                        onMouseEnter={() => setActive(idx)}
                        onClick={() => choose(item)}
                      >
                        {item.label}
                      </button>
                    );
                  })}
                </div>
              ));
            })()
          )}
        </div>
      )}
    </div>
  );
}
