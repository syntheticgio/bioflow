"""Split a multi-sequence reference into memory-budget-aware buckets.

The bucket planner reads a FASTA index and packs sequences using a greedy
first-fit-decreasing algorithm keyed by estimated per-sequence memory usage.
Per-sequence estimates come from the aligner's MemoryModel.

Chunking is unnecessary for single-sequence references (returns None).
STAR and Winnowmap are gated at the AlignerSpec level; the planner does
not check — callers must gate before invoking.
"""

import math
from dataclasses import dataclass
from pathlib import Path


@dataclass
class BucketSpec:
    index: int
    sequences: list[str]
    total_bases: int
    estimated_mb: int
    fasta_path: Path | None = None


def pack_buckets(
    *,
    sequences: list[tuple[str, int]],  # (name, bases) from .fai
    memory_budget_mb: int,
    per_base_index_mb: float,
    fixed_overhead_mb: int = 256,
    bytes_per_thread_mb: int = 512,
    threads: int = 4,
    sort_memory_mb: int = 1024,
) -> list[BucketSpec] | None:
    """Pack reference sequences into memory-budget-aware buckets.

    Returns None when chunking is unnecessary (<= 1 bucket or <= 1 sequence),
    or when the budget is too tight for any configuration.
    """
    if len(sequences) <= 1:
        return None

    # Per-bucket overhead: aligner fixed cost + worker memory + sort buffers
    worker_mb = threads * bytes_per_thread_mb
    sort_mb = threads * sort_memory_mb
    per_bucket_overhead = fixed_overhead_mb + worker_mb + sort_mb
    effective_budget = memory_budget_mb - per_bucket_overhead

    if effective_budget <= 0:
        # Budget too tight for even a single bucket with overhead — the
        # caller should run the single-shot check and surface a refusal.
        return None

    sorted_seqs = sorted(sequences, key=lambda s: s[1], reverse=True)
    buckets: list[BucketSpec] = []

    for name, bases in sorted_seqs:
        seq_index_mb = math.ceil((bases * per_base_index_mb))
        seq_total_mb = seq_index_mb + per_bucket_overhead
        placed = False
        for bucket in buckets:
            if bucket.estimated_mb + seq_index_mb <= memory_budget_mb:
                bucket.sequences.append(name)
                bucket.total_bases += bases
                bucket.estimated_mb += seq_index_mb
                placed = True
                break
        if not placed:
            if seq_total_mb > memory_budget_mb:
                # This sequence alone cannot fit a bucket.  Raising here
                # gives the caller a useful ValidationError naming the
                # sequence and the budget instead of a plan that OOMs
                # forty minutes in.
                from app.errors import PermanentError

                raise PermanentError(
                    f"Sequence '{name}' ({bases:,} bp) requires "
                    f"{seq_total_mb:,} MB (index + overhead) but the "
                    f"per-bucket budget is {memory_budget_mb:,} MB — "
                    f"cannot produce a chunked alignment plan that fits."
                )
            buckets.append(
                BucketSpec(
                    index=len(buckets),
                    sequences=[name],
                    total_bases=bases,
                    estimated_mb=seq_total_mb,
                )
            )

    if len(buckets) <= 1:
        return None
    return buckets


def write_bucket_fastas(
    full_fasta: Path,
    buckets: list[BucketSpec],
    out_dir: Path,
) -> list[BucketSpec]:
    """Write per-bucket FASTA files by extracting sequences from `full_fasta`.

    Two-pass to avoid loading the whole reference into memory:
    1. Collect the set of sequence names needed across all buckets.
    2. Stream through the FASTA, collecting only those sequences.

    For a 3.2 GB human genome this keeps peak memory at roughly the
    largest single sequence rather than the whole file.
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    # Pass 1: which sequences do we need?
    needed: set[str] = set()
    for bucket in buckets:
        needed.update(bucket.sequences)

    # Pass 2: stream the FASTA, collecting only needed sequences
    records: dict[str, list[str]] = {}
    current_name: str | None = None
    current_lines: list[str] = []

    with open(full_fasta) as f:
        for line in f:
            line = line.rstrip("\n")
            if line.startswith(">"):
                if current_name is not None:
                    records[current_name] = current_lines
                    current_lines = []
                    # Stop early once all needed sequences are collected
                    if len(records) == len(needed):
                        break
                name = line[1:].split()[0]
                current_name = name if name in needed else None
            elif current_name is not None:
                current_lines.append(line)
    if current_name is not None and len(records) < len(needed):
        records[current_name] = current_lines

    # Write per-bucket files
    for bucket in buckets:
        path = out_dir / f"bucket_{bucket.index}.fa"
        with open(path, "w") as f:
            for name in bucket.sequences:
                seq_lines = records.get(name)
                if seq_lines is None:
                    raise ValueError(
                        f"Sequence '{name}' in bucket {bucket.index} "
                        f"was not found in the reference FASTA "
                        f"({full_fasta}). The .fai index and FASTA "
                        f"disagree — the reference may be corrupt."
                    )
                f.write(f">{name}\n")
                f.write("\n".join(seq_lines))
                f.write("\n")
        bucket.fasta_path = path

    return buckets
