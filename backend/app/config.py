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
