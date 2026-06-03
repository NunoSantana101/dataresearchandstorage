# dataresearchandstorage

A small Streamlit harness that **collects and stores data from research archives**,
one record at a time, to test a pattern before lifting it into
[sAImone](../saimoneAPIbackenddemo).

**Pattern under test:** `fetch → GPT-5.4-nano structure → JSON file → human-readable display`.

First agent: **PubMed by PMID**. Regulatory archives (FDA/EMA) are the next
target and will reuse the same fetch → structure → store layout.

## What it does

1. You enter a PubMed **PMID**.
2. The app fetches the full article record from **NCBI E-utils** (free, public,
   no key required for casual use).
3. A single **GPT-5.4-nano** pass structures the raw record into a clean JSON
   envelope (sectioned abstract, extracted key findings, plain-language summary,
   normalized bibliographic fields).
4. The JSON is **saved to `data/`** and rendered in a readable UI, with a
   download button and a raw-JSON view.

If no OpenAI key is supplied (or nano errors), the app falls back to a
deterministic structuring of the parsed record so it always renders something.

## Project layout

```
dataresearchandstorage/
├── app.py                      # Streamlit UI (input → collect → render → store)
├── pubmed_agent/
│   ├── __init__.py
│   ├── fetch.py                # NCBI E-utils fetch + PubMed XML parsing (swappable)
│   └── nano_agent.py           # GPT-5.4-nano structuring pass (sAImone nano pattern)
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
| `NCBI_TOOL` / `NCBI_EMAIL` | No | NCBI politeness identifiers |

Provide them via `.streamlit/secrets.toml`, environment, or the sidebar inputs.

## Output schema

```jsonc
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
  "plain_summary": "string",
  "source_url": "https://pubmed.ncbi.nlm.nih.gov/<pmid>/",
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
