import type { DataObject } from "../api/types";

/** Where a file sits in a mate pair, for the connector drawn across the two rows. */
export type PairPosition = "first" | "second" | null;

export interface OrderedFile {
  object: DataObject;
  /** "first" is the top half of a pair, "second" the bottom. Null when unpaired. */
  pair: PairPosition;
}

/** A run of files rendered together: either one unpaired file, or a mate pair
 *  drawn as a labelled unit. */
export interface FileGroup {
  /** Stable key for React, and the pair's identity. */
  key: string;
  files: DataObject[];
  /** The pair's shared name stem, e.g. "sample" for sample_R1/sample_R2.
   *  Null for unpaired files, which render as plain rows. */
  pairLabel: string | null;
}

/**
 * The name both mates share, with the read-number marker removed.
 *
 * Derived from the pair rather than stored: the mate link is the source of
 * truth about what pairs with what, and a stem parsed out of one filename
 * would disagree with it the moment a file is renamed. Falls back to the
 * common prefix when the usual _R1/_R2 convention is absent, and to null when
 * that leaves nothing meaningful -- the label is decoration, and a wrong or
 * empty one is worse than none.
 */
export function pairStem(a: string, b: string): string | null {
  const strip = (n: string) =>
    n
      // Drop extensions first (.fastq.gz, .fq), then the read marker, which
      // sits at the end of the stem: sample_R1.fastq.gz -> sample.
      .replace(/\.(fastq|fq|fasta|fa)(\.gz)?$/i, "")
      .replace(/[._-]?R?[12]$/i, "");

  const sa = strip(a);
  const sb = strip(b);
  if (sa && sa === sb) return sa;

  // No shared convention: fall back to the literal common prefix, trimmed of
  // any separator it happens to end on.
  let i = 0;
  while (i < sa.length && i < sb.length && sa[i] === sb[i]) i++;
  const prefix = sa.slice(0, i).replace(/[._-]+$/, "");
  return prefix.length >= 2 ? prefix : null;
}

/** Collapse an ordered list into render groups, so a pair can be drawn as one
 *  labelled unit rather than two rows that happen to be adjacent. */
export function groupPairs(ordered: OrderedFile[]): FileGroup[] {
  const groups: FileGroup[] = [];

  for (let i = 0; i < ordered.length; i++) {
    const entry = ordered[i];
    const next = ordered[i + 1];

    if (entry.pair === "first" && next?.pair === "second") {
      groups.push({
        key: entry.object.id,
        files: [entry.object, next.object],
        pairLabel: pairStem(entry.object.name, next.object.name),
      });
      i++; // consumed the mate
      continue;
    }

    // Includes a "first" whose mate is missing from this list -- orderWithPairs
    // already declines to mark those, but guarding here keeps the two
    // functions independently correct.
    groups.push({ key: entry.object.id, files: [entry.object], pairLabel: null });
  }

  return groups;
}

/**
 * Order a category's files so mates sit adjacent, R1 above R2.
 *
 * The spine can only be drawn between neighbouring rows, so ordering is a
 * prerequisite for the visual rather than a cosmetic choice.
 *
 * Pairs sort by the name of their first member, so a pair stays where its name
 * puts it instead of being hoisted above the unpaired files.
 */
export function orderWithPairs(files: DataObject[]): OrderedFile[] {
  const byId = new Map(files.map((f) => [f.id, f]));
  const consumed = new Set<string>();
  const units: { sortKey: string; entries: OrderedFile[] }[] = [];

  for (const file of files) {
    if (consumed.has(file.id)) continue;

    // A self-referential pointer would emit the same file twice, duplicate
    // React keys and all. `_link_mate` cannot produce one, but the planned
    // manual-tagging feature writes this field directly, so it is guarded here
    // rather than trusted.
    const mateId = file.mate_object_id === file.id ? null : file.mate_object_id;
    const mate = mateId ? byId.get(mateId) : undefined;

    // Unpaired, or half of a pair whose other side is not in this list --
    // deleted, or living in another project. Rendering a spine to nothing
    // would be worse than rendering none, so it reads as a plain file.
    if (!mate) {
      units.push({ sortKey: file.name, entries: [{ object: file, pair: null }] });
      continue;
    }

    consumed.add(file.id);
    consumed.add(mate.id);

    // R1 on top. When neither side carries a read number -- a pair whose names
    // never had the convention -- name order is the stable fallback.
    let top = file;
    let bottom = mate;
    if (file.read_number != null && mate.read_number != null) {
      if (file.read_number > mate.read_number) [top, bottom] = [mate, file];
    } else if (file.name.localeCompare(mate.name) > 0) {
      [top, bottom] = [mate, file];
    }

    units.push({
      sortKey: top.name,
      entries: [
        { object: top, pair: "first" },
        { object: bottom, pair: "second" },
      ],
    });
  }

  units.sort((a, b) => a.sortKey.localeCompare(b.sortKey));
  return units.flatMap((u) => u.entries);
}
