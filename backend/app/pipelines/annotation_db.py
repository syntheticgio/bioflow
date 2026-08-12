"""The SQLite database backing the annotation feature table.

The sibling of `variant_db`, for the same reason and with the same shape: a
human GFF3 holds millions of rows, where reading the whole file to slice a
page in Python costs hundreds of MB of RSS per in-flight request, and the
same data in SQLite answers a filtered page in well under a millisecond.

What differs from variant_db is hierarchy. Every row is stored, but the table
pages over *top-level* features (those with no parent) and fetches children
per-parent on expand -- see the spec's paging decision. That keeps LIMIT and
OFFSET meaning the same thing they mean in the variant table.
"""

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from app.logging import get_logger

log = get_logger(__name__)

# Batched inserts, same reasoning as variant_db._INSERT_BATCH.
_INSERT_BATCH = 10_000

# The columns every read returns. Named once so the page query and the
# children query cannot drift apart about the row shape the client parses.
_COLUMNS = (
    "contig, start, end, type, strand, score, name, feature_id, "
    "parent, biotype, attributes, parent_status, depth"
)


@dataclass(frozen=True)
class FeatureFilters:
    """What the table is currently showing.

    One object rather than loose arguments so `query_features` and
    `count_features` cannot drift apart about what is being filtered -- the
    page and its total have to agree or pagination silently misreports.

    `top_level_only` defaults True because the table opens on parents. It is
    a field rather than a hardcoded clause because the type filter has to
    clear it: every exon has a parent, so filtering to `exon` with the flag
    set returns an empty table on a perfectly good GFF3.

    `parent_status` is how the Unresolved view expresses itself -- the rows
    whose parent reference resolved to nothing. It is a tuple rather than a
    single value because that view shows four statuses at once.
    """

    contig: str | None = None
    start_min: int | None = None
    start_max: int | None = None
    feature_type: str | None = None
    biotype: str | None = None
    name_query: str | None = None
    strand: str | None = None
    top_level_only: bool = True
    parent_status: tuple[str, ...] | None = None


