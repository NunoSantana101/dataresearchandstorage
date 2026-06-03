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
REQUEST_TIMEOUT = 30  # seconds

# Full text can be large; cap what we feed downstream (we still store it all).
FULL_TEXT_CHAR_CAP = 200_000


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
    pmcid: str = ""
    authors: list[str] = field(default_factory=list)
    abstract_sections: list[dict[str, str]] = field(default_factory=list)
    mesh_terms: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    publication_types: list[str] = field(default_factory=list)
    full_text_sections: list[dict[str, str]] = field(default_factory=list)
    full_text_available: bool = False

    def abstract_text(self) -> str:
        """Join structured abstract sections into one block."""
        parts = []
        for sec in self.abstract_sections:
            label = sec.get("label", "").strip()
            text = sec.get("text", "").strip()
            parts.append(f"{label}: {text}" if label else text)
        return "\n".join(p for p in parts if p)

    def full_text(self) -> str:
        """Join full-text sections into one block (verbatim)."""
        parts = []
        for sec in self.full_text_sections:
            title = (sec.get("title") or "").strip()
            text = (sec.get("text") or "").strip()
            if not text and not title:
                continue
            parts.append(f"## {title}\n{text}".strip() if title else text)
        return "\n\n".join(parts)

    def to_flat_text(self) -> str:
        """Render the record as a flat text block for the nano structuring prompt."""
        lines = [
            f"PMID: {self.pmid}",
            f"PMCID: {self.pmcid or '(none)'}",
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
        if self.full_text_available:
            full = self.full_text()
            if len(full) > FULL_TEXT_CHAR_CAP:
                full = full[:FULL_TEXT_CHAR_CAP] + "\n\n[... full text truncated ...]"
            lines += ["", "Full text (from PubMed Central):", full]
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


_MONTHS = {
    "jan": "01", "feb": "02", "mar": "03", "apr": "04", "may": "05", "jun": "06",
    "jul": "07", "aug": "08", "sep": "09", "oct": "10", "nov": "11", "dec": "12",
}


def _norm_month(month: str) -> str:
    """Normalize a PubMed month (name, number, or season) to a zero-padded number, or ''."""
    month = (month or "").strip()
    if not month:
        return ""
    if month.isdigit():
        return month.zfill(2)
    return _MONTHS.get(month[:3].lower(), "")  # '' for seasons (Spring/Fall/…)


def _format_date(year: str, month: str, day: str) -> str:
    """Render a (year, month, day) triple as a sortable ISO-ish date.

    Returns 'YYYY-MM-DD', 'YYYY-MM', or 'YYYY' depending on what's present.
    Empty if there's no year.
    """
    year = (year or "").strip()
    if not year:
        return ""
    mm = _norm_month(month)
    if not mm:
        return year
    parts = [year, mm]
    day = (day or "").strip()
    if day.isdigit():
        parts.append(day.zfill(2))
    return "-".join(parts)


def _date_from_el(el: ET.Element | None) -> str:
    """Format a date from an element carrying <Year>/<Month>/<Day> children."""
    if el is None:
        return ""
    return _format_date(
        _text(el.find("Year")), _text(el.find("Month")), _text(el.find("Day"))
    )


def _extract_publication_date(article: ET.Element) -> str:
    """Best publication date for sorting/recency.

    Prefer the article's electronic publication date (<ArticleDate>, which is the
    actual e-pub date) over the journal issue's cover date (<JournalIssue>/<PubDate>,
    which is frequently an end-of-period placeholder like '31 Dec'). Fall back to the
    History epublish/pubmed dates, then the issue cover date / MedlineDate.
    """
    # 1) <ArticleDate> — the electronic publication date (DateType defaults to Electronic).
    for adate in article.findall(".//Article/ArticleDate"):
        if (adate.get("DateType") or "Electronic") == "Electronic":
            iso = _date_from_el(adate)
            if iso:
                return iso

    # 2) History pub dates — epublish first, then the pubmed entry date.
    history = {
        ppd.get("PubStatus"): ppd
        for ppd in article.findall(".//PubmedData/History/PubMedPubDate")
    }
    for status in ("epublish", "pubmed", "entrez"):
        iso = _date_from_el(history.get(status))
        if iso:
            return iso

    # 3) Journal issue cover date (coarse / placeholder), then MedlineDate string.
    pubdate = article.find(".//Article/Journal/JournalIssue/PubDate")
    if pubdate is not None:
        iso = _date_from_el(pubdate)
        if iso:
            return iso
        return _text(pubdate.find("MedlineDate"))
    return ""


def _parse_article(article: ET.Element, pmid: str) -> RawArticle:
    rec = RawArticle(pmid=pmid)

    rec.title = _text(article.find(".//Article/ArticleTitle"))
    rec.journal = _text(article.find(".//Article/Journal/Title"))

    # Publication date — actual e-pub date preferred over the issue cover date.
    rec.publication_date = _extract_publication_date(article)

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
    # PMC id + DOI fallback — both live in the ArticleIdList.
    for aid in article.findall(".//PubmedData/ArticleIdList/ArticleId"):
        id_type = aid.get("IdType")
        value = _text(aid)
        if not value:
            continue
        if id_type == "doi" and not rec.doi:
            rec.doi = value
        elif id_type == "pmc":
            # Normalize to "PMC1234567".
            rec.pmcid = value if value.upper().startswith("PMC") else f"PMC{value}"

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


# --------------------------------------------------------------------------- #
# PubMed Central full-text (JATS XML) — robust, namespace-agnostic parsing
# --------------------------------------------------------------------------- #
# JATS in the wild is inconsistent: some feeds carry XML namespaces, sections
# may lack titles, content paragraphs sometimes sit loose in <body>, nesting can
# be deep, and figures/tables/boxed-text appear anywhere. We match elements by
# their *local* tag name (ignoring any namespace) and walk defensively so the
# parser degrades gracefully instead of throwing on an unfamiliar layout.

_BLOCK_TAGS = {"p", "list", "disp-quote", "statement", "verse-group", "speech"}
_CAPTIONED_TAGS = {"fig", "table-wrap", "boxed-text", "supplementary-material"}
_MAX_SECTION_DEPTH = 12  # runaway guard for pathological nesting


def _local(tag: str) -> str:
    """Strip any XML namespace, returning the bare local tag name."""
    if isinstance(tag, str) and "}" in tag:
        return tag.rsplit("}", 1)[-1]
    return tag


def _first_child(el: ET.Element, name: str) -> ET.Element | None:
    for child in el:
        if _local(child.tag) == name:
            return child
    return None


def _caption_entry(el: ET.Element) -> dict[str, str] | None:
    """Build a {title, text} entry from a <fig>/<table-wrap>/<boxed-text> node."""
    label = ""
    cap_parts: list[str] = []
    for child in el:
        ln = _local(child.tag)
        if ln == "label":
            label = _text(child)
        elif ln in ("caption", "title"):
            t = _text(child)
            if t:
                cap_parts.append(t)
    text = "\n".join(p for p in cap_parts if p)
    if not text:
        return None
    return {"title": label or _local(el.tag).replace("-", " ").title(), "text": text}


def _walk_section(sec: ET.Element, prefix: str, out: list[dict[str, str]], depth: int) -> None:
    if depth > _MAX_SECTION_DEPTH:
        return
    title_el = _first_child(sec, "title")
    title = _text(title_el) if title_el is not None else ""
    full_title = f"{prefix} › {title}" if prefix and title else (title or prefix)

    # Direct block content of THIS section only (nested <sec> handled by recursion,
    # so itertext on leaf blocks never double-counts subsection text).
    body_parts: list[str] = []
    for child in sec:
        ln = _local(child.tag)
        if ln in _BLOCK_TAGS:
            txt = _text(child)
            if txt:
                body_parts.append(txt)
    if body_parts:
        out.append({"title": full_title or "Section", "text": "\n\n".join(body_parts)})

    # Figures/tables/boxes anchored in this section, then recurse subsections — in order.
    for child in sec:
        ln = _local(child.tag)
        if ln in _CAPTIONED_TAGS:
            entry = _caption_entry(child)
            if entry:
                out.append(entry)
        elif ln == "sec":
            _walk_section(child, full_title, out, depth + 1)


def _flush_loose(loose: list[str], out: list[dict[str, str]]) -> None:
    if loose:
        out.append({"title": "", "text": "\n\n".join(loose)})
        loose.clear()


def _parse_pmc_body(root: ET.Element) -> list[dict[str, str]]:
    """Parse a JATS article body into an ordered list of {title, text} sections."""
    body = next((el for el in root.iter() if _local(el.tag) == "body"), None)
    if body is None:
        return []

    out: list[dict[str, str]] = []
    loose: list[str] = []
    for child in body:
        ln = _local(child.tag)
        if ln == "sec":
            _flush_loose(loose, out)
            _walk_section(child, "", out, 0)
        elif ln in _BLOCK_TAGS:
            txt = _text(child)
            if txt:
                loose.append(txt)
        elif ln in _CAPTIONED_TAGS:
            _flush_loose(loose, out)
            entry = _caption_entry(child)
            if entry:
                out.append(entry)
    _flush_loose(loose, out)
    return out


def fetch_pmc_fulltext(
    pmcid: str,
    *,
    api_key: str | None = None,
    tool: str | None = None,
    email: str | None = None,
) -> list[dict[str, str]]:
    """
    Fetch and parse PMC full text (JATS XML) for a PMCID.

    Returns an ordered list of {title, text} sections (possibly empty if the
    article has no machine-readable body). Raises PubMedFetchError on a bad
    PMCID, network failure, or unparseable XML.
    """
    num = (pmcid or "").upper().replace("PMC", "").strip()
    if not num.isdigit():
        raise PubMedFetchError(f"'{pmcid}' is not a valid PMCID.")

    params: dict[str, str] = {"db": "pmc", "id": num, "retmode": "xml"}
    if api_key:
        params["api_key"] = api_key
    if tool:
        params["tool"] = tool
    if email:
        params["email"] = email

    logger.info("Fetching full text for PMC%s", num)
    try:
        resp = requests.get(EFETCH_URL, params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise PubMedFetchError(f"PMC full-text request failed for PMC{num}: {exc}") from exc

    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as exc:
        raise PubMedFetchError(f"Could not parse PMC XML for PMC{num}: {exc}") from exc

    return _parse_pmc_body(root)


def fetch_pubmed_record(
    pmid: str,
    *,
    include_full_text: bool = True,
    api_key: str | None = None,
    tool: str | None = None,
    email: str | None = None,
) -> RawArticle:
    """
    Fetch and parse a single PubMed record by PMID.

    When `include_full_text` is set and the article has a PMCID, the PMC full
    text is fetched and attached. A full-text failure never fails the record —
    it logs a warning and returns the abstract-level record (`full_text_available`
    stays False).

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

    if include_full_text and record.pmcid:
        try:
            sections = fetch_pmc_fulltext(
                record.pmcid, api_key=api_key, tool=tool, email=email
            )
            record.full_text_sections = sections
            record.full_text_available = bool(sections)
            logger.info(
                "PMID %s: attached %d full-text sections from %s",
                pmid, len(sections), record.pmcid,
            )
        except PubMedFetchError as exc:
            logger.warning("Full text unavailable for %s: %s", record.pmcid, exc)

    return record
