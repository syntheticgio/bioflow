"""The SQLite database backing the structural variant table.

Separate from `variant_db.py` rather than an extension of it, because the
small-variant table classifies type by comparing `len(REF)` to `len(ALT)`.
A Sniffles record's ALT is symbolic (`<DEL>`), so every SV matches that
table's indel filter and a 4.8 kb deletion renders as a 1 bp point event at
its start position -- silently, with nothing raising. See
docs/superpowers/specs/2026-08-18-sniffles2-structural-variants-design.md.

The streaming build mirrors `variant_db.py`'s structure. Note that its own
justification -- millions of rows, a 32M-row memory ceiling -- largely does
not apply here, since an SV callset is typically thousands of records. The
shape is copied for consistency and because it costs nothing.
"""

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from app.logging import get_logger

log = get_logger(__name__)

_INSERT_BATCH = 10_000

# Log-scaled, because SV sizes span five orders of magnitude and linear bins
# would collapse nearly every call into the first bar. Each entry is the
# bin's inclusive lower bound and its label; the last bin has no upper bound.
LENGTH_BINS: tuple[tuple[int, str], ...] = (
    (50, "50 bp"),
    (100, "100 bp"),
    (1_000, "1 kb"),
    (10_000, "10 kb"),
    (100_000, "100 kb"),
    (1_000_000, "1 Mb+"),
)


@dataclass(frozen=True)
class SvRecord:
    chrom: str
    pos: int
    end: int | None
    svtype: str
    svlen: int | None
    qual: float | None
    filter_value: str
    support: int | None
    gt: str
    mate: str | None


@dataclass(frozen=True)
class SvFilters:
    """What the SV table is currently showing.

    One object rather than loose arguments so `query_svs` and `count_svs`
    cannot drift apart about what is being filtered -- the page and its total
    have to agree or pagination silently misreports.
    """

    contig: str | None = None
    pos_min: int | None = None
    pos_max: int | None = None
    svtype: str | None = None
    min_length: int | None = None
    max_length: int | None = None
    filter_value: str | None = None
    min_qual: float | None = None


def _num(value: str) -> float | None:
    if value in (".", ""):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _info(field: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in field.split(";"):
        if not item:
            continue
        key, _, value = item.partition("=")
        out[key] = value
    return out


def parse_sv_record(line: str) -> SvRecord | None:
    """One VCF data line into an SV record, or None if it is not one.

    None rather than an exception: a malformed line in a large callset should
    cost that line, not the whole build, matching how `variant_db` skips and
    counts.
    """
    parts = line.rstrip("\n").split("\t")
    if len(parts) < 8:
        return None
    try:
        pos = int(parts[1])
    except ValueError:
        return None

    info = _info(parts[7])
    svtype = info.get("SVTYPE")
    if not svtype:
        return None

    # SVLEN is negative for deletions. Stored as a magnitude: the sign is
    # redundant with SVTYPE, and keeping it would make every length filter
    # and the histogram wrong -- a -4823 bp deletion sorts below a 50 bp
    # insertion and falls into no positive bin.
    raw_len = info.get("SVLEN")
    svlen: int | None = None
    if raw_len not in (None, "", "."):
        try:
            svlen = abs(int(raw_len))
        except ValueError:
            svlen = None

    raw_end = info.get("END")
    end: int | None = None
    if raw_end not in (None, "", "."):
        try:
            end = int(raw_end)
        except ValueError:
            end = None

    support = info.get("SUPPORT")
    qual = _num(parts[5])

    return SvRecord(
        chrom=parts[0],
        pos=pos,
        end=end,
        svtype=svtype,
        svlen=svlen,
        qual=qual,
        filter_value=parts[6],
        support=int(support) if support and support.isdigit() else None,
        # Every column after FORMAT is one sample's genotype. Rejoined
        # rather than taking the first alone, which would silently drop
        # samples 2..n -- the trap `variant_db.py`'s own gt comment records.
        gt="\t".join(parts[9:]) if len(parts) > 9 else "",
        mate=info.get("MATEID"),
    )


def build_sv_db(*, rows, db_path: Path) -> int:
    """Stream VCF data lines into an indexed SQLite database.

    Indexes are built after the bulk insert, and journaling is off, for the
    reasons `variant_db.build_variant_db` documents: this file is a derived
    artifact rebuilt from the VCF on demand, so durability buys nothing.

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
            CREATE TABLE svs (
              chrom   TEXT,
              pos     INTEGER,
              end     INTEGER,
              svtype  TEXT,
              svlen   INTEGER,
              qual    REAL,
              filter  TEXT,
              support INTEGER,
              gt      TEXT,
              mate    TEXT
            )
            """
        )
        con.execute(
            """
            CREATE TABLE meta (
              key   TEXT PRIMARY KEY,
              value TEXT
            )
            """
        )

        inserted = 0
        skipped = 0
        samples: list[str] = []
        batch: list[tuple] = []
        for line in rows:
            if not line:
                continue
            if line.startswith("#CHROM"):
                parts = line.rstrip("\n").split("\t")
                if len(parts) > 9:
                    samples = parts[9:]
                continue
            if line.startswith("#"):
                continue
            rec = parse_sv_record(line)
            if rec is None:
                skipped += 1
                continue
            batch.append(
                (
                    rec.chrom,
                    rec.pos,
                    rec.end,
                    rec.svtype,
                    rec.svlen,
                    rec.qual,
                    rec.filter_value,
                    rec.support,
                    rec.gt,
                    rec.mate,
                )
            )
            if len(batch) >= _INSERT_BATCH:
                con.executemany(
                    "INSERT INTO svs VALUES (?,?,?,?,?,?,?,?,?,?)", batch
                )
                inserted += len(batch)
                batch = []

        if batch:
            con.executemany("INSERT INTO svs VALUES (?,?,?,?,?,?,?,?,?,?)", batch)
            inserted += len(batch)

        if samples:
            con.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('samples', ?)",
                (json.dumps(samples),),
            )

        con.execute("CREATE INDEX ix_svs_locus ON svs(chrom, pos)")
        con.execute("CREATE INDEX ix_svs_svtype ON svs(svtype)")
        con.execute("CREATE INDEX ix_svs_filter ON svs(filter)")
        con.commit()
    finally:
        con.close()

    if skipped:
        log.warning("sv_db_skipped_lines", count=skipped, db=str(db_path))
    return inserted


