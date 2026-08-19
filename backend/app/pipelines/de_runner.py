"""Merging per-sample counts and testing them between conditions.

Two jobs, and the first one is the reason this file is careful.

`quantify` writes one counts file per sample, each from its own job, possibly
run days apart and possibly against different annotations. Merging them is
therefore not a formality: `merge_counts` refuses inputs that do not describe
the same gene universe rather than intersecting them, because an inner join
across two annotation releases produces a matrix that is structurally perfect,
smaller than either input, and silently tests a different set of genes than
the user believes. Nothing downstream can detect that, and the result reads as
a normal experiment.

The PyDESeq2 call itself is thin. It is kept here rather than in the handler
for the same reason every other runner module exists: the parts worth testing
are pure functions over dicts, with no queue and no filesystem.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from app.errors import ValidationError
from app.logging import get_logger

log = get_logger(__name__)

# DESeq2's own convention, and the threshold the results table highlights.
DEFAULT_ALPHA = 0.05

# Below this, a negative binomial fit has nothing to estimate dispersion from.
# PyDESeq2 does not refuse a singleton group -- it produces a result with no
# usable standard error -- so the refusal has to happen here.
MIN_REPLICATES = 2

# How far the gene sets of two samples may differ before the merge refuses.
# Zero: any difference at all means the inputs were counted against different
# annotations, and there is no threshold at which silently dropping genes from
# a differential expression test is the helpful thing to do. Named rather than
# inlined so the reasoning has somewhere to live.
GENE_SET_TOLERANCE = 0


@dataclass
class SampleCounts:
    """One sample's counts, as read from one `quantify` output."""

    sample: str
    condition: str
    counts: dict[str, int]
    annotation_sha256: str | None = None
    object_id: str | None = None


@dataclass
class CountMatrix:
    """N samples' counts over a shared gene set."""

    genes: list[str]
    samples: list[str]
    conditions: list[str]
    # [sample][gene] -- the orientation PyDESeq2 wants.
    values: list[list[int]]

    @property
    def n_samples(self) -> int:
        return len(self.samples)

    @property
    def n_genes(self) -> int:
        return len(self.genes)


@dataclass
class DEResult:
    rows: list[dict] = field(default_factory=list)
    facts: dict = field(default_factory=dict)

    def to_tsv(self) -> str:
        cols = [
            "gene",
            "base_mean",
            "log2_fold_change",
            "lfc_std_error",
            "stat",
            "p_value",
            "padj",
        ]
        lines = ["\t".join(cols)]
        for row in self.rows:
            lines.append(
                "\t".join(
                    "" if row.get(c) is None else str(row.get(c)) for c in cols
                )
            )
        return "\n".join(lines) + "\n"


def validate_design(samples: list[SampleCounts], *, test: str, reference: str) -> None:
    """Refuse a design that cannot produce a meaningful result.

    Every check here could be left to PyDESeq2, and none of them would produce
    a message a user could act on -- a singleton group yields a fit with no
    standard error rather than an error, and a contrast naming a condition
    that is not present raises a KeyError from inside the library. Failing
    here means failing in the launch request, where the dialog can point at
    the offending group, rather than twenty seconds into a worker thread.
    """
    if len(samples) < 2:
        raise ValidationError(
            "Differential expression needs at least two samples.",
            details={"samples": len(samples)},
        )

    by_condition: dict[str, list[str]] = {}
    for s in samples:
        by_condition.setdefault(s.condition, []).append(s.sample)

    seen: set[str] = set()
    repeated_set: set[str] = set()
    for s in samples:
        if s.sample in seen:
            repeated_set.add(s.sample)
        seen.add(s.sample)
    repeated = sorted(repeated_set)
    if repeated:
        raise ValidationError(
            f"The same sample appears more than once: {', '.join(repeated)}.",
            details={"duplicates": repeated},
        )

    missing = [c for c in (test, reference) if c not in by_condition]
    if missing:
        raise ValidationError(
            f"No samples are assigned to {' or '.join(repr(m) for m in missing)}. "
            f"Conditions present: {', '.join(sorted(by_condition))}.",
            details={
                "missing": missing,
                "present": sorted(by_condition),
            },
        )

    if test == reference:
        raise ValidationError(
            f"The contrast compares {test!r} with itself. Pick two different "
            f"conditions.",
            details={"contrast": [test, reference]},
        )

    thin = {
        cond: names
        for cond, names in by_condition.items()
        if cond in (test, reference) and len(names) < MIN_REPLICATES
    }
    if thin:
        detail = "; ".join(
            f"{cond} has {len(names)} ({', '.join(names)})"
            for cond, names in sorted(thin.items())
        )
        raise ValidationError(
            f"Every condition needs at least {MIN_REPLICATES} replicates to "
            f"estimate variability: {detail}.",
            details={"conditions": {c: len(n) for c, n in thin.items()}},
        )


