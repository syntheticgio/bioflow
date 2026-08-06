"""Documentation the MCP server serves.

Split by whether the content can go stale. The derived resources (OpenAPI,
TOOL_META, sources, job types) are generated from the code they describe and
cannot drift. The guides are hand-written, and `tests/mcp/test_guides.py` is
what keeps them honest.
"""

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
