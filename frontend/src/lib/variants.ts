/**
 * Which variants a protein structure could say anything about.
 *
 * A structure view answers "where in the protein does this land, and what is
 * around it". That question only exists for a variant that changes a residue,
 * which is a narrower set than "has an amino-acid position":
 * `bcftools csq` gives synonymous variants an `aa_pos` too, and they are the
 * largest annotated class in the real yeast callset (2,173 of 4,060). Opening
 * a structure for one would show an unchanged protein.
 */

/**
 * Consequence types that change the protein sequence.
 *
 * An allow-list rather than "anything except synonymous". bcftools emits
 * types this app has never seen -- and compound forms like
 * `stop_lost&frameshift` -- so the open question is what to do with an
 * unrecognised one. Excluding it means a missing button on something that
 * might deserve one; including it means offering a structure view of a
 * variant that may not touch the protein at all. The first is a gap the user
 * can work around, the second is the app asserting something wrong, so
 * unknown types are excluded.
 *
 * Measured against the real callset, this covers 1,782 of the 3,955 variants
 * carrying a residue position.
 */
const RESIDUE_CHANGING = new Set([
  "missense",
  "frameshift",
  "stop_gained",
  "stop_lost",
  "start_lost",
  "inframe_deletion",
  "inframe_insertion",
]);

/**
 * Whether a consequence changes the protein sequence.
 *
 * Compound consequences (`stop_lost&frameshift`) count when any component
 * does: bcftools joins them with `&`, and such a variant changes the protein
 * by every component that named it.
 */
export function isResidueChanging(consequence: string | null): boolean {
  if (!consequence) return false;
  return consequence
    .split("&")
    .some((part) => RESIDUE_CHANGING.has(part.trim()));
}

/**
 * Whether a row can offer a structure view at all.
 *
 * Gene and residue are both required: the gene is what UniProt is asked
 * about, and the residue is what picks between two proteins sharing a symbol.
 * Without either there is nothing to resolve, and the button is not rendered
 * rather than rendered disabled -- a disabled control invites a click and
 * then explains why it was pointless.
 */
export function canShowStructure(row: {
  gene: string | null;
  consequence: string | null;
  aa_pos: number | null;
}): boolean {
  return (
    !!row.gene && row.aa_pos != null && isResidueChanging(row.consequence)
  );
}