def merge_counts(samples: list[SampleCounts]) -> CountMatrix:
    """N per-sample count files as one matrix, or a refusal.

    The refusal is the point. These files came from independent jobs, and two
    of them counted against different annotation releases will overlap heavily
    without agreeing -- the same organism, most of the same genes, a few
    hundred added or renamed. Intersecting them yields a matrix that passes
    every downstream sanity check while quietly testing a different gene set
    than either input describes, and no plot or number in the results would
    look wrong.

    So: same annotation digest where all inputs record one, and identical gene
    sets always. A mismatch names the samples and the size of the difference,
    because "re-quantify these two against the same annotation" is the fix and
    the user needs to know which two.
    """
    if not samples:
        raise ValidationError("No samples to merge.")

    digests = {s.annotation_sha256 for s in samples if s.annotation_sha256}
    if len(digests) > 1:
        by_digest: dict[str, list[str]] = {}
        for s in samples:
            if s.annotation_sha256:
                by_digest.setdefault(s.annotation_sha256, []).append(s.sample)
        groups = "; ".join(
            f"{', '.join(sorted(names))}" for names in by_digest.values()
        )
        raise ValidationError(
            "These samples were counted against different annotations, so "
            "their genes are not comparable. Re-quantify them against one "
            f"annotation. Groups: {groups}.",
            details={"annotation_groups": {d: n for d, n in by_digest.items()}},
        )

    reference_genes = set(samples[0].counts)
    if not reference_genes:
        raise ValidationError(
            f"{samples[0].sample!r} has no counts in it.",
            details={"sample": samples[0].sample},
        )

    for s in samples[1:]:
        genes = set(s.counts)
        missing = reference_genes - genes
        extra = genes - reference_genes
        if len(missing) + len(extra) > GENE_SET_TOLERANCE:
            raise ValidationError(
                f"{s.sample!r} and {samples[0].sample!r} do not have the same "
                f"genes ({len(missing)} present only in "
                f"{samples[0].sample!r}, {len(extra)} only in {s.sample!r}). "
                "They were most likely counted against different annotations; "
                "re-quantify them against one.",
                details={
                    "sample": s.sample,
                    "compared_with": samples[0].sample,
                    "missing": len(missing),
                    "extra": len(extra),
                    "examples": sorted(list(missing)[:5] + list(extra)[:5]),
                },
            )

    # Sorted so the matrix is reproducible across runs -- dict order follows
    # the order featureCounts wrote its rows, which is annotation order and
    # stable in practice, but nothing guarantees it and a results table whose
    # row order shifts between identical runs is needlessly confusing.
    genes = sorted(reference_genes)

    return CountMatrix(
        genes=genes,
        samples=[s.sample for s in samples],
        conditions=[s.condition for s in samples],
        values=[[s.counts[g] for g in genes] for s in samples],
    )


def output_name(test: str, reference: str) -> str:
    """The results file name for a contrast."""
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in f"{test}_vs_{reference}")
    return f"de_{safe}.tsv"


