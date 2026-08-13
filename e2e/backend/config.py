"""Configuration for the BioFlow e2e harness.

Loaded from a config.json in the harness data dir, overridable by env vars.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path


def data_dir() -> str:
    """The harness data directory (config + result DB). Created if missing."""
    path = Path.home() / ".hermes" / "plugins" / "bioflow-e2e" / "data"
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


@dataclass
class Config:
    base_url: str = "http://localhost:8000"
    profile: str = ""
    cleanup: bool = False


def _defaults() -> dict:
    return {f.name: f.default for f in fields(Config)}


def _apply_env(cfg: dict) -> dict:
    if os.environ.get("BIOFLOW_BASE_URL"):
        cfg["base_url"] = os.environ["BIOFLOW_BASE_URL"]
    if os.environ.get("BIOFLOW_PROFILE"):
        cfg["profile"] = os.environ["BIOFLOW_PROFILE"]
    return cfg


def load(data_dir: str) -> Config:
    """Read config.json from ``data_dir``, filling missing keys from defaults.

    Env vars ``BIOFLOW_BASE_URL`` and ``BIOFLOW_PROFILE`` always win. A
    missing or unreadable config file falls back to defaults.
    """
    path = Path(data_dir) / "config.json"
    cfg = _defaults()
    if path.exists():
        try:
            cfg.update(json.loads(path.read_text()))
        except (json.JSONDecodeError, OSError):
            pass  # corrupt/unreadable config -> defaults
    _apply_env(cfg)
    known = _defaults()
    return Config(**{k: cfg.get(k, known[k]) for k in known})


def save(data_dir: str, cfg: Config) -> None:
    """Write ``cfg`` as JSON to ``data_dir``/config.json."""
    path = Path(data_dir) / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(cfg), indent=2))
