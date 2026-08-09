import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import { getRecentProjects } from "../lib/recentProjects";
import { useElementWidth } from "../lib/useElementWidth";

// Fixed per-chip budget rather than measuring text: exact glyph-width
// measurement is unwarranted complexity for a cosmetic shortcut list, and a
// fixed max-width is already how each chip's own label truncation works.
const CHIP_WIDTH_PX = 160;
const LABEL_WIDTH_PX = 60; // "RECENT" + its divider, roughly
const MAX_CHIPS = 3;

export function RecentProjects() {
  const [containerRef, availableWidth] = useElementWidth<HTMLDivElement>();

  const { data: projects } = useQuery({
    queryKey: ["projects", null],
    queryFn: () => api.listProjects(),
  });

  const recent = getRecentProjects();
  if (recent.length === 0) return null;

  const knownIds = new Set((projects ?? []).map((p) => p.id));
  const visible = recent.filter((p) => knownIds.has(p.id));
  if (visible.length === 0) return null;

  // Available width is measured on a full-width probe (see Header.tsx),
  // so budget is: total minus the "RECENT" label, divided into chip slots.
  const chipBudget = Math.max(0, availableWidth - LABEL_WIDTH_PX);
  const chipCount = Math.min(
    MAX_CHIPS,
    visible.length,
    Math.floor(chipBudget / CHIP_WIDTH_PX),
  );

  return (
    <div className="recent-projects" ref={containerRef}>
      {chipCount > 0 && (
        <>
          <span className="recent-projects-label">RECENT</span>
          {visible.slice(0, chipCount).map((p) => (
            <Link
              key={p.id}
              to={`/p/${p.id}`}
              className="recent-projects-chip"
              title={p.name}
            >
              {p.name}
            </Link>
          ))}
        </>
      )}
    </div>
  );
}