def run_deseq2(
    matrix: CountMatrix,
    *,
    test: str,
    reference: str,
    threads: int = 4,
    alpha: float = DEFAULT_ALPHA,
    on_phase: Callable[[str, float, str], None] | None = None,
) -> DEResult:
    """Fit the model and extract one contrast.

    Imported lazily. PyDESeq2 pulls anndata, zarr, h5py, scikit-learn and
    matplotlib on the way in, and this module is imported by the handler
    registry at worker start -- paying that import for every job type, on
    every worker boot, to serve one handler is not worth it.
    """
    import pandas as pd
    from pydeseq2.dds import DeseqDataSet
    from pydeseq2.ds import DeseqStats

    def phase(name: str, pct: float, message: str) -> None:
        if on_phase is not None:
            on_phase(name, pct, message)

    counts_df = pd.DataFrame(
        matrix.values, index=matrix.samples, columns=matrix.genes
    )
    metadata = pd.DataFrame(
        {"condition": matrix.conditions}, index=matrix.samples
    )

    phase("fitting", 0.35, f"fitting {matrix.n_genes} genes across {matrix.n_samples} samples")

    dds = DeseqDataSet(
        counts=counts_df,
        metadata=metadata,
        design="~condition",
        refit_cooks=True,
        n_cpus=threads,
        quiet=True,
    )
    dds.deseq2()

    phase("testing", 0.75, f"testing {test} against {reference}")

    stats = DeseqStats(
        dds,
        contrast=["condition", test, reference],
        alpha=alpha,
        quiet=True,
        n_cpus=threads,
    )
    stats.summary()
    results = stats.results_df

    phase("clustering", 0.85, "projecting samples")
    # One matrix, both plots. Computing the top-variance selection once is what
    # guarantees the projection and the correlation heatmap describe the same
    # genes -- if one used all genes and the other a subset they could disagree,
    # and a reader would have no way to tell which to believe.
    selected = _expression_matrix(dds)
    pca = _sample_pca(selected, matrix)
    correlation = _sample_correlation(selected, matrix)

    phase("writing", 0.9, "collecting results")

    rows: list[dict] = []
    for gene, row in results.iterrows():
        rows.append({
            "gene": gene,
            "base_mean": _round(row.get("baseMean")),
            "log2_fold_change": _round(row.get("log2FoldChange")),
            "lfc_std_error": _round(row.get("lfcSE")),
            "stat": _round(row.get("stat")),
            "p_value": _sci(row.get("pvalue")),
            "padj": _sci(row.get("padj")),
        })

    # Sorted by adjusted p-value, nulls last. padj is NaN for genes DESeq2
    # filtered out of multiple-testing correction (low counts, or a Cook's
    # outlier), which is a real outcome rather than a missing value -- they
    # belong in the file, just not at the top of it.
    rows.sort(key=lambda r: (r["padj"] is None, r["padj"] if r["padj"] is not None else 0))

    facts = _facts(rows, matrix, test, reference, alpha)
    if pca is not None:
        facts["sample_pca"] = pca
    if correlation is not None:
        facts["sample_correlation"] = correlation

    return DEResult(rows=rows, facts=facts)


# Genes used for the sample plots, chosen by variance. DESeq2's own plotPCA
# uses 500 and the number is not critical: enough that the projection reflects
# real structure, few enough that housekeeping genes carrying no signal do not
# dominate it.
PCA_TOP_GENES = 500

# Below this a correlation matrix is not worth drawing. Three samples give
# three off-diagonal numbers, which is the fewest that can show one sample
# disagreeing with two that agree; two samples give a single number beside a
# diagonal of 1.0, which says nothing a scalar would not have said.
MIN_CORRELATION_SAMPLES = 3


def _expression_matrix(dds):
    """The log-normalised top-variance matrix both sample plots are read from.

    Returns a samples x genes array, or None if the fit does not carry what is
    expected here. Shared by `_sample_pca` and `_sample_correlation` so the two
    provably describe the same genes.
    """
    try:
        import numpy as np

        counts = np.asarray(dds.layers["normed_counts"], dtype=float)
        # log2(x+1) before either plot: raw counts span several orders of
        # magnitude, and without it both end up describing which genes are
        # highly expressed rather than how the samples differ.
        logged = np.log2(counts + 1.0)

        variances = logged.var(axis=0)
        if variances.size == 0:
            return None
        top = np.argsort(variances)[::-1][:PCA_TOP_GENES]
        return logged[:, top]
    except Exception as e:  # noqa: BLE001
        log.warning("expression_matrix_failed", error=str(e))
        return None


