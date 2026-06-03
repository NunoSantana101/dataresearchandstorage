# dataresearchandstorage

A small Streamlit harness that **collects and stores data from research archives**,
one record at a time, to test a pattern before lifting it into
[sAImone](../saimoneAPIbackenddemo).

**Pattern under test:** `fetch → GPT-5.4-nano structure → JSON file → human-readable display`.

First agent: **PubMed by PMID**. Regulatory archives (FDA/EMA) are the next
target and will reuse the same fetch → structure → store layout.

## What it does

1. You enter a PubMed **PMID**.
2. The app fetches the article record from **NCBI E-utils** (free, public, no key
   required for casual use).
3. **Full text follow-through** via a retrieval ladder (many "free" articles live
   outside PMC, each publisher exposing full text differently):
   1. **PubMed Central** JATS (`db=pmc`) — cleanest, when a PMCID exists
   2. **Europe PMC** full-text XML — broader coverage, same JATS parser, free, no key
   3. **Unpaywall** (DOI → legal open-access locations) — surfaces the free
      PDF/landing link even when no machine-readable XML exists
   4. **OA PDF text extraction** (via `pypdf`) — last resort, unstructured

   Sections are parsed verbatim (including figure/table captions). The envelope
   records which source succeeded (`full_text_source`) and any open-access links
   found (`oa_locations`), so even an unparseable article still surfaces its free
   full-text link. Every rung degrades gracefully — one failing source never
   sinks the others, and a missing/broken `pypdf` just disables the PDF rung.
4. A single **GPT-5.4-nano** pass adds an *analysis layer* — study type, key
   findings (drawn from the full text when present), per-section summaries, and a
   plain-language summary.
5. The JSON is **saved to `data/`** and rendered in a readable UI, with a download
   button, a section-by-section summary, the verbatim full text, and a raw-JSON view.

**Faithful vs. analytical split:** all verbatim data (title, authors, abstract,
full-text sections, MeSH, etc.) comes straight from the parser — nano never
re-emits it, so there's no hallucination risk and far fewer output tokens. Nano
only produces the analysis fields. If no OpenAI key is supplied (or nano errors),
those fields fall back to deterministic values so the app always renders.

Not every PubMed article has open-access full text (~6M do). When it doesn't, the
app falls back gracefully to abstract-only and says so in the UI.

## Project layout

```
dataresearchandstorage/
├── app.py                      # Streamlit UI (input → collect → render → store)
├── pubmed_agent/
│   ├── __init__.py
│   ├── fetch.py                # PubMed record + full-text ladder (PMC / Europe PMC / Unpaywall / PDF)
│   └── nano_agent.py           # GPT-5.4-nano analysis pass (sAImone nano pattern)
├── data/                       # collected JSON records land here (gitignored)
├── .streamlit/secrets.toml.example
└── requirements.txt
```

The fetch layer (`fetch_pubmed_record`) and the structuring layer
(`structure_record`) are intentionally decoupled, single-purpose units so the
same loop can be reused for regulatory archives and ported into sAImone.

## Setup

```bash
pip install -r requirements.txt

# Provide your OpenAI key (either of these):
cp .streamlit/secrets.toml.example .streamlit/secrets.toml   # then edit it
# ...or paste the key into the sidebar at runtime.

streamlit run app.py
```

### Keys

| Key | Required? | Purpose |
|-----|-----------|---------|
| `OPENAI_API_KEY` | For the nano pass | GPT-5.4-nano structuring |
| `NCBI_API_KEY` | No | Raises PubMed rate limit 3→10 req/s |
| `NCBI_TOOL` / `NCBI_EMAIL` | No | NCBI politeness identifiers. **`NCBI_EMAIL` also unlocks the Unpaywall rung** (Unpaywall requires a contact email); without it, OA-link discovery is skipped. |

Provide them via `.streamlit/secrets.toml`, environment, or the sidebar inputs.

## Output schema

```jsonc
{
  // --- faithful, verbatim from the parser ---
  "pmid": "string",
  "pmcid": "string",                       // "" if not in PMC
  "title": "string",
  "journal": "string",
  "publication_date": "string",            // e-pub date, ISO (YYYY-MM-DD), sortable
  "doi": "string",
  "authors": ["string"],
  "abstract_sections": [{"label": "string", "text": "string"}],
  "mesh_terms": ["string"],
  "keywords": ["string"],
  "publication_types": ["string"],
  "full_text_available": true,
  "full_text_source": "pmc",               // "pmc" | "europepmc" | "pdf:unpaywall" | ""
  "full_text_sections": [{"title": "string", "text": "string"}],  // verbatim sections
  "oa_locations": [{"url": "...", "pdf_url": "...", "host_type": "...", "version": "..."}],
  "source_url": "https://pubmed.ncbi.nlm.nih.gov/<pmid>/",
  "pmc_url": "https://www.ncbi.nlm.nih.gov/pmc/articles/<pmcid>/",
  "doi_url": "https://doi.org/<doi>",

  // --- analysis layer, from GPT-5.4-nano ---
  "study_type": "string",
  "key_findings": ["string"],
  "section_summaries": [{"section": "string", "summary": "string"}],  // [] if no full text
  "plain_summary": "string",

  "_meta": { "model": "gpt-5.4-nano", "fallback": false, "response_id": "...", "elapsed_seconds": 1.2 }
}
```

## Notes / next steps

- **Why not the MCP server?** A standalone Streamlit app can't depend on an MCP
  server that only exists inside a Claude Code session. E-utils keeps it
  self-contained and free. The fetch layer is swappable if sAImone later wants an
  MCP- or backend-backed fetcher.
- **Next:** add a `regulatory_agent/` (FDA openFDA / EMA) implementing the same
  `fetch → structure → store` contract, and a source selector in the UI.
