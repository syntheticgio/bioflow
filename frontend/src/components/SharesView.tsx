import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import { BioIcon } from "../icons/BioIcon";
import { formatBytes, formatDate } from "../lib/format";
import { notify } from "../stores/messageStore";
import { useProfileStore } from "../stores/profileStore";
import type { Share } from "../api/types";

/** One inbox row: sender, filename, optional message, accept/decline.
 *
 * Accept takes an optional destination project -- omitted, the backend lands
 * the copy in a lazily created "Shared with me" project. */
function InboxRow({ share }: { share: Share }) {
  const qc = useQueryClient();
  const { data: projects } = useQuery({
    queryKey: ["projects"],
    queryFn: () => api.listProjects(),
  });
  const [projectId, setProjectId] = useState("");

  const accept = useMutation({
    mutationFn: () => api.acceptShare(share.id, projectId || undefined),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["shares"] });
      qc.invalidateQueries({ queryKey: ["objects"] });
      qc.invalidateQueries({ queryKey: ["projects"] });
      notify.success(`Accepted “${share.name}”`);
    },
    onError: (e: Error) => notify.error(e.message),
  });

  const decline = useMutation({
    mutationFn: () => api.declineShare(share.id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["shares"] });
      notify.success(`Declined “${share.name}”`);
    },
    onError: (e: Error) => notify.error(e.message),
  });

  return (
    <div className="share-row">
      <div className="share-row-main">
        <span title={share.from_profile.username}>
          <BioIcon name="user" size={16} />
        </span>
        <strong>{share.from_profile.username}</strong>
        <span>wants to share</span>
        <strong>{share.name}</strong>
        <span className="share-row-size">{formatBytes(share.size)}</span>
      </div>
      {share.message && <div className="share-row-message">“{share.message}”</div>}
      <div className="share-row-actions">
        <select value={projectId} onChange={(e) => setProjectId(e.target.value)}>
          <option value="">Shared with me (default)</option>
          {(projects ?? []).map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
        </select>
        <button
          type="button"
          className="btn primary"
          onClick={() => accept.mutate()}
          disabled={accept.isPending || decline.isPending}
        >
          {accept.isPending ? "Accepting…" : "Accept"}
        </button>
        <button
          type="button"
          className="btn"
          onClick={() => decline.mutate()}
          disabled={accept.isPending || decline.isPending}
        >
          {decline.isPending ? "Declining…" : "Decline"}
        </button>
      </div>
    </div>
  );
}

/** One outbox row: recipient, filename, state, and Withdraw only while
 *  pending -- an accepted share can only 409 on revoke, so the button must
 *  not render for it. */
function OutboxRow({ share }: { share: Share }) {
  const qc = useQueryClient();

  const revoke = useMutation({
    mutationFn: () => api.revokeShare(share.id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["shares"] });
      notify.success(`Withdrew the offer of “${share.name}”`);
    },
    onError: (e: Error) => notify.error(e.message),
  });

  return (
    <div className="share-row">
      <div className="share-row-main">
        <span title={share.to_profile.username}>
          <BioIcon name="user" size={16} />
        </span>
        <strong>{share.to_profile.username}</strong>
        <strong>{share.name}</strong>
        <span className={`share-state share-state-${share.state}`}>{share.state}</span>
        <span className="share-row-size">{formatDate(share.created_at)}</span>
      </div>
      {share.state === "offered" && (
        <div className="share-row-actions">
          <button
            type="button"
            className="btn danger"
            onClick={() => revoke.mutate()}
            disabled={revoke.isPending}
          >
            {revoke.isPending ? "Withdrawing…" : "Withdraw"}
          </button>
        </div>
      )}
      {share.state === "accepted" && (
        <div className="share-row-note">Accepted. It now belongs to their library.</div>
      )}
    </div>
  );
}

/**
 * Sharing's own page rather than a header dropdown: it needs more room than a
 * menu gives, it survives a reload, and it hangs off the profile menu the
 * same way /settings does.
 *
 * Query keys carry the profile id so switching profiles does not serve the
 * previous profile's offers from cache before the refetch lands.
 */
export function SharesView() {
  const profileId = useProfileStore((s) => s.current?.id);

  const { data: inbox = [] } = useQuery({
    queryKey: ["shares", "inbox", profileId],
    queryFn: api.shareInbox,
  });

  const { data: outbox = [] } = useQuery({
    queryKey: ["shares", "outbox", profileId],
    queryFn: api.shareOutbox,
  });

  return (
    <div className="shares-view">
      <h1>Sharing</h1>

      <section>
        <h2>Shared with me</h2>
        {inbox.length === 0 ? (
          <p className="share-empty">No pending offers.</p>
        ) : (
          inbox.map((s) => <InboxRow key={s.id} share={s} />)
        )}
      </section>

      <section>
        <h2>Shared by me</h2>
        {outbox.length === 0 ? (
          <p className="share-empty">You haven't shared anything yet.</p>
        ) : (
          outbox.map((s) => <OutboxRow key={s.id} share={s} />)
        )}
      </section>
    </div>
  );
}
