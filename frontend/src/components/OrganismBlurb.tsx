import { useQuery } from "@tanstack/react-query";

import { api } from "../api/client";

/**
 * A couple of sentences about the species, under the file name.
 *
 * Deliberately the least important thing on the page. It is colour -- context
 * for someone glancing at a file, not data anyone should act on -- and the
 * styling says so: smaller, dimmer, and set apart from the measured facts in
 * the kicker above and the QC numbers below.
 *
 * That framing is load-bearing rather than cosmetic. Unlike the file summary,
 * which restates numbers it was handed, this asks the model to recall facts
 * about a species from its own weights, and a small local model can simply be
 * wrong. The prompt steers hard toward textbook-level claims and away from
 * precise figures (see ORGANISM_SYSTEM_PROMPT), but the honest belt-and-braces
 * answer is to present it as background and never let it look authoritative.
 *
 * Renders nothing at all -- no heading, no empty box, no error -- when the
 * organism is unknown, unrecognized, or no model server is running. A file
 * whose species has no blurb looks exactly as it did before this existed.
 */
export function OrganismBlurb({ organism }: { organism: string | null }) {
  const { data, isLoading } = useQuery({
    // Keyed by species, matching the server's cache: two files of the same
    // organism share one query and one request.
    queryKey: ["organism", organism],
    queryFn: () => api.organismBlurb(organism!),
    enabled: Boolean(organism),
    // The text for a species does not change. Once fetched, keep it for the
    // session rather than refetching each time the panel opens.
    staleTime: Infinity,
    gcTime: Infinity,
    retry: false,
    refetchOnWindowFocus: false,
  });

  if (!organism) return null;

  // A first-time generation takes a few seconds, so the space is held rather
  // than left to pop in and shove the buttons down as the user reaches them.
  if (isLoading) {
    return <div className="organism-blurb is-loading">Looking up {organism}…</div>;
  }

  if (!data?.text) return null;

  return (
    <div className="organism-blurb">
      <p>{data.text}</p>
    </div>
  );
}
