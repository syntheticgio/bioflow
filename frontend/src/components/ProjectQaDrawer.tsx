import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import type { ConversationTurn } from "../api/types";

/**
 * A project-scoped chat drawer, structurally modeled on QueuePanel: a
 * click-away backdrop plus a positioned panel. Anchored to slide up from the
 * bottom rather than QueuePanel's corner popover, and with a minimize
 * control distinct from close -- close ends the session (clears open state),
 * minimize collapses to a small pill while a still-running
 * answer_project_question job keeps working underneath it.
 *
 * The model's only way to "see" project data is the same search/jobs tool
 * calls the UI itself uses -- never free-text retrieval, never an answer
 * generated from training data alone. See
 * docs/superpowers/specs/2026-08-05-project-qa-chat-design.md.
 */
export function ProjectQaDrawer({
  projectId,
  onClose,
}: {
  projectId: string;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const [minimized, setMinimized] = useState(false);
  const [question, setQuestion] = useState("");
  // Tracks a question this drawer itself submitted, so "thinking..." shows
  // only for an answer we are actually waiting on -- not forever, if the
  // qa.answered event is ever missed and the user reopens later.
  const [pendingQuestion, setPendingQuestion] = useState<string | null>(null);
  const bodyRef = useRef<HTMLDivElement>(null);

  const { data } = useQuery({
    queryKey: ["project-conversation", projectId],
    queryFn: () => api.getProjectConversation(projectId),
  });

  // Polled only while a question is in flight, and only to notice the job's
  // own terminal state -- a skipped question (no provider configured, or
  // the model failed) never produces an assistant turn, so watching turns
  // alone leaves "Thinking..." stuck forever with nothing to clear it.
  const [pendingJobId, setPendingJobId] = useState<string | null>(null);
  const { data: pendingJob } = useQuery({
    queryKey: ["job", pendingJobId],
    queryFn: () => api.getJob(pendingJobId as string),
    enabled: pendingJobId !== null,
    refetchInterval: 1000,
  });

  const ask = useMutation({
    mutationFn: (q: string) => api.askProjectQuestion(projectId, q),
    // `variables` (the question text passed to .mutate()) rather than the
    // component's `question` state -- submit() clears that state
    // immediately on send, so by the time this callback runs it would
    // always read back "" and render an empty turn (found by asking a real
    // question and inspecting the DOM: a .qa-turn-user with no text).
    onSuccess: (response, variables) => {
      setPendingJobId(response.job_id);
      // Optimistic: the user's own question should appear immediately,
      // before the job even starts, so the drawer never looks unresponsive
      // waiting on a queue slot.
      qc.setQueryData<{ turns: ConversationTurn[]; compacted_summary: string | null }>(
        ["project-conversation", projectId],
        (prev) => ({
          turns: [...(prev?.turns ?? []), { role: "user" as const, content: variables }],
          compacted_summary: prev?.compacted_summary ?? null,
        }),
      );
    },
  });

  const clear = useMutation({
    mutationFn: () => api.clearProjectConversation(projectId),
    onSuccess: () => {
      qc.setQueryData(["project-conversation", projectId], { turns: [], compacted_summary: null });
      // Otherwise a pending "Thinking..." from a question asked just before
      // clearing has nothing left to match against and never clears itself
      // (found by clicking through the drawer manually against a real
      // no_provider skip, where the assistant turn that would have cleared
      // it never arrives).
      setPendingQuestion(null);
      setPendingJobId(null);
    },
  });

  const turns = data?.turns ?? [];

  // The pending flag clears on whichever comes first: a matching assistant
  // turn (the normal case, via the qa.answered-triggered refetch), or the
  // job itself reaching a terminal state with no such turn (a skip -- no
  // provider configured, or the model failed -- which by design never
  // produces one). Without the second half, a skip leaves "Thinking..."
  // stuck forever, since nothing else would ever clear it.
  useEffect(() => {
    if (!pendingQuestion) return;
    const lastAssistant = [...turns].reverse().find((t) => t.role === "assistant");
    const lastUser = [...turns].reverse().find((t) => t.role === "user");
    if (lastUser?.content === pendingQuestion && lastAssistant) {
      setPendingQuestion(null);
      setPendingJobId(null);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [turns]);

  useEffect(() => {
    if (!pendingJob) return;
    const terminal = ["succeeded", "failed", "cancelled", "dead"];
    if (terminal.includes(pendingJob.state)) {
      setPendingQuestion(null);
      setPendingJobId(null);
    }
  }, [pendingJob]);

  useEffect(() => {
    bodyRef.current?.scrollTo({ top: bodyRef.current.scrollHeight });
  }, [turns.length, pendingQuestion]);

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    const q = question.trim();
    if (!q) return;
    setPendingQuestion(q);
    setQuestion("");
    ask.mutate(q);
  };

  if (minimized) {
    return (
      <button
        type="button"
        className="qa-pill"
        onClick={() => setMinimized(false)}
        title="Reopen project chat"
      >
        💬 Project chat
        {pendingQuestion && <span className="qa-pill-dot" />}
      </button>
    );
  }

  return (
    <>
      <div className="queue-backdrop" onClick={onClose} />
      <div className="qa-drawer">
        <div className="queue-panel-head">
          <span className="panel-title">Ask about this project</span>
          <button
            type="button"
            className="icon-btn"
            onClick={() => clear.mutate()}
            title="Clear chat"
            style={{ marginLeft: "auto" }}
          >
            🗑
          </button>
          <button
            type="button"
            className="icon-btn"
            onClick={() => setMinimized(true)}
            title="Minimize"
          >
            –
          </button>
          <button type="button" className="icon-btn" onClick={onClose} title="Close">
            ×
          </button>
        </div>

        <div className="qa-drawer-body" ref={bodyRef}>
          {turns.length === 0 && !pendingQuestion ? (
            <div className="queue-empty">
              Ask a question about this project's files or jobs. Answers come only
              from searching the actual data here, never from guessing.
            </div>
          ) : (
            <>
              {turns.map((t, i) => (
                <div key={i} className={`qa-turn qa-turn-${t.role}`}>
                  {t.content}
                </div>
              ))}
              {pendingQuestion && (
                <div className="qa-turn qa-turn-assistant qa-thinking">Thinking…</div>
              )}
            </>
          )}
        </div>

        <form className="qa-drawer-input" onSubmit={submit}>
          <input
            type="text"
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            placeholder="How many BAM files do I have?"
            autoFocus
          />
          <button type="submit" disabled={!question.trim()}>
            Ask
          </button>
        </form>
      </div>
    </>
  );
}
