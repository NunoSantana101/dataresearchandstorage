"""PubMed-by-PMID collection agent: fetch (NCBI E-utils) → nano structuring → JSON."""

from .fetch import (
    PubMedFetchError,
    RawArticle,
    fetch_pmc_fulltext,
    fetch_pubmed_record,
)
from .nano_agent import NANO_MODEL, structure_record

__all__ = [
    "PubMedFetchError",
    "RawArticle",
    "fetch_pubmed_record",
    "fetch_pmc_fulltext",
    "structure_record",
    "NANO_MODEL",
]
