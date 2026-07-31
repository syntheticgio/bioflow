"""The SQLite database backing the variant table.

Why a database rather than the flat TSV the BAM Results tab paginates: this
tool targets plant genomes, where a resequencing VCF holds millions of calls.
Benchmarked on a synthetic 5M-variant file in the api container, reading the
whole TSV and slicing it in Python costs ~440 MB of RSS per in-flight request
and ~0.9s per page, which projects to ~2.8 GB for wheat. The same data in
SQLite with indexes on (chrom, pos) and filter answers a filtered page in
0.2-0.4ms using 14 MB.

The build is deliberately streaming: at 32M variants a list of rows would
exhaust the container before a single row was written.
"""

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from app.logging import get_logger

log = get_logger(__name__)

# Batched inserts. Large enough that per-statement overhead disappears, small
# enough that the pending batch is never a meaningful fraction of memory.
_INSERT_BATCH = 10_000


@dataclass(frozen=True)
class VariantFilters:
    """What the table is currently showing.

    One object rather than loose arguments so `query_variants` and
    `count_variants` cannot drift apart about what is being filtered -- the
    page and its total have to agree or pagination silently misreports.
    """

    contig: str | None = None
    pos_min: int | None = None
    pos_max: int | None = None
    filter_value: str | None = None
    variant_type: str | None = None  # "snp" | "indel"
    min_qual: float | None = None


def _num(value: str) -> float | None:
    """A bcftools numeric field, which is '.' when absent.

    None rather than 0: an absent depth is not a depth of zero, and storing it
    as one would place the record at the bottom of a depth chart rather than
    out of it.
    """
    if value == "." or value == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def build_variant_db(*, rows, db_path: Path) -> int:
    """Stream parsed `bcftools query` lines into an indexed SQLite database.

    `rows` is consumed once, in the order bcftools emits it (locus order), and
    is never materialized -- see the module docstring.

    Indexes are built *after* the bulk insert: creating them first makes every
    insert maintain a B-tree and turns a 7-second load into minutes. Journaling
    and synchronous writes are off because this file is a derived artifact
    rebuilt from the VCF on demand, so durability buys nothing.

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
            CREATE TABLE variants (
              chrom  TEXT,
              pos    INTEGER,
              ref    TEXT,
              alt    TEXT,
              qual   REAL,
              filter TEXT,
              dp     INTEGER,
              gt     TEXT
            )
            """
        )

        inserted = 0
        skipped = 0
        batch: list[tuple] = []
        for line in rows:
            line = line.rstrip("\n")
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) < 8:
                skipped += 1
                continue
            try:
                pos = int(parts[1])
            except ValueError:
                skipped += 1
                continue
            qual = _num(parts[4])
            dp = _num(parts[6])
            # Every column from 7 on is one sample's genotype -- the query
            # format's `[\t%GT]` repeats per sample. Rejoined rather than
            # taking parts[7] alone, which would silently drop samples 2..n
            # and leave the table showing sample 1's genotype whichever
            # sample the picker selects. The frontend splits this back apart
            # by index.
            batch.append(
                (
                    parts[0],
                    pos,
                    parts[2],
                    parts[3],
                    qual,
                    parts[5],
                    int(dp) if dp is not None else None,
                    "\t".join(parts[7:]),
                )
            )
            if len(batch) >= _INSERT_BATCH:
                con.executemany(
                    "INSERT INTO variants VALUES (?,?,?,?,?,?,?,?)", batch
                )
                inserted += len(batch)
                batch = []

        if batch:
            con.executemany("INSERT INTO variants VALUES (?,?,?,?,?,?,?,?)", batch)
            inserted += len(batch)

        con.execute("CREATE INDEX ix_variants_locus ON variants(chrom, pos)")
        con.execute("CREATE INDEX ix_variants_filter ON variants(filter)")
        con.commit()
    finally:
        con.close()

    if skipped:
        log.warning("variant_db_skipped_lines", count=skipped, db=str(db_path))
    return inserted


def _where(filters: VariantFilters) -> tuple[str, list]:
    """The WHERE clause and its bound parameters.

    Every value is bound, never interpolated: these come from query string
    arguments and reach a SQL statement directly.
    """
    clauses: list[str] = []
    args: list = []

    if filters.contig:
        clauses.append("chrom = ?")
        args.append(filters.contig)
    if filters.pos_min is not None:
        clauses.append("pos >= ?")
        args.append(filters.pos_min)
    if filters.pos_max is not None:
        clauses.append("pos <= ?")
        args.append(filters.pos_max)
    if filters.filter_value:
        clauses.append("filter = ?")
        args.append(filters.filter_value)
    if filters.min_qual is not None:
        clauses.append("qual >= ?")
        args.append(filters.min_qual)
    if filters.variant_type == "snp":
        clauses.append("length(ref) = 1 AND length(alt) = 1")
    elif filters.variant_type == "indel":
        clauses.append("length(ref) <> length(alt)")

    if not clauses:
        return "", []
    return " WHERE " + " AND ".join(clauses), args


def _connect(db_path: Path) -> sqlite3.Connection:
    """Read-only. Nothing but the compute job ever writes, and SQLite handles
    concurrent readers without coordination."""
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def query_variants(
    *, db_path: Path, filters: VariantFilters, offset: int, limit: int
) -> list[dict]:
    """One page of the table, in locus order.

    Ordered by rowid rather than an ORDER BY on (chrom, pos): bcftools query
    emits records in locus order already, so insertion order *is* locus order,
    and sorting explicitly would cost a scan on every page.
    """
    where, args = _where(filters)
    con = _connect(db_path)
    try:
        con.row_factory = sqlite3.Row
        cur = con.execute(
            f"SELECT chrom,pos,ref,alt,qual,filter,dp,gt FROM variants{where} "
            f"LIMIT ? OFFSET ?",
            [*args, limit, offset],
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        con.close()


def count_variants(*, db_path: Path, filters: VariantFilters) -> int:
    """How many rows match. See the route: this is not recomputed on every
    page turn, because a combined predicate costs ~400ms at 5M rows."""
    where, args = _where(filters)
    con = _connect(db_path)
    try:
        return con.execute(f"SELECT COUNT(*) FROM variants{where}", args).fetchone()[0]
    finally:
        con.close()
