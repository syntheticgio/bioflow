Dashboard is honest now. Here's where things stand:

## 📊 N. tazetta status — 2026-08-05 07:12

| Job | Progress | Size | Status |
|-----|----------|------|--------|
| Hi-C R1 compression | ✅ **100%** | 93 GB .gz | **done** (sources cleaned) |
| Hi-C R2 compression | ✅ **100%** | 94 GB .gz | **done** (sources cleaned) |
| HiFi B → FASTQ.gz (SRR36683177) | **76%** | 191.8 GB / ~253 GB | running, ~1.7 MB/s |
| HiFi A → FASTQ.gz (SRR36683178) | **83.5%** | 192.0 GB / ~231 GB | running, ~1.7 MB/s |

**ETA: roughly 6–10 hours** for both (fastq-dump's internal gzip is single-threaded — that's the bottleneck, ~2.4 GB/h).

### Why it said 100% earlier — and what I fixed
The monitor's `expected` sizes were set to the SRA file sizes (180/176 GB), but gzip FASTQ expands **~1.31×** beyond SRA (your own Hi-C pair proved it: 143 GB SRA → 187 GB gz). So the jobs capped at 100% while still growing. I updated the estimates to the measured ratio — the page now shows the real 76%/83.5%, and the cron will mark them done when the fastq-dump processes actually exit. The conversions themselves are healthy (both processes ~70% CPU, files growing steadily, ~1.2 TiB disk free — no capacity risk).

## 🗺️ Next steps (what happens after the conversions finish)

**1. Read QC (~15 min)**
- `seqkit stats` on the four .gz files → read counts, N50, total bases. Verify coverage: ~700 Gb HiFi over a ~14.9 Gb genome ≈ **~47× HiFi** (excellent for assembly).
- Cross-check read counts against the SRA metadata so we know nothing was lost in conversion.

**2. Genome size confirmation (~1–2 h)** — worth doing before committing a multi-day assembly
- K-mer counting (Jellyfish) → histogram to independently confirm the 14.9 Gb estimate (from 2C = 15.2 pg). If it's wildly different, assembly parameters change.
- This also gives you the heterozygosity estimate, which matters for hifiasm settings.

**3. Assembly decision — this is where your input matters** 🧭
For a ~15 Gb plant genome with HiFi + Hi-C, the standard is **hifiasm with Hi-C mode** (`--h1/--h2` — does assembly + chromosome scaffolding in one pass, produces a Hi-C-validated phased assembly). But there's a real constraint to weigh:

- **Memory**: 256 GB RAM is the ceiling, and a 15 Gb genome in hifiasm is *near the edge* of what fits (roughly 1.5–2 GB per Gb of genome during peak → ~25–40 GB for graphs, but Hi-C mode adds overhead). It will likely fit, but it's not comfortable.
- **Options to consider**:
  - **A. hifiasm straight up** with `--hg-size 15g` — simplest, one tool, standard output. Risk: RAM ceiling.
  - **B. Same split-and-merge strategy you used for Lycoris** — more control, proven on your 24 Gb monster, but more moving parts.
  - **C. Small test run first** (subset ~10× reads → quick hifiasm smoke test) to validate memory footprint before committing the full multi-day run. I'd recommend this regardless of A or B.

**4. Assembly QC (~1 h)**
- QUAST (contiguity stats), BUSCO against the liliopsida lineage, Hi-C contact map (HiGlass/Juicebox) to sanity-check the 14 chromosomes visually.

**5. Post-assembly** (separate phase, can be planned while 3–4 run)
- Repeat masking, then gene annotation (BRAKER), then the synteny/comparative work vs. Lycoris that motivated this.

**Data management note:** reads stay as-is (713 GB HiFi + 331 GB Hi-C gz on ModelExtension — 1.2 TiB free, fine). The `assembly/` dir is ready for the hifiasm output.

---

Want me to set up the QC + k-mer step now (it's scriptable and runs when the conversions finish), and put together the hifiasm memory math for options A vs B so you can pick?