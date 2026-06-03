"""
Nano analysis agent.

Takes a raw PubMed record (from fetch.py — bibliographic metadata, abstract, and
PMC full text when available) and runs a single GPT-5.4-nano pass that produces an
*analysis* layer: study type, key findings, per-section summaries, and a
plain-language summary.

Design note: the faithful, verbatim data (title, authors, abstract sections, full
text, MeSH, etc.) is carried straight from the parser into the final envelope —
nano never re-emits it, so there's no hallucination risk and far fewer output
tokens. Nano only adds the analytical fields. This mirrors the sAImone nano
pattern (Responses API, low reasoning effort, parse output_text, strip fences,
json.loads) with a deterministic fallback so the UI always renders.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from openai import OpenAI

from .fetch import RawArticle

logger = logging.getLogger(__name__)

NANO_MODEL = "gpt-5.4-nano"
NANO_REASONING_EFFORT = "low"
NANO_MAX_TOKENS = 6000  # headroom for per-section summaries on long full-text reviews
NANO_TIMEOUT = 180  # seconds

# Analytical contract only — bibliographic data is supplied deterministically.
NANO_INSTRUCTIONS = """You are a biomedical literature analysis agent for a medical-affairs platform.

You receive one PubMed record: bibliographic metadata, the abstract, and — when
the article is open-access in PubMed Central — its FULL TEXT.

Return ONLY a JSON object (no prose, no markdown fences) with exactly this shape:
{
  "study_type": "string",
  "key_findings": ["string"],
  "section_summaries": [{"section": "string", "summary": "string"}],
  "plain_summary": "string"
}

Rules:
- Use ONLY information present in the provided record. Never invent or infer facts
  that are not in the source.
- "study_type": a short tag, e.g. "randomized controlled trial", "review",
  "observational study", "in silico / computational", "meta-analysis",
  "case report", "unknown".
- "key_findings": 3-8 short, concrete result/conclusion statements. If full text
  is present, draw on the Methods/Results/Conclusions — not only the abstract.
- "section_summaries": if full text is present, one entry per MAJOR section
  (use the section's own heading as "section", a 1-3 sentence "summary"). If NO
  full text is present, return an empty list [].
- "plain_summary": 2-3 sentences a non-specialist can understand."""


def _extract_output_text(response: Any) -> str:
    """Pull text from a Responses API result (output_text shorthand or output[].content[])."""
    text = getattr(response, "output_text", None)
    if text:
        return text
    parts: list[str] = []
    for item in getattr(response, "output", []) or []:
        if getattr(item, "type", None) == "message":
            for block in getattr(item, "content", []) or []:
                if getattr(block, "type", None) == "output_text":
                    parts.append(block.text)
    return "".join(parts)


def _strip_fences(raw: str) -> str:
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines)
    return cleaned.strip()


def _faithful_fields(record: RawArticle) -> dict[str, Any]:
    """Verbatim, parser-sourced data that nano must NOT regenerate."""
    return {
        "pmid": record.pmid,
        "pmcid": record.pmcid,
        "title": record.title,
        "journal": record.journal,
        "publication_date": record.publication_date,
        "doi": record.doi,
        "authors": record.authors,
        "abstract_sections": record.abstract_sections,
        "mesh_terms": record.mesh_terms,
        "keywords": record.keywords,
        "publication_types": record.publication_types,
        "full_text_available": record.full_text_available,
        "full_text_sections": record.full_text_sections,
        "source_url": f"https://pubmed.ncbi.nlm.nih.gov/{record.pmid}/",
        "pmc_url": (
            f"https://www.ncbi.nlm.nih.gov/pmc/articles/{record.pmcid}/"
            if record.pmcid else ""
        ),
        "doi_url": f"https://doi.org/{record.doi}" if record.doi else "",
    }


def _fallback_analysis(record: RawArticle) -> dict[str, Any]:
    """Deterministic analysis layer when nano is unavailable/fails."""
    return {
        "study_type": (
            record.publication_types[0].lower() if record.publication_types else "unknown"
        ),
        "key_findings": [],
        "section_summaries": [],
        "plain_summary": "",
    }


def _run_nano(record: RawArticle, *, api_key: str, model: str, meta: dict[str, Any]) -> dict[str, Any]:
    """Run nano, returning only the analytical fields. Raises on any failure."""
    client = OpenAI(api_key=api_key)
    user_input = [
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": f"PubMed record to analyze:\n\n{record.to_flat_text()}"}
            ],
        }
    ]
    response = client.responses.create(
        model=model,
        input=user_input,
        instructions=NANO_INSTRUCTIONS,
        max_output_tokens=NANO_MAX_TOKENS,
        reasoning={"effort": NANO_REASONING_EFFORT},
        truncation="auto",
        store=False,
        timeout=NANO_TIMEOUT,
    )
    meta["response_id"] = getattr(response, "id", None)
    status = getattr(response, "status", None)
    if status and status != "completed":
        raise ValueError(f"nano response status={status}")

    parsed = json.loads(_strip_fences(_extract_output_text(response)))
    # Keep only the analytical contract; ignore anything else nano emits.
    return {
        "study_type": str(parsed.get("study_type") or "unknown"),
        "key_findings": list(parsed.get("key_findings") or []),
        "section_summaries": list(parsed.get("section_summaries") or []),
        "plain_summary": str(parsed.get("plain_summary") or ""),
    }


def structure_record(
    record: RawArticle,
    *,
    api_key: str,
    model: str = NANO_MODEL,
) -> dict[str, Any]:
    """
    Produce the final structured envelope: faithful parser data + nano analysis.

    Always carries a "_meta" block (model, elapsed seconds, response id, fallback
    flag). Never raises for model issues — falls back to a deterministic analysis
    so the UI always has something to render.
    """
    start = time.monotonic()
    meta: dict[str, Any] = {"model": model, "fallback": False, "response_id": None}

    try:
        analysis = _run_nano(record, api_key=api_key, model=model, meta=meta)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("nano analysis failed (%s); using deterministic fallback", exc)
        analysis = _fallback_analysis(record)
        meta.update(fallback=True, error=str(exc))
    except Exception as exc:  # network / auth / SDK errors
        logger.warning("nano call errored (%s); using deterministic fallback", exc)
        analysis = _fallback_analysis(record)
        meta.update(fallback=True, error=str(exc))

    meta["elapsed_seconds"] = round(time.monotonic() - start, 2)

    payload = _faithful_fields(record)
    payload.update(analysis)
    payload["_meta"] = meta
    return payload
