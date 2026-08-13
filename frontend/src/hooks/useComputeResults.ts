import { useMutation, useQueryClient } from "@tanstack/react-query";
import { notify } from "../stores/messageStore";

/**
 * The compute-results mutation shared by every on-demand Results view.
 *
 * All three launch endpoints take the same (objectId, targetNode?) shape and
 * queue a job, so the only thing that varies between views is which one to
 * call. Invalidating ["jobs"] is what makes the queued job appear without a
 * refresh; the toast is what tells the user the click landed, since the view
 * itself does not change until the job finishes.
 */
export function useComputeResults(
  objectId: string,
  targetNode: string,
  launch: (objectId: string, targetNode?: string) => Promise<unknown>,
) {
  const qc = useQueryClient();

  return useMutation({
    mutationFn: () => launch(objectId, targetNode || undefined),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["jobs"] });
      notify.info("Computing results");
    },
    onError: (e: Error) => notify.error(e.message),
  });
}
