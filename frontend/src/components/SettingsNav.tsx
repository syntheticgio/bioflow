import { Link, useLocation } from "react-router-dom";

/**
 * The section rail Settings did not have until Tools existed alongside AI.
 *
 * A plain nav rather than a `SettingsView`-owned tab state, because the two
 * pages are otherwise unrelated -- AI's own selection state (which provider is
 * open) has nothing to do with Tools', and forcing them through one parent's
 * `useState` would couple two screens that only share a shell. Routing already
 * *is* the state that matters here: which page is open.
 */
export function SettingsNav() {
  const { pathname } = useLocation();

  const items = [
    { to: "/settings/ai", label: "AI" },
    { to: "/settings/tools", label: "Tools" },
    { to: "/settings/mcp", label: "MCP" },
  ];

  // `/settings` with no section renders the AI page, so it counts as AI being
  // active -- otherwise landing on the bare path shows no item selected.
  const active =
    items.find((i) => pathname.startsWith(i.to))?.to ?? "/settings/ai";

  return (
    <nav className="settings-section-nav">
      {items.map((item) => (
        <Link
          key={item.to}
          to={item.to}
          className={`settings-section-nav-item${
            active === item.to ? " active" : ""
          }`}
        >
          {item.label}
        </Link>
      ))}
    </nav>
  );
}
