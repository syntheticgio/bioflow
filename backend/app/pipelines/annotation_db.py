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
from app.pipelines.annotation_parse import Feature

log = get_logger(__name__)

# Batched inserts, same reasoning as variant_db._INSERT_BATCH.
_INSERT_BATCH = 10_000

# The columns every read returns. Named once so the page query and the
# children query cannot drift apart about the row shape the client parses.
_COLUMNS = (
    "contig, start, end, type, strand, score, name, feature_id, "
    "parent, biotype, attributes"
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
    """

    contig: str | None = None
    start_min: int | None = None
    start_max: int | None = None
    feature_type: str | None = None
    biotype: str | None = None
    name_query: str | None = None
    strand: str | None = None
    top_level_only: bool = True


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
              attributes TEXT
            )
            """
        )

        inserted = 0
        batch: list[tuple] = []
        for f in rows:
            batch.append(
                (
                    f.contig, f.start, f.end, f.type, f.strand, f.score,
                    f.name, f.feature_id, f.parent, f.biotype, f.attributes,
                )
            )
            if len(batch) >= _INSERT_BATCH:
                con.executemany(
                    "INSERT INTO features VALUES (?,?,?,?,?,?,?,?,?,?,?)", batch
                )
                inserted += len(batch)
                batch = []

        if batch:
            con.executemany(
                "INSERT INTO features VALUES (?,?,?,?,?,?,?,?,?,?,?)", batch
            )
            inserted += len(batch)

        con.execute("CREATE INDEX ix_features_locus ON features(contig, start)")
        # The index the whole paging design rests on: expanding a gene must
        # be a seek, not a scan of three million rows.
        con.execute("CREATE INDEX ix_features_parent ON features(parent)")
        con.execute("CREATE INDEX ix_features_type ON features(type)")
        con.execute("CREATE INDEX ix_features_name ON features(name)")
        con.commit()
    finally:
        con.close()

    return inserted


def _where(filters: FeatureFilters) -> tuple[str, list]:
    """The WHERE clause and its bound parameters.

    Every value is bound, never interpolated: these come from query string
    arguments and reach a SQL statement directly.
    """
    clauses: list[str] = []
    args: list = []

    if filters.top_level_only:
        clauses.append("parent IS NULL")
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
