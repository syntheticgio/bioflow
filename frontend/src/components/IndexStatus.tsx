import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import { notify } from "../stores/messageStore";
import type { AlignerName, DataObject } from "../api/types";

/**
 * Keys in a reference's index status that are not aligner indexes. The
 * samtools `.fai` rides along in the same map but has its own row below and
 * no "build" button, since every path that needs it makes one.
 */
const NON_ALIGNER_INDEXES = new Set(["fai"]);

/**
 * Which indexes a reference has, and a way to build the ones it does not.
 *
 * Indexes are filtered out of the explorer listing -- a bwa-mem2 index is five
 * files, and a few references would bury the files a user works with. They are
 * still real objects with real verification and garbage collection; this is
 * where they surface, attached to the reference they belong to.
 */
export function IndexStatus({ object }: { object: DataObject }) {
  const qc = useQueryClient();

  const { data, isLoading } = useQuery({
    queryKey: ["pipelines", "references", object.project_id],
    queryFn: () => api.references(object.project_id),
    // An index build takes a while and produces no event this panel sees, so
    // poll gently rather than leaving a stale "not built" on screen.
    refetchInterval: 15_000,
  });

  const { data: tools } = useQuery({
    queryKey: ["pipelines", "tools"],
    queryFn: api.pipelineTools,
    staleTime: 60_000,
  });

  const build = useMutation({
    mutationFn: (aligner: AlignerName) =>
      api.buildIndex({ reference_id: object.id, aligner }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["jobs"] });
      notify.success("Building the index");
    },
    onError: (e: Error) => notify.error(e.message),
  });

  const entry = data?.references.find((r) => r.object_id === object.id);
  if (isLoading || !entry) return null;

  // The backend keys this map over every Aligner member, so reading the keys
  // back means a newly registered aligner shows up here without a frontend
  // edit -- which the previous hardcoded pair did not, and it sat two
  // aligners behind the backend for three additions.
  const aligners = Object.keys(entry.indexes).filter(
    (name) => !NON_ALIGNER_INDEXES.has(name),
  ) as AlignerName[];

  return (
    <div className="section">
      <div className="section-title">Indexes</div>
      <div className="index-rows">
        {aligners.map((name) => {
          const built = entry.indexes[name] === true;
          const tool = tools?.tools.find((t) => t.name === name);
          const usable = tool?.available !== false;
          return (
            <div key={name} className="index-row">
              <span className="index-name">{name}</span>
              <span className={built ? "index-built" : "index-missing"}>
                {built ? "✓ built" : usable ? "not built" : "unavailable here"}
              </span>
              {!built && usable && (
                <button
                  type="button"
                  className="btn"
                  style={{ padding: "1px 8px", fontSize: 11 }}
                  onClick={() => build.mutate(name)}
                  disabled={build.isPending}
                >
                  Build index
                </button>
              )}
            </div>
          );
        })}
        <div className="index-row">
          <span className="index-name">samtools .fai</span>
          <span className={entry.indexes.fai ? "index-built" : "index-missing"}>
            {entry.indexes.fai ? "✓ built" : "not built"}
          </span>
        </div>
      </div>
      <p className="index-note">
        Built once and reused. Deleting this reference deletes its indexes —
        nothing that cannot be rebuilt from it.
      </p>
    </div>
  );
}