def sample_names(db_path: Path) -> list[str]:
    """Retrieve sample names stored in sv.db metadata table."""
    con = sqlite3.connect(db_path)
    try:
        cur = con.execute("SELECT value FROM meta WHERE key = 'samples'")
        row = cur.fetchone()
        if row and row[0]:
            return json.loads(row[0])
        return []
    except sqlite3.OperationalError:
        return []
    finally:
        con.close()



def _where(filters: SvFilters) -> tuple[str, list]:
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
    if filters.svtype:
        clauses.append("svtype = ?")
        args.append(filters.svtype)
    if filters.min_length is not None:
        clauses.append("svlen >= ?")
        args.append(filters.min_length)
    if filters.max_length is not None:
        clauses.append("svlen <= ?")
        args.append(filters.max_length)
    if filters.filter_value:
        clauses.append("filter = ?")
        args.append(filters.filter_value)
    if filters.min_qual is not None:
        clauses.append("qual >= ?")
        args.append(filters.min_qual)

    if not clauses:
        return "", []
    return " WHERE " + " AND ".join(clauses), args


_COLUMNS = (
    "chrom",
    "pos",
    "end",
    "svtype",
    "svlen",
    "qual",
    "filter",
    "support",
    "gt",
    "mate",
)


def query_svs(
    db_path: Path, filters: SvFilters, *, limit: int, offset: int
) -> list[dict]:
    where, args = _where(filters)
    con = sqlite3.connect(db_path)
    try:
        cur = con.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM svs{where} "
            "ORDER BY chrom, pos LIMIT ? OFFSET ?",
            [*args, limit, offset],
        )
        return [dict(zip(_COLUMNS, row, strict=True)) for row in cur.fetchall()]
    finally:
        con.close()


def count_svs(db_path: Path, filters: SvFilters) -> int:
    where, args = _where(filters)
    con = sqlite3.connect(db_path)
    try:
        cur = con.execute(f"SELECT COUNT(*) FROM svs{where}", args)
        return int(cur.fetchone()[0])
    finally:
        con.close()


def type_counts(db_path: Path) -> dict[str, int]:
    """How many of each SVTYPE."""
    con = sqlite3.connect(db_path)
    try:
        cur = con.execute("SELECT svtype, COUNT(*) FROM svs GROUP BY svtype")
        return {row[0]: int(row[1]) for row in cur.fetchall()}
    finally:
        con.close()


def length_histogram(db_path: Path) -> list[dict]:
    """SV counts per log-scaled length bin.

    Records with no length -- breakends, which join two loci and span
    neither -- are excluded rather than counted as zero, which would invent a
    bar for events that have no size.

    Every bin is returned even when empty, so the chart's axis is stable
    across callsets rather than reshaping itself per run.
    """
    con = sqlite3.connect(db_path)
    try:
        cur = con.execute("SELECT svlen FROM svs WHERE svlen IS NOT NULL")
        lengths = [int(row[0]) for row in cur.fetchall()]
    finally:
        con.close()

    counts = [0] * len(LENGTH_BINS)
    for length in lengths:
        for i in range(len(LENGTH_BINS) - 1, -1, -1):
            # Bin 0 has no effective floor: anything under its named
            # threshold (e.g. a sub-50bp call from a lower --minsvlen) still
            # belongs in the smallest bucket rather than falling through
            # unmatched and vanishing from the histogram.
            if length >= LENGTH_BINS[i][0] or i == 0:
                counts[i] += 1
                break

    return [
        {"label": label, "min_length": lower, "count": count}
        for (lower, label), count in zip(LENGTH_BINS, counts, strict=True)
    ]
