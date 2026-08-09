import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import { useProfileStore } from "../stores/profileStore";
import { notify } from "../stores/messageStore";
import { ModalBackdrop } from "./ModalBackdrop";

interface Props {
  objectId: string;
  objectName: string;
  isExternal: boolean;
  onClose: () => void;
}

/**
 * Offer a file to another profile. Follows `NewProjectModal`'s shape exactly
 * -- one mutation, one form, `notify` on either outcome -- rather than
 * inventing a second modal pattern.
 */
export function ShareFileModal({ objectId, objectName, isExternal, onClose }: Props) {
  const currentProfileId = useProfileStore((s) => s.current?.id);
  const [toProfileId, setToProfileId] = useState("");
  const [message, setMessage] = useState("");
  const qc = useQueryClient();

  // Every profile, minus the current one: the backend 422s a self-share, and
  // offering the option only to reject it is worse than not offering it.
  const { data: profiles } = useQuery({
    queryKey: ["profiles"],
    queryFn: api.listProfiles,
  });
  const recipients = (profiles ?? []).filter((p) => p.id !== currentProfileId);

  const mutation = useMutation({
    mutationFn: () =>
      api.offerShare({
        object_id: objectId,
        to_profile_id: toProfileId,
        message: message.trim() || undefined,
      }),
    onSuccess: (share) => {
      qc.invalidateQueries({ queryKey: ["shares"] });
      notify.success(`Offered “${objectName}” to ${share.to_profile.username}`);
      onClose();
    },
    onError: (e: Error) => notify.error(e.message),
  });

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    if (toProfileId) mutation.mutate();
  };

  return (
    <ModalBackdrop onClick={onClose} onKeyDown={(e) => e.key === "Escape" && onClose()}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <h2>Share “{objectName}”</h2>
        <form onSubmit={submit}>
          <label htmlFor="sf-recipient">Share with</label>
          <select
            id="sf-recipient"
            autoFocus
            value={toProfileId}
            onChange={(e) => setToProfileId(e.target.value)}
          >
            <option value="" disabled>
              Choose a profile…
            </option>
            {recipients.map((p) => (
              <option key={p.id} value={p.id}>
                {p.username}
              </option>
            ))}
          </select>

          <label htmlFor="sf-message">Message</label>
          <textarea
            id="sf-message"
            rows={2}
            value={message}
            onChange={(e) => setMessage(e.target.value)}
            placeholder="Optional"
          />

          {/* This is the one warning worth surfacing before the offer goes
              out: an externally registered file's bytes are not ours to
              guarantee, so the recipient's copy can go missing through no
              action of their own. Same register as ManageFile's own note on
              deleting an external object. */}
          {isExternal && (
            <div className="manage-note" style={{ marginTop: 8 }}>
              This file was registered from an external location. BioFlow does
              not control those bytes, so the recipient's copy could stop
              working if the original file moves or is deleted.
            </div>
          )}

          <div className="modal-actions">
            <button type="button" className="btn" onClick={onClose}>
              Cancel
            </button>
            <button
              type="submit"
              className="btn primary"
              disabled={!toProfileId || mutation.isPending}
            >
              {mutation.isPending ? "Sharing…" : "Share"}
            </button>
          </div>
        </form>
      </div>
    </ModalBackdrop>
  );
}
