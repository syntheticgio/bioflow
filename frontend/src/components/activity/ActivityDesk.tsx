import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "../../api/client";
import { formatBytes } from "../../lib/format";
import { SectionHead } from "./SectionHead";

/**
 * The standing rail: what the library holds, and what has landed in it lately.
 *
 * Both queries are keys the app already holds -- `["system","stats"]` is the
 * masthead's (`Header.tsx`) and `["system","load"]` is the load indicator's --
 * so react-query serves this column from cache rather than issuing requests of
 * its own. Keep the keys and intervals identical or that stops being true.
 */
export function ActivityDesk() {
  const { data: stats } = useQuery({
    queryKey: ["system", "stats"],
    queryFn: api.systemStats,
    refetchInterval: 15000,
  });

  const { data: load } = useQuery({
    queryKey: ["system", "load"],
    queryFn: api.systemLoad,
    refetchInterval: 5000,
  });

  // Newest first, which is the API's default sort (`-created_at`) and the order
  // that suits an activity page: these are the files the runs above produced.
  const { data: index } = useQuery({
    queryKey: ["activity", "file-index"],
    queryFn: () => api.searchObjects({ limit: 6 }),
    refetchInterval: 30000,
  });

  const queue = stats?.queue;
  const readouts: { k: string; v: string }[] = [];
  if (stats) {
    readouts.push(
      { k: "Files", v: String(stats.counts.objects) },
      { k: "Projects", v: String(stats.counts.projects) },
      { k: "Stored", v: formatBytes(stats.storage.library_bytes) },
    );
  }
  if (load) readouts.push({ k: "CPU", v: `${Math.round(load.cpu.percent)}%` });
  if (queue) {
    readouts.push({ k: "Queued", v: String(queue.ready + queue.delayed) });
  }

  return (
    <aside className="activity-desk">
      <div className="desk-block">
        <SectionHead title="Desk" />
        <div className="desk-readouts">
          {readouts.map((d) => (
            <div key={d.k} className="desk-readout">
              <span className="desk-readout-k">{d.k}</span>
              <span className="desk-readout-v">{d.v}</span>
            </div>
          ))}
        </div>
      </div>

      <div className="desk-block">
        <SectionHead title="File index" />
        {index && index.objects.length === 0 ? (
          <div className="activity-empty">No files yet.</div>
        ) : (
          <div className="desk-files">
            {index?.objects.map((o) => (
              <div key={o.id} className="desk-file">
                <Link
                  className="desk-file-name"
                  to={`/p/${o.project_id}?sel=object:${o.id}`}
                  title={o.name}
                >
                  {o.name}
                </Link>
                <span className="desk-file-size">{formatBytes(o.size)}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </aside>
  );
}
