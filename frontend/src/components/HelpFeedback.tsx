import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { api, ApiRequestError } from "../api/client";
import type { FeedbackSubmission } from "../api/types";

const CONTACT_MAX_LENGTH = 200;
const SUBJECT_MAX_LENGTH = 200;
const COMMENT_MAX_LENGTH = 2000;

/**
 * A feedback form under Help.
 *
 * Submitting persists the feedback and pushes a Discord notification to the
 * team. The form resets on success; there is no on-page list anymore --
 * notifications go to the configured channel, not to a self-read log.
 */
export function HelpFeedback() {
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
      } as FeedbackSubmission),
    onSuccess: () => {
      setContact("");
      setSubject("");
      setComment("");
      setError(null);
    },
    onError: (err) => {
      setError(err instanceof ApiRequestError ? err.message : "Could not submit feedback");
    },
  });

  const canSubmit =
    contact.trim().length > 0 &&
    subject.trim().length > 0 &&
    comment.trim().length > 0 &&
    !submit.isPending;

  return (
    <div className="help-page">
      <h1>Feedback</h1>
      <p className="help-intro">
        Report a bug, request something, or just say what's working. Your
        submission is saved and forwarded to the team via Discord -- you should
        hear back if you included a way to reach you.
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
    </div>
  );
}
