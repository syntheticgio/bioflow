"""Organism-name search: taxon autocomplete and genome search by taxon.

`ncbi_assembly.py` answers "what is this one assembly accession". This
answers a different question -- "what organism did the user mean, and what
assemblies exist for it" -- for a search box that takes a name instead of an
accession. Both live on the NCBI Datasets API and share its throttled HTTP
client (`sra._get`).

Everything here is best-effort, exactly like `ncbi_assembly.py`: a network
failure or an unrecognized organism name must never raise, only return
nothing.
"""

import json
import urllib.parse
from dataclasses import dataclass, field

from app.logging import get_logger
from app.metadata import ncbi_assembly
from app.metadata.ncbi_assembly import DATASETS, AssemblyMetadata, _int
from app.metadata.sra import _get  # same throttling, retry and never-raise rules

log = get_logger(__name__)


@dataclass
class TaxonSuggestion:
    """One candidate organism from `taxon_suggest`."""

    sci_name: str
    tax_id: int
    common_name: str | None = None
    rank: str | None = None
    group_name: str | None = None

    def as_dict(self) -> dict:
        return {
            "sci_name": self.sci_name,
            "tax_id": self.tax_id,
            "common_name": self.common_name,
            "rank": self.rank,
            "group_name": self.group_name,
        }


@dataclass
class AssemblyPage:
    """One page of assemblies for a taxon, plus how to fetch the next."""

    assemblies: list[AssemblyMetadata] = field(default_factory=list)
    next_page_token: str | None = None
    total_count: int | None = None


def suggest_organisms(query: str) -> list[TaxonSuggestion]:
    """Candidate organisms matching a partial name, for autocomplete.

    Returns an empty list on any failure -- a suggestion dropdown with
    nothing in it is the correct degraded behavior, not an error the user
    has to dismiss.
    """
    query = (query or "").strip()
    if not query:
        return []

    encoded = urllib.parse.quote(query, safe="")
    body = _get(f"{DATASETS}/taxonomy/taxon_suggest/{encoded}")
    if body is None:
        return []

    try:
        payload = json.loads(body)
        entries = payload.get("sci_name_and_ids")
        if not isinstance(entries, list):
            return []
    except (ValueError, TypeError) as e:
        log.warning("taxon_suggest_parse_failed", query=query, error=str(e))
        return []
    except Exception as e:  # noqa: BLE001 - a suggestion lookup must never raise
        log.warning("taxon_suggest_error", query=query, error=str(e))
        return []

    suggestions: list[TaxonSuggestion] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        sci_name = entry.get("sci_name")
        tax_id = _int(entry.get("tax_id"))
        if not sci_name or tax_id is None:
            continue
        suggestions.append(
            TaxonSuggestion(
                sci_name=sci_name,
                tax_id=tax_id,
                common_name=entry.get("common_name") or None,
                rank=entry.get("rank") or None,
                group_name=entry.get("group_name") or None,
            )
        )
    return suggestions


def search_assemblies_by_taxon(
    tax_id: int,
    *,
    page_token: str | None = None,
    page_size: int = 20,
    reference_only: bool = False,
    assembly_level: str | None = None,
) -> AssemblyPage:
    """One page of assemblies for a taxon ID.

    Uses NCBI's own `page_token` cursor rather than an offset: the Datasets
    API does not support arbitrary offsets into a taxon's assembly list, only
    forward paging via the token it hands back.

    `assembly_level` maps straight to the Datasets API's own
    `filters.assembly_level` param (`complete_genome` / `chromosome` /
    `scaffold` / `contig`), confirmed live against `/genome/taxon/.../
    dataset_report`.
    """
    params: dict[str, str] = {"page_size": str(page_size)}
    if page_token:
        params["page_token"] = page_token
    if reference_only:
        params["filters.reference_only"] = "true"
    if assembly_level:
        params["filters.assembly_level"] = assembly_level

    query = urllib.parse.urlencode(params)
    body = _get(f"{DATASETS}/genome/taxon/{tax_id}/dataset_report?{query}")
    if body is None:
        return AssemblyPage()

    try:
        payload = json.loads(body)
    except (ValueError, TypeError) as e:
        log.warning("taxon_assembly_search_parse_failed", tax_id=tax_id, error=str(e))
        return AssemblyPage()
    except Exception as e:  # noqa: BLE001 - a search must never fail the caller
        log.warning("taxon_assembly_search_error", tax_id=tax_id, error=str(e))
        return AssemblyPage()

    assemblies = ncbi_assembly.parse_report_list(payload)
    payload_obj = payload if isinstance(payload, dict) else {}
    return AssemblyPage(
        assemblies=assemblies,
        next_page_token=payload_obj.get("next_page_token") or None,
        total_count=_int(payload_obj.get("total_count")),
    )
