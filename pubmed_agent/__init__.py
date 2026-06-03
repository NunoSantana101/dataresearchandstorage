"""PubMed-by-PMID collection agent: fetch (NCBI E-utils) → nano structuring → JSON."""

from .fetch import (
    PubMedFetchError,
    RawArticle,
    fetch_pmc_fulltext,
    fetch_pubmed_record,
    resolve_full_text,
)
from .nano_agent import NANO_MODEL, structure_record

__all__ = [
    "PubMedFetchError",
    "RawArticle",
    "fetch_pubmed_record",
    "fetch_pmc_fulltext",
    "resolve_full_text",
    "structure_record",
    "NANO_MODEL",
]
