import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiRequestError } from "../api/client";
import type { Feedback } from "../api/types";

const CONTACT_MAX_LENGTH = 200;
const SUBJECT_MAX_LENGTH = 200;
const COMMENT_MAX_LENGTH = 2000;

function formatTimestamp(iso: string): string {
  return new Date(iso).toLocaleString();
}

function FeedbackEntry({ item }: { item: Feedback }) {
  return (
    <article className="software-entry">
      <div className="software-entry-head">
        <h3 className="software-name">{item.subject}</h3>
      </div>
      <div className="software-entry-body">
        <div className="software-prose">
          <p>{item.comment}</p>
        </div>
        <div className="software-facts">
          <div className="software-fact">
            <span className="software-fact-label">From</span>
            <span className="software-fact-value">{item.contact}</span>
          </div>
          <div className="software-fact">
            <span className="software-fact-label">Submitted</span>
            <span className="software-fact-value">{formatTimestamp(item.created_at)}</span>
          </div>
        </div>
      </div>
    </article>
  );
}

/**
 * A feedback form under Help, and the list of what's already been submitted.
 *
 * Write-only in the sense that nothing reads it back into a workflow -- there
 * is no email or ticketing behind this, just a `feedback` collection someone
 * can query directly. The list exists so the form isn't a hole things vanish
 * into: submitting shows your entry appear below, same as everyone else's.
 */
export function HelpFeedback() {
  const queryClient = useQueryClient();
  const { data, isLoading, isError } = useQuery({
    queryKey: ["feedback"],
    queryFn: api.listFeedback,
  });

  const [contact, setContact] = useState("");
  const [subject, setSubject] = useState("");
  const [comment, setComment] = useState("");
  const [error, setError] = useState<string | null>(null);

  const submit = useMutation({
    mutationFn: () =>
      api.submitFeedback({
        contact: contact.trim(),
        subject: subject.trim(),
        comment: comment.trim(),
      }),
    onSuccess: () => {
      setContact("");
      setSubject("");
      setComment("");
      setError(null);
      queryClient.invalidateQueries({ queryKey: ["feedback"] });
    },
    onError: (err) => {
      setError(err instanceof ApiRequestError ? err.message : "Could not submit feedback");
    },
  });

  const canSubmit = contact.trim() && subject.trim() && comment.trim() && !submit.isPending;

  return (
    <div className="help-page">
      <h1>Feedback</h1>
      <p className="help-intro">
        Report a bug, request something, or just say what's working. This
        saves straight to BioFlow's database — there's no email or ticket
        system behind it yet, so nobody is notified when you submit.
      </p>

      <form
        className="feedback-form"
        onSubmit={(e) => {
          e.preventDefault();
          if (canSubmit) submit.mutate();
        }}
      >
        <label htmlFor="fb-contact">Contact</label>
        <input
          id="fb-contact"
          value={contact}
          onChange={(e) => setContact(e.target.value)}
          maxLength={CONTACT_MAX_LENGTH}
          placeholder="Email, or however you'd like to be reached"
        />

        <label htmlFor="fb-subject">Subject</label>
        <input
          id="fb-subject"
          value={subject}
          onChange={(e) => setSubject(e.target.value)}
          maxLength={SUBJECT_MAX_LENGTH}
          placeholder="Short summary"
        />

        <label htmlFor="fb-comment">Comment</label>
        <textarea
          id="fb-comment"
          value={comment}
          onChange={(e) => setComment(e.target.value)}
          maxLength={COMMENT_MAX_LENGTH}
          rows={6}
          placeholder="What happened, or what you'd like to see"
        />
        <p className="feedback-char-count">
          {comment.length} / {COMMENT_MAX_LENGTH}
        </p>

        {error && <div className="error-box">{error}</div>}

        <div className="feedback-form-actions">
          <button type="submit" className="btn primary" disabled={!canSubmit}>
            {submit.isPending ? "Submitting…" : "Submit feedback"}
          </button>
        </div>
      </form>

      <h2 className="software-group-title">Previous submissions</h2>

      {isLoading && <p className="software-note">Loading feedback…</p>}
      {isError && <p className="software-note">Could not reach the server to list feedback.</p>}
      {data && data.length === 0 && (
        <p className="software-note">Nothing submitted yet.</p>
      )}

      {data?.map((item) => (
        <FeedbackEntry key={item.id} item={item} />
      ))}
    </div>
  );
}