def _sample_pca(selected, matrix: CountMatrix) -> list[dict] | None:
    """Samples projected onto their first two principal components.

    The plot worth having, and the reason it is computed here rather than in
    the browser: it is how a swapped or mislabelled sample is caught. A
    replicate sitting with the wrong group is obvious in two dimensions and
    invisible in a table of 6000 p-values -- and if it is there, every number
    in the results is describing the wrong comparison.

    Bounded output: N samples with two coordinates each, so it goes in `facts`
    rather than on disk however many genes went into it.

    Returns None rather than raising if anything about the fit is not what is
    expected here. A missing plot is a worse results tab; an exception is no
    results tab at all, after the expensive part already succeeded.
    """
    if selected is None:
        return None
    try:
        import numpy as np

        centered = selected - selected.mean(axis=0)
        # SVD rather than sklearn's PCA: the projection is three lines this
        # way, and it avoids importing an estimator to use one of its methods.
        _, singular, vt = np.linalg.svd(centered, full_matrices=False)
        if singular.size < 2:
            return None

        coords = centered @ vt[:2].T
        explained = (singular**2) / (singular**2).sum()

        return [
            {
                "sample": matrix.samples[i],
                "condition": matrix.conditions[i],
                "pc1": round(float(coords[i, 0]), 4),
                "pc2": round(float(coords[i, 1]), 4),
                "pc1_pct": round(float(explained[0]) * 100, 1),
                "pc2_pct": round(float(explained[1]) * 100, 1),
            }
            for i in range(len(matrix.samples))
        ]
    except Exception as e:  # noqa: BLE001
        log.warning("sample_pca_failed", error=str(e))
        return None


def _sample_correlation(selected, matrix: CountMatrix) -> dict | None:
    """Sample-to-sample correlation over the same genes the projection uses.

    Spearman rather than Pearson, and the choice matters enough that the chart
    labels it. Even after log2, a handful of very highly expressed genes carry
    most of the remaining spread, and Pearson lets those few genes set the
    correlation for the whole pair. Ranking first bounds every gene's
    contribution, so the number answers "do these samples order their genes the
    same way" rather than "do their loudest genes agree". A reader comparing
    against another tool needs to know which of the two they are looking at,
    hence `method` travelling with the matrix.

    Answers what the projection cannot: how strongly replicates actually agree
    (two samples can sit adjacent on PC1/PC2 while correlating poorly, since
    the first two components may carry only a modest share of the variance),
    whether there is block structure orthogonal to those components, and which
    specific pair an outlier is and is not similar to.

    Bounded output: N samples means N^2 values, a few hundred numbers for any
    realistic DE design, so it goes in `facts` alongside the projection.

    Returns None rather than raising, matching `_sample_pca` -- a missing plot
    is a worse results tab, an exception is no results tab at all.
    """
    if selected is None:
        return None
    if len(matrix.samples) < MIN_CORRELATION_SAMPLES:
        return None
    try:
        import numpy as np

        # Rank each sample's genes against each other (axis 1), so a value
        # becomes that gene's standing within its own sample. Correlating those
        # ranks is what makes this Spearman rather than Pearson, and ranking
        # within the sample -- not down each gene -- is what makes the pairwise
        # number a comparison of two expression profiles.
        order = selected.argsort(axis=1).argsort(axis=1).astype(float)

        centered = order - order.mean(axis=1, keepdims=True)
        norms = np.sqrt((centered**2).sum(axis=1))
        # A sample whose genes all tie has no spread to correlate. It cannot
        # happen in a real fit, but a zero norm would divide to NaN and blank
        # the plot rather than one row of it.
        if not np.all(norms > 0):
            return None

        corr = (centered @ centered.T) / np.outer(norms, norms)
        # Clip: the products above can land a hair outside [-1, 1] on floating
        # point, and a 1.0000000002 on the diagonal reads as a bug.
        corr = np.clip(corr, -1.0, 1.0)

        return {
            "method": "spearman",
            "samples": list(matrix.samples),
            "conditions": list(matrix.conditions),
            "matrix": [[round(float(v), 4) for v in row] for row in corr],
        }
    except Exception as e:  # noqa: BLE001
        log.warning("sample_correlation_failed", error=str(e))
        return None


