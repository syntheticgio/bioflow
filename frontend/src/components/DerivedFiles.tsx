import { useQuery } from "@tanstack/react-query";
import { useNavigate, useSearchParams } from "react-router-dom";
import { api } from "../api/client";
import { formatBytes } from "../lib/format";
import type { DataObject } from "../api/types";

/**
 * Provenance in both directions: what this file produced, and what produced it.
 *
 * Lineage is the reason trimmed output is a first-class object rather than a
 * sidecar -- without a way back to the source, a folder of `.trimmed.fastq.gz`
 * files is just files.
 */
export function DerivedFiles({ object }: { object: DataObject }) {
  const [, setParams] = useSearchParams();
  const navigate = useNavigate();

  // Siblings in the project are already fetched for the explorer, so this
  // resolves names without another round trip per related file.
  const { data: siblings = [] } = useQuery({
    queryKey: ["objects", object.project_id],
    queryFn: () => api.listObjects(object.project_id),
  });

  const children = siblings.filter((o) => o.derived_from.includes(object.id));
  const parents = siblings.filter((o) => object.derived_from.includes(o.id));
  const mate = siblings.find((o) => o.id === object.mate_object_id);

  if (children.length === 0 && parents.length === 0 && !mate) return null;

  const select = (id: string) => {
    setParams({ sel: `object:${id}` }, { replace: true });
    navigate(`/p/${object.project_id}?sel=object:${id}`, { replace: true });
  };

  return (
    <div className="section">
      <div className="section-title">Related files</div>

      {mate && (
        <Group
          label={
            object.read_number
              ? `Paired with (this file is R${object.read_number})`
              : "Paired with"
          }
        >
          <FileRow object={mate} onSelect={select} />
        </Group>
      )}

      {parents.length > 0 && (
        <Group label="Derived from">
          {parents.map((p) => (
            <FileRow key={p.id} object={p} onSelect={select} />
          ))}
        </Group>
      )}

      {children.length > 0 && (
        <Group label={children.length === 1 ? "Produced" : `Produced (${children.length})`}>
          {children.map((c) => (
            <FileRow key={c.id} object={c} onSelect={select} />
          ))}
        </Group>
      )}
    </div>
  );
}

function Group({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div style={{ marginBottom: 8 }}>
      <div style={{ fontSize: 11, color: "var(--text-faint)", marginBottom: 3 }}>
        {label}
      </div>
      {children}
    </div>
  );
}

function FileRow({
  object,
  onSelect,
}: {
  object: DataObject;
  onSelect: (id: string) => void;
}) {
  return (
    <button type="button" className="derived-row" onClick={() => onSelect(object.id)}>
      <span className="derived-name">{object.name}</span>
      <span className="derived-meta">
        {formatBytes(object.size)}
        {object.read_number && <span className="chip">R{object.read_number}</span>}
        {object.role === "trimmed_reads" && <span className="chip">trimmed</span>}
      </span>
    </button>
  );
}
