"""
Nano structuring agent.

Takes a raw PubMed record (from fetch.py) and runs a single GPT-5.4-nano pass to
produce a clean, machine-readable JSON envelope: normalized bibliographic fields,
a sectioned abstract, extracted key findings, and a plain-language summary.

This mirrors the sAImone `nano_agent` pattern (Responses API, low reasoning
effort, parse `output_text`, strip fences, json.loads). It is deliberately a thin,
swappable unit so the same "fetch → nano → JSON" pattern can later be lifted into
the sAImone backend.
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
NANO_MAX_TOKENS = 4000
NANO_TIMEOUT = 120  # seconds

# The contract the model must emit. Kept as plain text (not strict json_schema)
# because nano follows an explicit example reliably and it keeps the unit simple.
NANO_INSTRUCTIONS = """You are a biomedical literature structuring agent for a medical-affairs platform.

You receive the raw record of a single PubMed article. Your job is to return a
clean JSON object — and nothing else (no prose, no markdown fences).

Rules:
- Use ONLY information present in the provided record. Do NOT invent or infer
  facts that are not in the source. If a field is unknown, use an empty string or
  empty list.
- "abstract_sections": split the abstract into its logical sections. If the source
  already labels sections (Background, Methods, Results, Conclusions), preserve
  those labels. If the abstract is a single block, return one section with an empty
  label.
- "key_findings": 2-6 short bullet statements capturing the article's concrete
  results/conclusions, drawn strictly from the abstract.
- "plain_summary": 2-3 sentences a non-specialist can understand.
- "study_type": a short tag based on the publication types / abstract (e.g.
  "randomized controlled trial", "review", "observational study", "case report",
  "unknown").

Return exactly this JSON shape:
{
  "pmid": "string",
  "title": "string",
  "journal": "string",
  "publication_date": "string",
  "doi": "string",
  "authors": ["string"],
  "study_type": "string",
  "abstract_sections": [{"label": "string", "text": "string"}],
  "key_findings": ["string"],
  "mesh_terms": ["string"],
  "keywords": ["string"],
  "plain_summary": "string"
}"""


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


def _fallback_payload(record: RawArticle) -> dict[str, Any]:
    """Deterministic structuring from the parsed record when nano is unavailable/fails."""
    return {
        "pmid": record.pmid,
        "title": record.title,
        "journal": record.journal,
        "publication_date": record.publication_date,
        "doi": record.doi,
        "authors": record.authors,
        "study_type": (record.publication_types[0].lower() if record.publication_types else "unknown"),
        "abstract_sections": record.abstract_sections or [],
        "key_findings": [],
        "mesh_terms": record.mesh_terms,
        "keywords": record.keywords,
        "plain_summary": "",
    }


def structure_record(
    record: RawArticle,
    *,
    api_key: str,
    model: str = NANO_MODEL,
) -> dict[str, Any]:
    """
    Run nano over a raw PubMed record and return a structured dict.

    The returned dict always carries a "_meta" block describing how it was produced
    (model, elapsed seconds, OpenAI response id, and whether the deterministic
    fallback was used). Never raises for model issues — falls back to a
    deterministic structuring so the UI always has something to render.
    """
    client = OpenAI(api_key=api_key)
    flat = record.to_flat_text()
    user_input = [
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": f"PubMed record to structure:\n\n{flat}"}
            ],
        }
    ]

    start = time.monotonic()
    meta: dict[str, Any] = {"model": model, "fallback": False, "response_id": None}
    try:
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

        raw_text = _extract_output_text(response)
        payload = json.loads(_strip_fences(raw_text))
        # Guard: nano must at least echo the PMID; trust the source PMID regardless.
        payload["pmid"] = record.pmid
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("nano structuring failed (%s); using deterministic fallback", exc)
        payload = _fallback_payload(record)
        meta["fallback"] = True
        meta["error"] = str(exc)
    except Exception as exc:  # network / auth / SDK errors
        logger.warning("nano call errored (%s); using deterministic fallback", exc)
        payload = _fallback_payload(record)
        meta["fallback"] = True
        meta["error"] = str(exc)

    meta["elapsed_seconds"] = round(time.monotonic() - start, 2)
    payload["source_url"] = f"https://pubmed.ncbi.nlm.nih.gov/{record.pmid}/"
    payload["_meta"] = meta
    return payload
