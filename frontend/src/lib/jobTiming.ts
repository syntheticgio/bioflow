import { formatDuration } from "./format";
import type { TimingEstimate } from "../api/types";

/**
 * Compute the timing label for a running job.
 *
 * Prefers handler-reported progress over prediction when both exist, and
 * visibly marks predicted values as estimated. Degrades to elapsed-only when
 * no estimate is known. Keeps the "longer than expected" state for jobs that
 * have run past their predicted duration.
 */
export function computeTimingLabel(
  elapsedMs: number,
  reportedPct: number,
  timingEstimate?: TimingEstimate | null,
): { label: string; isEstimated: boolean } {
  const hasReported = reportedPct > 0;

  if (hasReported) {
    return { label: "", isEstimated: false };
  }

  const pctEstimated = timingEstimate?.known
    ? Math.min(1, elapsedMs / timingEstimate.estimate_ms!)
    : null;

  if (pctEstimated != null) {
    const predictedMs = timingEstimate!.estimate_ms!;
    const remainMs = Math.max(0, predictedMs - elapsedMs);

    if (elapsedMs >= predictedMs * 0.95) {
      return { label: "longer than expected", isEstimated: true };
    }
    if (remainMs > 1000) {
      return {
        label: `~${formatDuration(remainMs)} remaining (estimated)`,
        isEstimated: true,
      };
    }
    return { label: "finishing… (estimated)", isEstimated: true };
  }

  return { label: "", isEstimated: false };
}

/**
 * Render the "no estimate yet" subtext below a progress bar.
 */
export function estimateSubtext(
  timingEstimate?: TimingEstimate | null,
): string | null {
  if (!timingEstimate) return null;
  if (!timingEstimate.known) {
    const needed = timingEstimate.needed ?? 1;
    return `no estimate yet (${needed} more run${needed === 1 ? "" : "s"} needed)`;
  }
  if (timingEstimate.r_squared != null && timingEstimate.r_squared < 0.5) {
    return "rough estimate, timings vary";
  }
  return null;
}
