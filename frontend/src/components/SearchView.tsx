import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { api } from "../api/client";
import { formatBytes, formatKindLabel } from "../lib/format";
import { BulkEditBar } from "./BulkEditBar";

/**
 * Search results in the left panel.
 *
 * The whole query lives in the URL, so a search is shareable, survives reload,
 * and browser-back returns to the previous filter set rather than dumping the
 * user at the root.
 */
export function SearchView() {
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [lastClicked, setLastClicked] = useState<string | null>(null);

  const q = params.get("q") ?? "";
  const kinds = params.getAll("kind");
  const tags = params.getAll("tag");
  const metas = params.getAll("meta");
  const sel = params.get("sel");
  // Present when the search was opened from inside a project; scopes both the
  // results and the facet counts so the filters reflect what is actually there.
  const projectId = params.get("project_id") ?? undefined;

  const { data, isLoading } = useQuery({
    queryKey: ["search", q, kinds, tags, metas, projectId],
    queryFn: () =>
      api.searchObjects({ q, kind: kinds, tag: tags, meta: metas, projectId }),
  });

  const { data: project } = useQuery({
    queryKey: ["project", projectId],
    queryFn: () => api.getProject(projectId!),
    enabled: !!projectId,
  });

  const { data: facets } = useQuery({
    queryKey: ["search", "facets", projectId],
    queryFn: () => api.searchFacets(projectId),
    staleTime: 30_000,
  });

  const update = (mutate: (p: URLSearchParams) => void) => {
    const next = new URLSearchParams(params);
    mutate(next);
    setParams(next, { replace: true });
  };

  const toggleMulti = (key: string, value: string) =>
    update((p) => {
      const existing = p.getAll(key);
      p.delete(key);
      const next = existing.includes(value)
        ? existing.filter((v) => v !== value)
        : [...existing, value];
      for (const v of next) p.append(key, v);
    });

  const objects = data?.objects ?? [];

  const clickRow = (id: string, e: React.MouseEvent) => {
    // Shift-click extends the selection, matching every file browser.
    if (e.shiftKey && lastClicked) {
      const ids = objects.map((o) => o.id);
      const a = ids.indexOf(lastClicked);
      const b = ids.indexOf(id);
      if (a >= 0 && b >= 0) {
        const range = ids.slice(Math.min(a, b), Math.max(a, b) + 1);
        setSelected(new Set([...selected, ...range]));
        return;
      }
    }
    if (e.metaKey || e.ctrlKey) {
      const next = new Set(selected);
      next.has(id) ? next.delete(id) : next.add(id);
      setSelected(next);
      setLastClicked(id);
      return;
    }
    setSelected(new Set());
    setLastClicked(id);
    update((p) => p.set("sel", `object:${id}`));
  };

  return (
    <div className="panel panel-left">
      <div className="panel-header" style={{ flexWrap: "wrap", gap: 6 }}>
        <button
          type="button"
          onClick={() => navigate(projectId ? `/p/${projectId}` : "/")}
          style={{ color: "var(--accent)", fontSize: 13 }}
        >
          ‹ {project?.name ?? "Projects"}
        </button>
        <span className="panel-title">
          {projectId ? "Search in project" : "Search all files"}
        </span>
      </div>

      <div style={{ padding: "8px 12px", borderBottom: "1px solid var(--border)" }}>
        <input
          value={q}
          autoFocus
          placeholder="Search filenames…"
          onChange={(e) =>
            update((p) => {
              const v = e.target.value;
              v ? p.set("q", v) : p.delete("q");
            })
          }
          style={{ width: "100%", marginBottom: 8 }}
        />

        <MetadataFilterInput
          active={metas}
          onAdd={(f) => update((p) => p.append("meta", f))}
          onRemove={(f) =>
            update((p) => {
              const rest = p.getAll("meta").filter((m) => m !== f);
              p.delete("meta");
              for (const m of rest) p.append("meta", m);
            })
          }
          keys={facets?.metadata_keys.map((k) => k.key) ?? []}
        />

        {(facets?.formats.length ?? 0) > 0 && (
          <FilterRow
            label="Format"
            values={facets!.formats.map((f) => ({
              value: f.value,
              label: `${formatKindLabel(f.value)} (${f.count})`,
            }))}
            active={kinds}
            onToggle={(v) => toggleMulti("kind", v)}
          />
        )}

        {(facets?.tags.length ?? 0) > 0 && (
          <FilterRow
            label="Tags"
            values={facets!.tags.map((t) => ({
              value: t.value,
              label: `${t.value} (${t.count})`,
            }))}
            active={tags}
            onToggle={(v) => toggleMulti("tag", v)}
          />
        )}
      </div>

      {selected.size > 0 && (
        <BulkEditBar
          objectIds={[...selected]}
          onDone={() => setSelected(new Set())}
        />
      )}

      <div className="panel-body">
        <div
          style={{
            padding: "6px 12px",
            fontSize: 11,
            color: "var(--text-faint)",
            display: "flex",
            gap: 8,
          }}
        >
          <span>
            {isLoading ? "Searching…" : `${data?.total ?? 0} result(s)`}
            {data?.has_more && " (showing first 100)"}
          </span>
          {objects.length > 0 && (
            <button
              type="button"
              onClick={() =>
                setSelected(
                  selected.size === objects.length
                    ? new Set()
                    : new Set(objects.map((o) => o.id)),
                )
              }
              style={{ marginLeft: "auto", color: "var(--accent)", fontSize: 11 }}
            >
              {selected.size === objects.length ? "clear" : "select all"}
            </button>
          )}
        </div>

        {!isLoading && objects.length === 0 && (
          <div className="empty">
            <div className="empty-title">No matches</div>
            <div>Try removing a filter.</div>
          </div>
        )}

        {objects.map((o) => (
          <div
            key={o.id}
            className={`row ${sel === `object:${o.id}` ? "selected" : ""}`}
            style={
              selected.has(o.id)
                ? { background: "color-mix(in srgb, var(--accent) 14%, transparent)" }
                : undefined
            }
            onClick={(e) => clickRow(o.id, e)}
          >
            <input
              type="checkbox"
              checked={selected.has(o.id)}
              onClick={(e) => e.stopPropagation()}
              onChange={() => {
                const next = new Set(selected);
                next.has(o.id) ? next.delete(o.id) : next.add(o.id);
                setSelected(next);
                setLastClicked(o.id);
              }}
              style={{ marginRight: 2 }}
            />
            <div className="row-main">
              <div className="row-name">{o.name}</div>
              <div className="row-sub">
                <span>{formatBytes(o.size)}</span>
                {o.format.kind !== "unknown" && (
                  <span>{formatKindLabel(o.format.kind)}</span>
                )}
                {o.tags.slice(0, 2).map((t) => (
                  <span key={t}>#{t}</span>
                ))}
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function FilterRow({
  label,
  values,
  active,
  onToggle,
}: {
  label: string;
  values: { value: string; label: string }[];
  active: string[];
  onToggle: (v: string) => void;
}) {
  return (
    <div style={{ marginTop: 6 }}>
      <div style={{ fontSize: 10, color: "var(--text-faint)", marginBottom: 3 }}>
        {label}
      </div>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
        {values.map((v) => (
          <button
            key={v.value}
            type="button"
            onClick={() => onToggle(v.value)}
            className="chip"
            style={
              active.includes(v.value)
                ? { background: "var(--accent)", color: "#fff" }
                : { cursor: "pointer" }
            }
          >
            {v.label}
          </button>
        ))}
      </div>
    </div>
  );
}

function MetadataFilterInput({
  active,
  onAdd,
  onRemove,
  keys,
}: {
  active: string[];
  onAdd: (f: string) => void;
  onRemove: (f: string) => void;
  keys: string[];
}) {
  const [draft, setDraft] = useState("");

  return (
    <div style={{ marginTop: 4 }}>
      <div style={{ display: "flex", gap: 6 }}>
        <input
          value={draft}
          placeholder="metadata filter, e.g. sample_id=P-041"
          list="metadata-keys"
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && draft.trim()) {
              onAdd(draft.trim());
              setDraft("");
            }
          }}
          style={{ flex: 1, fontSize: 12, padding: "4px 6px" }}
        />
        <datalist id="metadata-keys">
          {keys.map((k) => (
            <option key={k} value={`${k}=`} />
          ))}
        </datalist>
      </div>
      <div style={{ fontSize: 10, color: "var(--text-faint)", marginTop: 2 }}>
        Supports = != &gt;= &lt;= ~ and key=* for "has any value"
      </div>
      {active.length > 0 && (
        <div style={{ display: "flex", flexWrap: "wrap", gap: 4, marginTop: 5 }}>
          {active.map((f) => (
            <span key={f} className="chip" style={{ background: "var(--accent)", color: "#fff" }}>
              {f}
              <button
                type="button"
                onClick={() => onRemove(f)}
                style={{ marginLeft: 4, color: "#fff", fontSize: 11 }}
              >
                ×
              </button>
            </span>
          ))}
        </div>
      )}
    </div>
  );
}
