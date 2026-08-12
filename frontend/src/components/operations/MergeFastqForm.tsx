import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../../api/client";
import { formatBytes } from "../../lib/format";
import { notify } from "../../stores/messageStore";

interface MergeFastqFormProps {
  projectId: string;
  onBack: () => void;
}

export function MergeFastqForm({ projectId, onBack }: MergeFastqFormProps) {
  const qc = useQueryClient();
  const { data: objects } = useQuery({
    queryKey: ["objects", projectId],
    queryFn: () => api.listObjects(projectId),
  });

  const fastqFiles = (objects ?? []).filter(
    (o) => o.format.kind === "fastq" && o.status === "ready"
  );

  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [outputName, setOutputName] = useState("");

  const merge = useMutation({
    mutationFn: () =>
      api.mergeFastq(projectId, [...selected], outputName.trim() || "merged.fastq.gz"),
    onSuccess: (result) => {
      notify.success(`Merge job launched: ${result.job_id}`);
      qc.invalidateQueries({ queryKey: ["objects", projectId] });
      onBack();
    },
    onError: (e: Error) => notify.error(e.message),
  });

  const toggleFile = (id: string) => {
    const next = new Set(selected);
    next.has(id) ? next.delete(id) : next.add(id);
    setSelected(next);
  };

  const canMerge = selected.size >= 2 && outputName.trim().length > 0 && !merge.isPending;

  return (
    <div className="panel">
      <div className="panel-header">
        <button type="button" className="btn-text" onClick={onBack}>
          ← Back to project
        </button>
        <span className="panel-title">Merge FASTQ files</span>
      </div>
      <div className="panel-body detail">
        <p style={{ marginBottom: 16, color: "var(--text-muted)" }}>
          Select two or more FASTQ files to concatenate into a single file.
          Files are merged in the order they are selected.
        </p>

        <div style={{ marginBottom: 16 }}>
          <label
            style={{
              display: "block",
              fontSize: 12,
              fontWeight: 600,
              marginBottom: 4,
            }}
          >
            Output filename
          </label>
          <input
            type="text"
            value={outputName}
            onChange={(e) => setOutputName(e.target.value)}
            placeholder="merged.fastq.gz"
            style={{ width: "100%", padding: "6px 8px" }}
          />
        </div>

        <div style={{ marginBottom: 12 }}>
          <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 8 }}>
            Select FASTQ files ({selected.size} selected)
          </div>
          {fastqFiles.length === 0 && (
            <p style={{ color: "var(--text-faint)" }}>No FASTQ files in this project.</p>
          )}
          <div
            style={{
              maxHeight: 300,
              overflowY: "auto",
              border: "1px solid var(--border)",
              borderRadius: 4,
            }}
          >
            {fastqFiles.map((file) => (
              <label
                key={file.id}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 8,
                  padding: "6px 8px",
                  cursor: "pointer",
                  background: selected.has(file.id)
                    ? "color-mix(in srgb, var(--accent) 10%, transparent)"
                    : undefined,
                }}
              >
                <input
                  type="checkbox"
                  checked={selected.has(file.id)}
                  onChange={() => toggleFile(file.id)}
                />
                <div style={{ flex: 1 }}>
                  <div style={{ fontSize: 13 }}>{file.name}</div>
                  <div style={{ fontSize: 11, color: "var(--text-faint)" }}>
                    {formatBytes(file.size)}
                  </div>
                </div>
              </label>
            ))}
          </div>
        </div>

        <button
          type="button"
          className="btn primary"
          disabled={!canMerge}
          onClick={() => merge.mutate()}
        >
          {merge.isPending ? "Merging…" : "Merge files"}
        </button>
      </div>
    </div>
  );
}
