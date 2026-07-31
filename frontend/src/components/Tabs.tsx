import { useRef } from "react";

export interface TabDef {
  id: string;
  label: string;
  /** Optional qualifier shown beside the label -- a count of what the panel
   *  holds, or a word for what it is. Omitted when there is nothing useful to
   *  say, rather than shown as a zero. */
  hint?: string;
}

interface Props {
  tabs: TabDef[];
  active: string;
  onChange: (id: string) => void;
  /** Distinguishes the generated ids when more than one strip is on the page. */
  idPrefix: string;
}

/**
 * A tab strip.
 *
 * Presentational only -- the parent owns the active id, which lets it live in
 * the URL rather than in component state that a remount would lose.
 *
 * Arrow keys move between tabs and select as they go, which is the expected
 * behaviour for tabs whose panels are cheap to render: the content is already
 * in the query cache, so there is nothing to wait for.
 */
export function Tabs({ tabs, active, onChange, idPrefix }: Props) {
  const ref = useRef<HTMLDivElement>(null);

  const onKeyDown = (e: React.KeyboardEvent) => {
    const i = tabs.findIndex((t) => t.id === active);
    if (i < 0) return;

    let next: number;
    switch (e.key) {
      case "ArrowLeft":
        next = (i - 1 + tabs.length) % tabs.length;
        break;
      case "ArrowRight":
        next = (i + 1) % tabs.length;
        break;
      case "Home":
        next = 0;
        break;
      case "End":
        next = tabs.length - 1;
        break;
      default:
        return;
    }

    e.preventDefault();
    onChange(tabs[next].id);
    // Selection follows focus, so focus has to follow selection too -- other-
    // wise the next arrow press would start from wherever focus was left.
    ref.current
      ?.querySelector<HTMLButtonElement>(`#${idPrefix}-tab-${tabs[next].id}`)
      ?.focus();
  };

  return (
    <div className="tabs" role="tablist" ref={ref} onKeyDown={onKeyDown}>
      {tabs.map((t) => {
        const selected = t.id === active;
        return (
          <button
            key={t.id}
            type="button"
            role="tab"
            id={`${idPrefix}-tab-${t.id}`}
            className={`tab${selected ? " active" : ""}`}
            aria-selected={selected}
            aria-controls={`${idPrefix}-panel-${t.id}`}
            // Roving tabindex: the whole strip is one stop in the tab order,
            // and the arrow keys move within it.
            tabIndex={selected ? 0 : -1}
            onClick={() => onChange(t.id)}
          >
            {t.label}
            {t.hint && <span className="tab-hint">{t.hint}</span>}
          </button>
        );
      })}
    </div>
  );
}

/** The panel a {@link Tabs} strip controls. Ids must match the strip's prefix. */
export function TabPanel({
  id,
  idPrefix,
  children,
}: {
  id: string;
  idPrefix: string;
  children: React.ReactNode;
}) {
  return (
    <div
      role="tabpanel"
      id={`${idPrefix}-panel-${id}`}
      aria-labelledby={`${idPrefix}-tab-${id}`}
      tabIndex={0}
    >
      {children}
    </div>
  );
}
