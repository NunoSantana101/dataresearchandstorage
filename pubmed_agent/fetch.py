"""
NCBI PubMed fetch layer.

Self-contained PubMed retrieval over the public NCBI E-utils HTTP API. Given a
PMID it pulls the full PubmedArticle record (efetch, XML) and parses it into a
clean dict plus a flat text block that the nano structuring pass consumes.

Why E-utils and not an MCP server: this keeps the app self-contained. E-utils is
free, needs no key for casual use (an optional NCBI_API_KEY only raises the rate
limit), and has zero runtime dependency on any external MCP service. The single
`fetch_pubmed_record()` entry point is intentionally swappable — a future MCP- or
sAImone-backed fetcher can implement the same signature.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any

import requests

logger = logging.getLogger(__name__)

EFETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
REQUEST_TIMEOUT = 20  # seconds


class PubMedFetchError(Exception):
    """Raised when a PMID cannot be retrieved or parsed."""


@dataclass
class RawArticle:
    """Parsed PubMed record + a flat text rendering for the nano pass."""

    pmid: str
    title: str = ""
    journal: str = ""
    publication_date: str = ""
    doi: str = ""
    authors: list[str] = field(default_factory=list)
    abstract_sections: list[dict[str, str]] = field(default_factory=list)
    mesh_terms: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    publication_types: list[str] = field(default_factory=list)

    def abstract_text(self) -> str:
        """Join structured abstract sections into one block."""
        parts = []
        for sec in self.abstract_sections:
            label = sec.get("label", "").strip()
            text = sec.get("text", "").strip()
            parts.append(f"{label}: {text}" if label else text)
        return "\n".join(p for p in parts if p)

    def to_flat_text(self) -> str:
        """Render the record as a flat text block for the nano structuring prompt."""
        lines = [
            f"PMID: {self.pmid}",
            f"Title: {self.title}",
            f"Journal: {self.journal}",
            f"Publication date: {self.publication_date}",
            f"DOI: {self.doi}",
            f"Authors: {', '.join(self.authors) if self.authors else '(none listed)'}",
            f"Publication types: {', '.join(self.publication_types) or '(none)'}",
            f"MeSH terms: {', '.join(self.mesh_terms) or '(none)'}",
            f"Keywords: {', '.join(self.keywords) or '(none)'}",
            "",
            "Abstract:",
            self.abstract_text() or "(no abstract available)",
        ]
        return "\n".join(lines)


def _clean_pmid(pmid: str) -> str:
    pmid = (pmid or "").strip()
    if pmid.lower().startswith("pmid:"):
        pmid = pmid.split(":", 1)[1].strip()
    if not pmid.isdigit():
        raise PubMedFetchError(
            f"'{pmid}' is not a valid PMID — a PMID is all digits (e.g. 38000000)."
        )
    return pmid


def _text(el: ET.Element | None) -> str:
    if el is None:
        return ""
    # itertext() flattens inline markup (e.g. <i>, <sup>) inside the node.
    return "".join(el.itertext()).strip()


def _parse_article(article: ET.Element, pmid: str) -> RawArticle:
    rec = RawArticle(pmid=pmid)

    rec.title = _text(article.find(".//Article/ArticleTitle"))
    rec.journal = _text(article.find(".//Article/Journal/Title"))

    # Publication date — prefer the article's PubDate, fall back to medline date.
    pubdate = article.find(".//Article/Journal/JournalIssue/PubDate")
    if pubdate is not None:
        year = _text(pubdate.find("Year"))
        month = _text(pubdate.find("Month"))
        day = _text(pubdate.find("Day"))
        medline = _text(pubdate.find("MedlineDate"))
        rec.publication_date = " ".join(p for p in (year, month, day) if p) or medline

    # Authors — "ForeName LastName", fall back to CollectiveName.
    for author in article.findall(".//Article/AuthorList/Author"):
        last = _text(author.find("LastName"))
        fore = _text(author.find("ForeName"))
        collective = _text(author.find("CollectiveName"))
        name = " ".join(p for p in (fore, last) if p) or collective
        if name:
            rec.authors.append(name)

    # DOI — from the ELocationID or the ArticleIdList.
    for eloc in article.findall(".//Article/ELocationID"):
        if eloc.get("EIdType") == "doi" and _text(eloc):
            rec.doi = _text(eloc)
            break
    if not rec.doi:
        for aid in article.findall(".//PubmedData/ArticleIdList/ArticleId"):
            if aid.get("IdType") == "doi" and _text(aid):
                rec.doi = _text(aid)
                break

    # Abstract — may be split into labelled sections (structured abstract).
    for abst in article.findall(".//Article/Abstract/AbstractText"):
        text = _text(abst)
        if not text:
            continue
        label = abst.get("Label") or abst.get("NlmCategory") or ""
        rec.abstract_sections.append({"label": label, "text": text})

    # MeSH headings.
    for mesh in article.findall(".//MeshHeadingList/MeshHeading/DescriptorName"):
        if _text(mesh):
            rec.mesh_terms.append(_text(mesh))

    # Author keywords.
    for kw in article.findall(".//KeywordList/Keyword"):
        if _text(kw):
            rec.keywords.append(_text(kw))

    # Publication types (e.g. "Randomized Controlled Trial", "Review").
    for pt in article.findall(".//Article/PublicationTypeList/PublicationType"):
        if _text(pt):
            rec.publication_types.append(_text(pt))

    return rec


def fetch_pubmed_record(
    pmid: str,
    *,
    api_key: str | None = None,
    tool: str | None = None,
    email: str | None = None,
) -> RawArticle:
    """
    Fetch and parse a single PubMed record by PMID.

    Raises PubMedFetchError on an invalid PMID, network failure, or a PMID that
    PubMed returns no article for.
    """
    pmid = _clean_pmid(pmid)
    params: dict[str, str] = {
        "db": "pubmed",
        "id": pmid,
        "rettype": "abstract",
        "retmode": "xml",
    }
    if api_key:
        params["api_key"] = api_key
    if tool:
        params["tool"] = tool
    if email:
        params["email"] = email

    logger.info("Fetching PMID %s from NCBI E-utils", pmid)
    try:
        resp = requests.get(EFETCH_URL, params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise PubMedFetchError(f"NCBI request failed for PMID {pmid}: {exc}") from exc

    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as exc:
        raise PubMedFetchError(f"Could not parse NCBI XML for PMID {pmid}: {exc}") from exc

    article = root.find(".//PubmedArticle")
    if article is None:
        raise PubMedFetchError(
            f"PubMed returned no article for PMID {pmid}. "
            "Check the ID is correct and publicly indexed."
        )

    record = _parse_article(article, pmid)
    if not record.title:
        logger.warning("PMID %s parsed with no title — record may be incomplete", pmid)
    return record
