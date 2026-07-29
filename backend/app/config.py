"""Application settings, loaded from the environment."""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Infrastructure ---
    mongo_url: str = "mongodb://mongo:27017/biopipe?replicaSet=rs0&directConnection=true"
    mongo_db: str = "biopipe"
    redis_url: str = "redis://redis:6379/0"

    # --- Storage ---
    bioinfo_home: Path = Path("/data")
    # Colon-separated roots that register-in-place may reference.
    bioinfo_register_roots: str = "/data"
    max_simple_upload_bytes: int = 100 * 1024 * 1024

    # --- Queue ---
    lease_ttl_seconds: int = 30
    worker_max_concurrent: int = 4
    worker_id: str = ""  # defaults to hostname:pid at startup
    job_max_attempts: int = 5
    # Reaper/promotion cadence.
    reaper_interval_seconds: float = 10.0
    promote_interval_seconds: float = 30.0
    drain_timeout_seconds: float = 60.0

    # --- Load governor (Phase 4; stubbed OPEN until then) ---
    bioinfo_cpu_budget: float | None = None
    bioinfo_mem_budget_mb: int | None = None

    # --- Metadata enrichment ---
    # Looks up SRR/SRX accessions at NCBI during ingest. Outbound network call;
    # set false to keep the stack fully offline.
    sra_enrichment_enabled: bool = True

    # Looks up GCA/GCF assembly accessions at NCBI during ingest. Outbound
    # network call; set false to keep the stack fully offline.
    assembly_enrichment_enabled: bool = True

    # --- Pipelines ---
    # Bare names by default, resolved on PATH; override with an absolute path
    # to pin a specific build.
    fastp_path: str = "fastp"
    fastqc_path: str = "fastqc"
    cutadapt_path: str = "cutadapt"
    # Debian ships no bare `trimmomatic`: the package installs TrimmomaticPE
    # and TrimmomaticSE as separate entry points around the JAR. The runner
    # picks between them by read layout (paired vs single-end).
    trimmomatic_path: str = "TrimmomaticSE"  # kept for the version probe only
    trimmomatic_pe_path: str = "TrimmomaticPE"
    trimmomatic_se_path: str = "TrimmomaticSE"
    # Adapter FASTA files the Debian package installs alongside the binaries.
    trimmomatic_adapters_dir: str = "/usr/share/trimmomatic"
    bwa_mem2_path: str = "bwa-mem2"
    minimap2_path: str = "minimap2"
    samtools_path: str = "samtools"
    fasterq_dump_path: str = "fasterq-dump"
    prefetch_path: str = "prefetch"
    nanoplot_path: str = "NanoPlot"
    # bcftools does the short-read calling and all VCF indexing
    # (`bcftools index -t`), so it is the only new binary the code invokes for
    # variant calling besides Clair3 itself.
    bcftools_path: str = "bcftools"
    clair3_path: str = "run_clair3.sh"
    # Model directories, one per Clair3 --platform. The install script
    # normalizes each to hold the checkpoint files directly.
    clair3_models_dir: str = "/opt/clair3/models"


    # Threads a single trim run may use. Deliberately well below the core count:
    # the queue admits more than one compute job at a time, and fastp's own
    # scaling flattens out past a handful of threads while the IO cost does not.
    pipeline_default_threads: int = 4

    # Memory per samtools sort thread. samtools spills to disk when it runs out,
    # and its own default (768M) is conservative enough to make a large sort
    # thrash on a machine that had memory to spare.
    samtools_sort_mem_mb: int = 1024

    # Captured job output is the only record of how a run actually went, but it
    # is not worth keeping forever on a drive holding sequencing data.
    pipeline_log_retention_days: int = 30

    log_level: str = "INFO"
    owner: str = "local"

    @property
    def register_roots(self) -> list[Path]:
        return [Path(p).resolve() for p in self.bioinfo_register_roots.split(":") if p]

    # Directory layout under BIOINFO_HOME.
    @property
    def objects_dir(self) -> Path:
        return self.bioinfo_home / "objects"

    @property
    def staging_dir(self) -> Path:
        return self.bioinfo_home / "staging"

    @property
    def tmp_dir(self) -> Path:
        return self.bioinfo_home / "tmp"

    @property
    def logs_dir(self) -> Path:
        return self.bioinfo_home / "logs"

    @property
    def ncbi_dir(self) -> Path:
        """Where the SRA Toolkit keeps its configuration and its cache.

        Set explicitly because the toolkit otherwise writes under $HOME, which
        in a container is whatever the runtime user happens to have -- often
        unwritable, and the resulting failure ("cannot open configuration")
        looks nothing like its cause. Derived from BIOINFO_HOME rather than
        hardcoded to /data so it follows a relocated home, and kept under tmp/
        because everything in it is a cache that can be rebuilt.
        """
        return self.tmp_dir / "ncbi"

    @property
    def ncbi_settings_path(self) -> Path:
        """The file `NCBI_SETTINGS` points at. See `ncbi_dir`."""
        return self.ncbi_dir / "user-settings.mkfg"

    @property
    def qc_reports_dir(self) -> Path:
        """Generated QC reports, keyed by object id.

        Outside objects/ deliberately: a FastQC report is derivative and
        regenerable, so content-addressing it would buy deduplication of
        something that is never shared and cost a blob record per run.
        """
        return self.bioinfo_home / "qc_reports"

    @property
    def bam_stats_dir(self) -> Path:
        """Generated BAM Results reports (the full per-contig TSV), keyed by
        object id.

        Outside objects/ deliberately, same rationale as qc_reports_dir: this
        is derivative and regenerable from the BAM itself, so content-
        addressing it would buy deduplication of something never shared and
        cost a blob record per run.
        """
        return self.bioinfo_home / "bam_stats"

    @property
    def meta_dir(self) -> Path:
        return self.bioinfo_home / ".biopipe"

    @property
    def sentinel_path(self) -> Path:
        """Proves the drive is actually mounted. See storage/home.py."""
        return self.meta_dir / "VERSION"

    @property
    def lock_path(self) -> Path:
        return self.meta_dir / "lock"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
