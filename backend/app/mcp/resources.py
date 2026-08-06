"""Documentation the MCP server serves.

Split by whether the content can go stale. The derived resources (OpenAPI,
TOOL_META, sources, job types) are generated from the code they describe and
cannot drift. The guides are hand-written, and `tests/mcp/test_guides.py` is
what keeps them honest.
"""

from dataclasses import asdict, is_dataclass
from enum import StrEnum
from pathlib import Path

GUIDES_DIR = Path(__file__).parent / "guides"


class GuideTopic(StrEnum):
    """The workflow guides, one per path a user actually walks.

    Members are the valid arguments to `bioflow_get_guide`, and each must have
    a matching `<value>.md` in GUIDES_DIR -- asserted both directions in
    tests/mcp/test_guides.py.
    """

    GETTING_STARTED = "getting-started"


def load_guide(topic: GuideTopic) -> str:
    return (GUIDES_DIR / f"{topic.value}.md").read_text()


def installed_tools() -> dict:
    """Every tool BioFlow documents, with the fields /help/software renders.

    Derived from TOOL_META rather than listed here, so a tool added to the
    registry reaches the agent without anyone remembering this file.
    `test_every_tool_is_documented` already forces the four fields to be
    populated, which is what makes them safe to promise.
    """
    from app.pipelines.tools import TOOL_META

    return {
        "tools": {
            name: (asdict(meta) if is_dataclass(meta) else dict(meta))
            for name, meta in TOOL_META.items()
        }
    }


def job_types() -> dict:
    """The registered job types -- the valid `kind` values for
    `bioflow_run_pipeline`.

    Read from `all_handlers()`, the same registry `GET /jobs/types` serves, so
    a newly registered handler is runnable from MCP with no change here.
    """
    from app.queue.registry import all_handlers

    return {
        "job_types": {
            name: {
                "mode": spec.mode.value,
                "default_class": spec.default_class.value,
            }
            for name, spec in all_handlers().items()
        }
    }


def data_sources() -> dict:
    """External data sources, from the catalog behind /help/sources."""
    from app.pipelines.sources import DATA_SOURCES

    return {
        "sources": [
            asdict(s) if is_dataclass(s) else dict(s) for s in DATA_SOURCES
        ]
    }
