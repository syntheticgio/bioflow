import type { AiProvider } from "../api/types";

/** The left rail: providers, then the routing entry. Selection is lifted to
 *  SettingsView because the detail pane is its sibling, not its child. */
export function ProviderList({
  providers,
  selected,
  onSelect,
  onAdd,
}: {
  providers: AiProvider[];
  selected: string;
  onSelect: (id: string) => void;
  onAdd: () => void;
}) {
  return (
    <nav className="settings-rail">
      {providers.map((p) => (
        <button
          key={p.id}
          className={`settings-rail-item${selected === p.id ? " active" : ""}`}
          onClick={() => onSelect(p.id)}
        >
          <span className="settings-rail-name">{p.name}</span>
          <StatusDot status={p.status} />
        </button>
      ))}

      <button className="settings-rail-item settings-rail-add" onClick={onAdd}>
        + Add provider
      </button>

      <button
        className={`settings-rail-item settings-rail-routing${
          selected === "routing" ? " active" : ""
        }`}
        onClick={() => onSelect("routing")}
      >
        Task routing
      </button>
    </nav>
  );
}

/** Colour only, with a title for the reason -- the detail pane carries the
 *  words. A rail crowded with status text is harder to scan than one dot. */
function StatusDot({ status }: { status: AiProvider["status"] }) {
  const title =
    status === "ok" ? "Working" : status === "failed" ? "Failed" : "Not tested yet";
  return <span className={`settings-dot settings-dot-${status}`} title={title} />;
}
