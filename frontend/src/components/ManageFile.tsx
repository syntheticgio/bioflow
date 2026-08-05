import { useState } from "react";
import { api } from "../api/client";
import { compressionLabel, formatBytes } from "../lib/format";
import type { ObjectDetail as ObjectDetailData } from "../api/types";
import { canPair, PairEditor } from "./PairEditor";
import { canConvertRole, RoleConverter } from "./RoleConverter";
import { ShareFileModal } from "./ShareFileModal";
import { TagEditor } from "./TagEditor";

interface Props {
  obj: ObjectDetailData;
  confirmingDelete: boolean;
  setConfirmingDelete: (v: boolean) => void;
  remove: { mutate: () => void; isPending: boolean };
  onTagsChanged: () => void;
  metadataDirty: boolean;
}

/**
 * The operations that change the record rather than describe it.
 *
 * A label/control grid rather than a stack of sections: these are five small,
 * unrelated controls, and giving each one a full-width bordered section made
 * the tab read as five equally weighty steps when it is really a drawer of
 * odds and ends.
 */
export function ManageFile({
  obj,
  confirmingDelete,
  setConfirmingDelete,
  remove,
  onTagsChanged,
  metadataDirty,
}: Props) {
  // Nothing to serve until the bytes have actually landed, and a blob the
  // verifier has marked missing (an unmounted drive, most often) would only
  // produce a failed download -- so the button says why instead of lying.
  const hasContent = Boolean(obj.blob && obj.blob_sha256);
  const contentMissing = obj.blob?.state === "missing";
  const downloadable = hasContent && !contentMissing;

  // RoleConverter and PairEditor each self-suppress on files they do not
  // apply to. In a stack that is invisible; in a static grid it would strand
  // the label, so the pairing row asks the component's own question first.
  const showPairing = canPair(obj);
  const showRole = canConvertRole(obj);

  // Mirrors the backend's own precondition (offer_share raises 409 on a
  // non-READY object), so the button is absent rather than present-and-failing.
  const shareable = obj.status === "ready" && Boolean(obj.blob_sha256);
  const [sharing, setSharing] = useState(false);

  return (
    <div className="section">
      <div className="section-title">Manage this file</div>

      <div className="manage-grid">
        <div className="manage-label">Download</div>
        <div>
          {downloadable ? (
            <>
              <a
                className="btn"
                href={api.objectDownloadUrl(obj.id)}
                // Named for the user's filename, not the digest. The attribute
                // only applies same-origin, which this is; the server sets
                // Content-Disposition regardless, so the name survives either
                // way.
                download={obj.name}
              >
                Download file
              </a>
              <div className="manage-note">
                The original file as stored
                {obj.blob?.size != null && <> · {formatBytes(obj.blob.size)}</>}
                {/* compressionLabel returns null for "none", so an
                    uncompressed file simply says nothing here. */}
                {compressionLabel(obj.format.compression) && (
                  <>
                    {" "}
                    · still {compressionLabel(obj.format.compression)}-compressed
                  </>
                )}
              </div>
            </>
          ) : (
            <div style={{ color: "var(--text-faint)", fontSize: 12 }}>
              {contentMissing
                ? "The stored file is not currently available. If it lives on an external drive, check that the drive is mounted."
                : "No stored content to download yet."}
            </div>
          )}
        </div>

        {/* Paired end sits opposite Download so that at four columns the two
            read as one row. It is conditional, so at narrower widths the grid
            simply closes up. */}
        {showPairing && (
          <>
            <div className="manage-label">Paired end</div>
            <div>
              <PairEditor obj={obj} bare />
            </div>
          </>
        )}

        <div className="manage-label">Tags</div>
        <div>
          <TagEditor objectId={obj.id} tags={obj.tags} onChanged={onTagsChanged} />
        </div>

        {shareable && (
          <>
            <div className="manage-label">Share</div>
            <div>
              <button type="button" className="btn" onClick={() => setSharing(true)}>
                Share with another profile
              </button>
            </div>
          </>
        )}

        <div className="manage-label">Delete</div>
        <div>
          {!confirmingDelete ? (
            <>
              <button
                type="button"
                className="btn danger"
                onClick={() => setConfirmingDelete(true)}
              >
                Delete file
              </button>
              <div className="manage-note">
                {obj.blob?.storage === "external"
                  ? "Removes this entry. The original file on disk is left untouched."
                  : (obj.blob?.ref_count ?? 0) > 1
                    ? `Removes this entry. ${obj.blob!.ref_count - 1} other file(s) share the same content, so the stored data is kept.`
                    : "Removes this entry. The stored data is reclaimed later by garbage collection."}
              </div>
            </>
          ) : (
            <div className="error-box" style={{ marginBottom: 0 }}>
              <div style={{ marginBottom: 8 }}>
                Delete <strong>{obj.name}</strong>? This cannot be undone.
              </div>
              <div style={{ display: "flex", gap: 8 }}>
                <button
                  type="button"
                  className="btn danger"
                  onClick={() => remove.mutate()}
                  disabled={remove.isPending}
                >
                  {remove.isPending ? "Deleting…" : "Yes, delete"}
                </button>
                <button
                  type="button"
                  className="btn"
                  onClick={() => setConfirmingDelete(false)}
                  disabled={remove.isPending}
                >
                  Cancel
                </button>
              </div>
            </div>
          )}
        </div>

        {/* Guarded like the pairing row above rather than merely placed last:
            RoleConverter renders null on a BAM or VCF, and position alone
            still leaves the label sitting over nothing. */}
        {showRole && (
          <>
            <div className="manage-label">Role</div>
            <div>
              <RoleConverter obj={obj} metadataDirty={metadataDirty} bare />
            </div>
          </>
        )}
      </div>

      {sharing && (
        <ShareFileModal
          objectId={obj.id}
          objectName={obj.name}
          isExternal={obj.blob?.storage === "external"}
          onClose={() => setSharing(false)}
        />
      )}
    </div>
  );
}