def _facts(
    rows: list[dict], matrix: CountMatrix, test: str, reference: str, alpha: float
) -> dict:
    """The bounded summary that goes onto the object.

    Bounded deliberately: the full table is tens of thousands of rows and
    lives on disk. What is stored on the object is what a person needs to
    decide whether to open it.
    """
    tested = [r for r in rows if r["padj"] is not None]
    significant = [r for r in tested if r["padj"] < alpha]
    up = [r for r in significant if (r["log2_fold_change"] or 0) > 0]

    counts_by_condition: dict[str, int] = {}
    for cond in matrix.conditions:
        counts_by_condition[cond] = counts_by_condition.get(cond, 0) + 1

    return {
        "contrast_test": test,
        "contrast_reference": reference,
        "alpha": alpha,
        "samples": matrix.n_samples,
        "samples_by_condition": counts_by_condition,
        "genes_in_matrix": matrix.n_genes,
        # Genes that survived independent filtering and actually carry an
        # adjusted p-value. Reported next to genes_in_matrix rather than
        # instead of it: the gap between the two is itself informative, and a
        # large one usually means a shallow library.
        "genes_tested": len(tested),
        "significant_genes": len(significant),
        "significant_up": len(up),
        "significant_down": len(significant) - len(up),
    }


def _round(value, places: int = 4):
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    # NaN and inf both fail this and become None. NaN is DESeq2 saying "not
    # estimable here", which is information the table should show as blank
    # rather than as the string "nan".
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return round(f, places)


def _sci(value):
    """A p-value, kept as a float rather than rounded.

    Rounding a p-value to four places collapses everything below 1e-4 to zero,
    which is most of what a DE run finds interesting.
    """
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f:
        return None
    return f


_RESULT_COLUMNS = (
    "gene",
    "base_mean",
    "log2_fold_change",
    "lfc_std_error",
    "stat",
    "p_value",
    "padj",
)


def read_results(path: Path) -> list[dict]:
    """A results TSV back as rows, for the table endpoint.

    The inverse of `DEResult.to_tsv`, and deliberately tolerant: an empty
    field means DESeq2 had no estimate (a gene filtered out of multiple-testing
    correction, or a Cook's outlier), which is a real outcome that must read
    back as None rather than as 0.0. Rounding a missing padj to zero would
    promote every untested gene to the top of a significance sort.
    """
    rows: list[dict] = []
    with open(path, errors="replace") as fh:
        header = fh.readline().rstrip("\n").split("\t")
        columns = header if header and header[0] == "gene" else list(_RESULT_COLUMNS)
        for line in fh:
            if not line.strip():
                continue
            parts = line.rstrip("\n").split("\t")
            row: dict = {}
            for i, col in enumerate(columns):
                value = parts[i] if i < len(parts) else ""
                if col == "gene":
                    row[col] = value
                elif value == "":
                    row[col] = None
                else:
                    try:
                        row[col] = float(value)
                    except ValueError:
                        row[col] = None
            rows.append(row)
    return rows


SORTABLE_COLUMNS = frozenset(
    {"gene", "padj", "p_value", "log2_fold_change", "base_mean", "stat"}
)


def sort_rows(rows: list[dict], sort: str, direction: str) -> list[dict]:
    """Results ordered for the table, with untested genes always last.

    Lives here rather than in the route because it is the kind of thing that
    looks right and is not. The obvious implementation -- a `(value is None,
    value)` sort key -- puts them last ascending and *first* descending, since
    `reverse` reverses the whole tuple. Sorting by fold change descending then
    opens on a page of genes that have no fold change, which is the least
    informative thing this table can show.

    Partitioning first is what makes the promise hold in both directions: the
    two groups are never compared to each other, so `reverse` cannot reorder
    them relative to one another.

    An unrecognised column leaves the order alone rather than raising. The
    caller is a query parameter, and a typo in a URL should not be a 500.
    """
    if sort not in SORTABLE_COLUMNS:
        return rows

    reverse = direction == "desc"

    if sort == "gene":
        # Always present, so no partition is needed -- and a gene name is a
        # string, which the numeric branch's key would not order sensibly.
        return sorted(rows, key=lambda r: str(r.get("gene", "")), reverse=reverse)

    present = [r for r in rows if r.get(sort) is not None]
    missing = [r for r in rows if r.get(sort) is None]
    present.sort(key=lambda r: r[sort], reverse=reverse)
    return present + missing


def counts_path_stem(name: str) -> str:
    """A readable sample name from a counts file name.

    `SRR1234567_trimmed.sorted.counts.tsv` is what the pipeline produces and
    is not a column header anyone wants to read. The metadata `sample` field
    is the real answer; this is the fallback when it is unset.
    """
    stem = Path(name).name
    for suffix in (".counts.tsv", ".tsv", ".txt"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    return stem