def build_annotation_db(*, rows, db_path: Path) -> int:
    """Stream features into an indexed SQLite database.

    `rows` is consumed once and never materialized: at 3M features a list
    would exhaust the container before a row was written.

    Indexes are built *after* the bulk insert -- creating them first makes
    every insert maintain four B-trees. Journaling and synchronous writes are
    off because this file is a derived artifact rebuilt from the annotation on
    demand, so durability buys nothing.

    Returns the number of rows inserted.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.unlink(missing_ok=True)

    con = sqlite3.connect(db_path)
    try:
        con.execute("PRAGMA journal_mode=OFF")
        con.execute("PRAGMA synchronous=OFF")
        con.execute(
            """
            CREATE TABLE features (
              contig     TEXT NOT NULL,
              start      INTEGER NOT NULL,
              end        INTEGER NOT NULL,
              type       TEXT,
              strand     TEXT,
              score      REAL,
              name       TEXT,
              feature_id TEXT,
              parent     TEXT,
              biotype    TEXT,
              attributes TEXT,
              parent_status TEXT NOT NULL DEFAULT 'root',
              depth      INTEGER NOT NULL DEFAULT 0
            )
            """
        )

        # Counts source features, not stored rows. A multi-parent GFF3 exon
        # writes one row per relationship so expanding either parent finds
        # it, but it is one feature -- returning the row count here would
        # inflate the summary's feature total (AH-12).
        features = 0
        batch: list[tuple] = []
        for f in rows:
            features += 1
            # An empty `parents` writes a single row with a NULL parent,
            # which is what makes the row a candidate root.
            for parent in f.parents or (None,):
                batch.append(
                    (
                        f.contig, f.start, f.end, f.type, f.strand, f.score,
                        f.name, f.feature_id, parent, f.biotype, f.attributes,
                    )
                )
            if len(batch) >= _INSERT_BATCH:
                con.executemany(
                    "INSERT INTO features (contig, start, end, type, strand, "
                    "score, name, feature_id, parent, biotype, attributes) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)", batch
                )
                batch = []

        if batch:
            con.executemany(
                "INSERT INTO features (contig, start, end, type, strand, "
                "score, name, feature_id, parent, biotype, attributes) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)", batch
            )

        con.execute("CREATE INDEX ix_features_locus ON features(contig, start)")
        # The index the whole paging design rests on: expanding a gene must
        # be a seek, not a scan of three million rows.
        con.execute("CREATE INDEX ix_features_parent ON features(parent)")
        # Resolution's every UPDATE looks parents up by this column; without
        # it each pass is a full scan.
        con.execute("CREATE INDEX ix_features_feature_id ON features(feature_id)")
        con.execute("CREATE INDEX ix_features_type ON features(type)")
        con.execute("CREATE INDEX ix_features_name ON features(name)")
        con.execute("CREATE INDEX ix_features_status ON features(parent_status)")
        con.commit()
    finally:
        con.close()

    return features


def _where(filters: FeatureFilters) -> tuple[str, list]:
    """The WHERE clause and its bound parameters.

    Every value is bound, never interpolated: these come from query string
    arguments and reach a SQL statement directly.
    """
    clauses: list[str] = []
    args: list = []

    if filters.top_level_only:
        clauses.append("parent IS NULL")
    if filters.parent_status:
        placeholders = ",".join("?" for _ in filters.parent_status)
        clauses.append(f"parent_status IN ({placeholders})")
        args.extend(filters.parent_status)
    if filters.contig:
        clauses.append("contig = ?")
        args.append(filters.contig)
    # Overlap rather than containment: a feature straddling the window's edge
    # is at that locus as far as anyone looking there is concerned.
    if filters.start_max is not None:
        clauses.append("start <= ?")
        args.append(filters.start_max)
    if filters.start_min is not None:
        clauses.append("end >= ?")
        args.append(filters.start_min)
    if filters.feature_type:
        clauses.append("type = ?")
        args.append(filters.feature_type)
    if filters.biotype:
        clauses.append("biotype = ?")
        args.append(filters.biotype)
    if filters.strand:
        clauses.append("strand = ?")
        args.append(filters.strand)
    if filters.name_query:
        # LIKE wildcards in user input are escaped: a search for "%" must
        # find features named "%", not every feature in the file.
        escaped = (
            filters.name_query.replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )
        clauses.append("name LIKE ? ESCAPE '\\'")
        args.append(f"%{escaped}%")

    if not clauses:
        return "", []
    return " WHERE " + " AND ".join(clauses), args


def _connect(db_path: Path) -> sqlite3.Connection:
    """Read-only. Nothing but the compute job ever writes, and SQLite handles
    concurrent readers without coordination."""
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def query_features(
    *, db_path: Path, filters: FeatureFilters, offset: int, limit: int
) -> list[dict]:
    """One page of the table, in file order.

    Ordered by rowid rather than (contig, start): annotation files are written
    in coordinate order, so insertion order already is position order, and an
    explicit ORDER BY would cost a sort on every page. This is the same trade
    `query_variants` documents.

    `has_children` rides along so the client knows whether to draw an expand
    chevron. Computed with an EXISTS subquery against ix_features_parent,
    which is a seek per row rather than a scan.
    """
    where, args = _where(filters)
    con = _connect(db_path)
    try:
        con.row_factory = sqlite3.Row
        cur = con.execute(
            f"SELECT {_COLUMNS}, "
            f"EXISTS(SELECT 1 FROM features c WHERE c.parent = features.feature_id) "
            f"AS has_children "
            f"FROM features{where} LIMIT ? OFFSET ?",
            [*args, limit, offset],
        )
        rows = [dict(r) for r in cur.fetchall()]
    finally:
        con.close()

    for r in rows:
        r["has_children"] = bool(r["has_children"])
    return rows


def children_of(*, db_path: Path, parent_id: str) -> list[dict]:
    """Every child of one feature, in position order.

    Unpaged deliberately: a transcript has tens of exons, not thousands, and
    paging inside an expanded row is complexity with no payoff.
    """
    con = _connect(db_path)
    try:
        con.row_factory = sqlite3.Row
        cur = con.execute(
            f"SELECT {_COLUMNS} FROM features WHERE parent = ? ORDER BY start",
            (parent_id,),
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        con.close()


def count_features(*, db_path: Path, filters: FeatureFilters) -> int:
    """How many rows match. See the route: this is not recomputed on every
    page turn -- a combined predicate is not guaranteed to use a single
    index, the same reasoning variant_db.count_variants documents (there,
    benchmarked at ~400ms/5M rows; not separately benchmarked here)."""
    where, args = _where(filters)
    con = _connect(db_path)
    try:
        return con.execute(f"SELECT COUNT(*) FROM features{where}", args).fetchone()[0]
    finally:
        con.close()


def count_in_window(*, db_path: Path, contig: str, start: int, end: int) -> int:
    """How many top-level features overlap this window.

    Top-level only, because that is what the viewer draws and therefore what
    the density threshold has to be measured against -- counting every exon
    would push a modest gene view over the threshold and show a density band
    where individual genes would have fitted.
    """
    con = _connect(db_path)
    try:
        return con.execute(
            "SELECT COUNT(*) FROM features "
            "WHERE parent IS NULL AND contig = ? AND start <= ? AND end >= ?",
            (contig, end, start),
        ).fetchone()[0]
    finally:
        con.close()


def bin_counts(
    *, db_path: Path, contig: str, start: int, end: int, bins: int
) -> list[int]:
    """Feature counts per equal-width bin across the window.

    Binned in SQL rather than in Python: the GROUP BY rides
    ix_features_locus, so a full-contig query over 200k features returns in
    ~82ms, where fetching every row to count it in Python would allocate all
    of them.

    Features are binned by `start`, so one straddling a bin edge is counted
    once rather than in both. A bin with no features is 0 rather than absent
    -- for feature counts, unlike GC content, "no features here" genuinely is
    zero, and the caller draws a flat band rather than a gap.

    The span is half-open (`end - start`) and `bin_bases` is a *ceiling*, so
    the bin count never exceeds what was asked for and the last bin absorbs
    the remainder. Deriving the width by flooring instead yields one bin more
    than requested whenever the span does not divide evenly -- verified
    against SQLite, where a 0-10000 window at 10 bins produced 11.
    """
    span = max(1, end - start)
    requested = max(1, min(int(bins), span))
    bin_bases = -(-span // requested)
    # Re-derived from the width rather than reused, so width and count cannot
    # disagree about which bin a coordinate falls in.
    n_bins = -(-span // bin_bases)

    con = _connect(db_path)
    try:
        rows = con.execute(
            "SELECT (start - ?) / ? AS bin, COUNT(*) FROM features "
            "WHERE parent IS NULL AND contig = ? AND start <= ? AND end >= ? "
            "GROUP BY bin",
            (start, bin_bases, contig, end, start),
        ).fetchall()
    finally:
        con.close()

    out = [0] * n_bins
    for bin_index, count in rows:
        # A feature starting before the window has a negative bin; it is
        # visible but its start is off-screen, so it belongs to the first bin.
        i = min(max(int(bin_index), 0), n_bins - 1)
        out[i] += count
    return out


def features_in_window(
    *,
    db_path: Path,
    contig: str,
    start: int,
    end: int,
    feature_type: str | None = None,
    biotype: str | None = None,
    strand: str | None = None,
) -> list[dict]:
    """Drawable features overlapping the window, children attached.

    One query rather than a join: every row -- parents and children alike --
    in a single SELECT, reassembled into a tree with two passes in Python. A
    join would repeat each parent's columns once per child, which for a gene
    with fifty exons is fifty copies of the same row to reassemble in Python
    anyway.

    Unpaged, deliberately -- the window is bounded by coordinates and the
    caller only reaches this below the density threshold, so the row count is
    already small by construction.

    A feature whose parent is not in the result (off-screen, or absent from a
    malformed file) is returned as a top-level row of its own rather than
    dropped: a viewer that claims to show a region must not silently omit
    features in it.
    """
    filters = FeatureFilters(
        contig=contig,
        start_min=start,
        start_max=end,
        feature_type=feature_type,
        biotype=biotype,
        strand=strand,
        # The window is the bound, not the hierarchy. An explicit type filter
        # (`exon`) must reach children, and a child whose parent is off-screen
        # still has to appear.
        top_level_only=False,
    )
    where, args = _where(filters)

    con = _connect(db_path)
    try:
        con.row_factory = sqlite3.Row
        rows = [
            dict(r)
            for r in con.execute(
                f"SELECT {_COLUMNS} FROM features{where} ORDER BY start", args
            ).fetchall()
        ]
    finally:
        con.close()

    by_id = {r["feature_id"]: r for r in rows if r["feature_id"]}
    out: list[dict] = []
    for r in rows:
        r["children"] = []
        is_self_parent = r["parent"] and r["parent"] == r["feature_id"]
        parent = by_id.get(r["parent"]) if r["parent"] and not is_self_parent else None
        if parent is None:
            out.append(r)
    for r in rows:
        is_self_parent = r["parent"] and r["parent"] == r["feature_id"]
        parent = by_id.get(r["parent"]) if r["parent"] and not is_self_parent else None
        if parent is not None:
            parent["children"].append(r)
    return out
