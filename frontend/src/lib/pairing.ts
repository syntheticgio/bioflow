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

/** One read unit as shown by the stage rail: a raw file (or pair), each
 *  optionally paired with its own current trimmed version. */
export interface StageRailEntry {
  key: string;
  /** Accession or shared name stem shown as the card header. */
  label: string | null;
  paired: boolean;
  /** Raw mates, ordered R1 above R2 (single entry for single-end). */
  raw: DataObject[];
  /** Each raw mate's newest trimmed child, aligned by index with `raw`.
   *  Null where that mate has not been trimmed. */
  trimmed: (DataObject | null)[];
}

/**
 * Every trimmed child derived from `raw`, newest first.
 *
 * A paired trim job records *both* raw mates in `derived_from` on each
 * trimmed output, so this alone cannot tell which mate a given trimmed file
 * corresponds to -- see `matchTrimmedGroup`, which resolves that using the
 * trimmed files' own mate link instead.
 */
function trimmedChildrenOf(raw: DataObject, all: DataObject[]): DataObject[] {
  return all
    .filter((o) => o.role === "trimmed_reads" && o.derived_from.includes(raw.id))
    .sort((a, b) => (a.created_at > b.created_at ? -1 : 1));
}

/**
 * Match each raw file in `rawGroup` (already ordered R1 above R2 by
 * `orderWithPairs`) to its own current trimmed version.
 *
 * The two raw mates' `derived_from` sets are identical on a paired trim
 * output, so a trimmed candidate cannot be attributed to one raw mate over
 * the other by that link alone. What's unambiguous is the trimmed files'
 * *own* pairing: `orderWithPairs` on the candidate pool orders trimmed R1
 * above trimmed R2 exactly as it does for raw files (via mate_object_id and
 * name fallback), and a paired trim job always produces one trimmed mate per
 * raw mate -- so zipping the two same-length, same-convention orderings by
 * index lines up correctly. A raw file with no candidates, or a candidate
 * pool that does not resolve into one trimmed file per raw file (mismatched
 * counts -- e.g. only one mate has been retrimmed since), leaves that raw
 * file's slot null rather than guessing.
 */
function matchTrimmedGroup(rawGroup: DataObject[], all: DataObject[]): (DataObject | null)[] {
  if (rawGroup.length === 1) {
    const [best] = trimmedChildrenOf(rawGroup[0], all);
    return [best ?? null];
  }

  // Every trimmed file reachable from any mate in this pair, deduplicated --
  // both mates' derived_from name the same set, so this is one pool, not two.
  // A re-trimmed pair leaves earlier rounds' outputs in this pool too, so it
  // can hold more than one trimmed pair; group it the same way raw files are
  // grouped and keep only the newest pair (by its later-created mate).
  const poolIds = new Set<string>();
  for (const raw of rawGroup) {
    for (const child of trimmedChildrenOf(raw, all)) poolIds.add(child.id);
  }
  const pool = all.filter((o) => poolIds.has(o.id));
  if (pool.length === 0) return rawGroup.map(() => null);

  const trimmedPairs = groupPairs(orderWithPairs(pool));
  const newestPair = trimmedPairs.reduce((newest, g) => {
    const gNewest = g.files.reduce((a, b) => (a.created_at > b.created_at ? a : b));
    const newestNewest = newest.files.reduce((a, b) =>
      a.created_at > b.created_at ? a : b,
    );
    return gNewest.created_at > newestNewest.created_at ? g : newest;
  });

  // Only a real pair (both mates present) can be attributed to rawGroup's two
  // slots by position; anything else (an orphaned single trimmed file from a
  // partial retrim) is not attributable to one specific mate, so it is
  // dropped rather than guessed at.
  if (newestPair.files.length !== rawGroup.length) return rawGroup.map(() => null);
  return newestPair.files;
}

/**
 * Group a category's files into stage-rail cards: raw reads (paired or
 * single) each carrying their own current trimmed version, if any.
 *
 * Built on top of `orderWithPairs`/`groupPairs` rather than replacing them --
 * the mate-pairing rules (R1 above R2, name fallback) are exactly the same
 * for raw files here as for the flat list elsewhere. Trimmed files never
 * become anchors themselves: they are always reached via a raw file's
 * `derived_from` link, so a trimmed file with no discoverable raw parent
 * (which should not normally happen) simply will not surface here -- the
 * caller falls back to plain rows for anything this function does not place.
 */
export function buildStageRail(files: DataObject[]): StageRailEntry[] {
  const rawFiles = files.filter((o) => o.role !== "trimmed_reads");
  const ordered = orderWithPairs(rawFiles);
  const groups = groupPairs(ordered);

  return groups.map((group) => {
    const first = group.files[0];
    const accession =
      (typeof first?.facts?.sra_accession === "string" && first.facts.sra_accession) ||
      (typeof first?.facts?.sra_downloaded_from === "string" &&
        first.facts.sra_downloaded_from) ||
      null;

    return {
      key: group.key,
      label: accession || group.pairLabel || first?.name || null,
      paired: group.pairLabel !== null,
      raw: group.files,
      trimmed: matchTrimmedGroup(group.files, files),
    };
  });
}
