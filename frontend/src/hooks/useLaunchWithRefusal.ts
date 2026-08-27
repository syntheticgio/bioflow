import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useNavigate } from "react-router-dom";

import { ApiRequestError } from "../api/client";
import type { ResourceRefusalDetails } from "../api/types";
import { notify } from "../stores/messageStore";

/**
 * The twin launch / launch-anyway mutations every pipeline dialog needs.
 *
 * Roughly ten dialogs each carried their own copy of the same thirty lines:
 * a mutation that launches, an onError that peels a `refusal` out of a 422's
 * details into local state, a second mutation that re-sends the same body with
 * `resource_override: true`, and the ResourceRefusalCard plumbing between them.
 * Every new dialog re-implemented it, and any fix to the refusal flow had to be
 * made ten times (#898).
 *
 * The dialogs differ in three ways and only three: which `api.launch*` method
 * they call, what body they build, and what the success message says. So those
 * are the parameters, and nothing else is -- a hook that grew a flag per caller
 * would be the duplication again with extra steps.
 *
 * `buildBody` is a function rather than a value because a dialog's body is
 * assembled from state that changes while the dialog is open; capturing it at
 * hook-call time would launch with whatever was selected on first render. It
 * receives `override`, so a caller that has to shape the two bodies
 * differently can, while the common case just spreads it.
 */
/**
 * Whether an error is a resource refusal rather than a genuine failure.
 *
 * Exported and pure so the discrimination is testable: getting it wrong in
 * either direction is bad in a specific way. Treating a refusal as an error
 * loses the "launch anyway" escape and dead-ends the user; treating an error
 * as a refusal offers an override that cannot help.
 */
export function refusalFrom(e: unknown): ResourceRefusalDetails | null {
  if (e instanceof ApiRequestError && "refusal" in e.details) {
    return e.details as unknown as ResourceRefusalDetails;
  }
  return null;
}

export interface LaunchWithRefusal {
  /** Fire the ordinary launch. A refusal populates `refusal` rather than erroring. */
  launch: () => void;
  /** Re-send with the resource check waived. */
  launchAnyway: () => void;
  /** The refusal to render in a ResourceRefusalCard, or null. */
  refusal: ResourceRefusalDetails | null;
  /** Clear it -- for a dialog that lets the user change the inputs and retry. */
  clearRefusal: () => void;
  /** The ordinary launch in flight, for the submit control's disabled state. */
  isPending: boolean;
  /** The override in flight, which ResourceRefusalCard shows separately -- the
   *  card's own button must not spin because the *first* launch is running. */
  isAnywayPending: boolean;
}

export function useLaunchWithRefusal<TBody>({
  send,
  buildBody,
  successMessage,
  onLaunched,
}: {
  /** The `api.launch*` call. Given the body this hook built.
   *
   *  Generic in the body so a caller keeps its own request type -- casting to
   *  Record<string, unknown> here would throw away the checking that catches a
   *  misspelled field, which is most of what these typed request shapes are
   *  for. */
  send: (body: TBody) => Promise<unknown>;
  /** Built per click. `override` is true on the launch-anyway path. */
  buildBody: (override: boolean) => TBody;
  /** Shown on a successful ordinary launch. The anyway path has its own,
   *  fixed message, because "launched without the memory check" is the thing
   *  worth saying there whatever the pipeline was. */
  successMessage: string;
  /** Usually the dialog's onClose. Called before navigating to Activity. */
  onLaunched: () => void;
}): LaunchWithRefusal {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const [refusal, setRefusal] = useState<ResourceRefusalDetails | null>(null);

  const succeed = (message: string) => {
    qc.invalidateQueries({ queryKey: ["jobs"] });
    notify.success(message);
    onLaunched();
    navigate("/activity");
  };

  const launch = useMutation({
    mutationFn: () => send(buildBody(false)),
    onSuccess: () => succeed(successMessage),
    onError: (e: Error) => {
      // A refusal is not an error to report -- it is the "launch anyway"
      // escape being offered. Anything else is a real failure.
      const found = refusalFrom(e);
      if (found) {
        setRefusal(found);
        return;
      }
      notify.error(e.message);
    },
  });

  const launchAnyway = useMutation({
    mutationFn: () => send(buildBody(true)),
    onSuccess: () => succeed("Launching without the memory check"),
    // No refusal branch: the override is what a refusal escalates to, so a
    // second refusal here would be a server bug, not a state to offer again.
    onError: (e: Error) => notify.error(e.message),
  });

  return {
    launch: () => launch.mutate(),
    launchAnyway: () => launchAnyway.mutate(),
    refusal,
    clearRefusal: () => setRefusal(null),
    isPending: launch.isPending,
    isAnywayPending: launchAnyway.isPending,
  };
}
