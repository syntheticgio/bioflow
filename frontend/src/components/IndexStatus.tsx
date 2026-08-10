import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api/client";
import { notify } from "../stores/messageStore";
import type { AlignerName, DataObject } from "../api/types";

/**
 * Keys in a reference's index status that are not aligner indexes. The
 * samtools `.fai` rides along in the same map but has its own row below and
 * no "build" button, since every path that needs it makes one. `star_annotated`
 * rides along too, but gets its own row nested under STAR's rather than being
 * dropped -- see the dedicated rendering below.
 */
const NON_ALIGNER_INDEXES = new Set(["fai", "star_annotated"]);

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
  // Which annotation to build STAR's index with, chosen once a project has
  // more than one and the ambiguity has to be resolved by hand -- mirrors
  // the refusal `resolve_annotation` raises server-side for the same reason.
  const [starAnnotationId, setStarAnnotationId] = useState<string>("");

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

  // Only STAR has an annotated index, so this project-wide lookup is scoped
  // to appear only once a STAR index (annotated or not) is actually offered
  // below -- see `enabled`, set once `tools` has resolved.
  const { data: annotations } = useQuery({
    queryKey: ["pipelines", "annotations", object.project_id],
    queryFn: () => api.annotations(object.project_id),
    staleTime: 60_000,
    enabled: tools?.tools.some((t) => t.name === "star" && t.available) ?? false,
  });

  const build = useMutation({
    mutationFn: (vars: { aligner: AlignerName; annotationId?: string }) =>
      api.buildIndex({
        reference_id: object.id,
        aligner: vars.aligner,
        annotation_id: vars.annotationId || null,
      }),
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

  const annotationList = annotations?.annotations ?? [];
  const starAnnotated = entry.indexes.star_annotated === true;

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
              {built && entry.index_ids[name] && (
                <a
                  href={api.objectDownloadUrl(entry.index_ids[name])}
                  className="btn"
                  style={{ padding: "1px 8px", fontSize: 11, textDecoration: "none" }}
                  download
                >
                  ↓ Download
                </a>
              )}
              {!built && usable && (
                <button
                  type="button"
                  className="btn"
                  style={{ padding: "1px 8px", fontSize: 11 }}
                  onClick={() => build.mutate({ aligner: name })}
                  disabled={build.isPending}
                >
                  Build index
                </button>
              )}
              {/* STAR only: an index built with --sjdbGTFfile improves
                  splice-junction sensitivity over STAR's own de novo
                  detection, and is a separate sidecar from the plain index
                  above rather than a replacement for it -- see
                  aligners.STAR_ANNOTATED_MEMBERS. Offered only once a GTF is
                  actually available to build against. */}
              {name === "star" && usable && annotationList.length > 0 && (
                <div className="index-row" style={{ paddingLeft: 16 }}>
                  <span className="index-name">star (annotated)</span>
                  <span
                    className={starAnnotated ? "index-built" : "index-missing"}
                  >
                    {starAnnotated ? "✓ built" : "not built"}
                  </span>
                  {!starAnnotated && (
                    <>
                      {annotationList.length > 1 && (
                        <select
                          value={starAnnotationId}
                          onChange={(e) => setStarAnnotationId(e.target.value)}
                          style={{ fontSize: 11 }}
                        >
                          <option value="">choose annotation…</option>
                          {annotationList.map((a) => (
                            <option key={a.object_id} value={a.object_id}>
                              {a.name}
                            </option>
                          ))}
                        </select>
                      )}
                      <button
                        type="button"
                        className="btn"
                        style={{ padding: "1px 8px", fontSize: 11 }}
                        onClick={() =>
                          build.mutate({
                            aligner: "star",
                            annotationId:
                              annotationList.length === 1
                                ? annotationList[0].object_id
                                : starAnnotationId,
                          })
                        }
                        disabled={
                          build.isPending ||
                          (annotationList.length > 1 && !starAnnotationId)
                        }
                      >
                        Build index
                      </button>
                    </>
                  )}
                </div>
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
