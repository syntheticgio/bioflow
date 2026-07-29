import type { DataObject } from "../api/types";

/** Where a file sits in a mate pair, for the connector drawn across the two rows. */
export type PairPosition = "first" | "second" | null;

export interface OrderedFile {
  object: DataObject;
  /** "first" is the top half of a pair, "second" the bottom. Null when unpaired. */
  pair: PairPosition;
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
