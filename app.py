"""
PubMed PMID Collector — Streamlit test harness.

Enter a PubMed PMID. The app fetches the record from NCBI E-utils, runs a
GPT-5.4-nano structuring pass, stores the result as a JSON file under data/, and
renders it in a human-readable form.

This is a pattern test for sAImone: prove the "fetch → nano structure → JSON +
display" loop is useful before lifting it into the backend.

Run:  streamlit run app.py
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st

from pubmed_agent import (
    NANO_MODEL,
    PubMedFetchError,
    fetch_pubmed_record,
    structure_record,
)

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)

st.set_page_config(page_title="PubMed PMID Collector", page_icon="🔬", layout="wide")


def _secret(name: str, default: str = "") -> str:
    try:
        return str(st.secrets.get(name, default))
    except Exception:
        return default


# --------------------------------------------------------------------------- #
# Sidebar — credentials & options
# --------------------------------------------------------------------------- #
with st.sidebar:
    st.header("⚙️ Configuration")

    openai_key = st.text_input(
        "OpenAI API key",
        value=_secret("OPENAI_API_KEY"),
        type="password",
        help="Used for the GPT-5.4-nano structuring pass. "
        "Falls back to .streamlit/secrets.toml if set there.",
    )
    model = st.text_input("Nano model", value=NANO_MODEL)

    with st.expander("NCBI options (optional)"):
        ncbi_key = st.text_input(
            "NCBI API key", value=_secret("NCBI_API_KEY"), type="password",
            help="Raises PubMed rate limit 3→10 req/s. Not required for testing.",
        )
        ncbi_tool = st.text_input("NCBI tool name", value=_secret("NCBI_TOOL", "saimone-pubmed-collector"))
        ncbi_email = st.text_input("NCBI email", value=_secret("NCBI_EMAIL"))

    st.divider()
    st.caption(
        "Data source: NCBI E-utils (free, public). "
        "Self-contained — no MCP server needed at runtime."
    )


# --------------------------------------------------------------------------- #
# Render helpers
# --------------------------------------------------------------------------- #
def render_record(payload: dict) -> None:
    meta = payload.get("_meta", {})

    st.subheader(payload.get("title") or "(untitled record)")

    # Top metadata row.
    cols = st.columns(4)
    cols[0].metric("PMID", payload.get("pmid", "—"))
    cols[1].metric("Study type", payload.get("study_type") or "—")
    cols[2].metric("Date", payload.get("publication_date") or "—")
    cols[3].metric(
        "Structured by",
        "fallback" if meta.get("fallback") else meta.get("model", "nano"),
    )

    journal = payload.get("journal")
    doi = payload.get("doi")
    bib = []
    if journal:
        bib.append(f"*{journal}*")
    if doi:
        bib.append(f"DOI: [{doi}]({payload.get('doi_url') or f'https://doi.org/{doi}'})")
    if payload.get("source_url"):
        bib.append(f"[View on PubMed]({payload['source_url']})")
    if payload.get("pmc_url"):
        bib.append(f"[PMC full text]({payload['pmc_url']})")
    if bib:
        st.markdown(" · ".join(bib))

    # Full-text availability badge.
    n_full = len(payload.get("full_text_sections") or [])
    if payload.get("full_text_available"):
        st.markdown(f"✅ **Full text retrieved** from PMC — {n_full} sections.")
    else:
        st.markdown("ℹ️ **Abstract only** — no open-access full text in PMC for this article.")

    authors = payload.get("authors") or []
    if authors:
        st.markdown(f"**Authors:** {', '.join(authors)}")

    if meta.get("fallback"):
        st.warning(
            "Nano structuring was unavailable — showing a deterministic structuring "
            f"of the raw record. ({meta.get('error', 'no detail')})"
        )

    # Plain-language summary.
    if payload.get("plain_summary"):
        st.markdown("### 📝 Plain-language summary")
        st.info(payload["plain_summary"])

    # Key findings.
    findings = payload.get("key_findings") or []
    if findings:
        st.markdown("### 🔑 Key findings")
        for f in findings:
            st.markdown(f"- {f}")

    # Per-section summaries (nano, full text only).
    summaries = payload.get("section_summaries") or []
    if summaries:
        st.markdown("### 🧭 Section-by-section summary")
        for s in summaries:
            section = (s.get("section") or "").strip()
            summary = (s.get("summary") or "").strip()
            if summary:
                st.markdown(f"**{section or 'Section'}** — {summary}")

    # Abstract sections.
    sections = payload.get("abstract_sections") or []
    if sections:
        st.markdown("### 📄 Abstract")
        for sec in sections:
            label = (sec.get("label") or "").strip()
            text = (sec.get("text") or "").strip()
            if not text:
                continue
            if label:
                st.markdown(f"**{label.title()}**")
            st.write(text)

    # Verbatim full text (collapsed — can be long).
    full_sections = payload.get("full_text_sections") or []
    if full_sections:
        with st.expander(f"📚 Full text — verbatim from PMC ({len(full_sections)} sections)"):
            for sec in full_sections:
                title = (sec.get("title") or "").strip()
                text = (sec.get("text") or "").strip()
                if not text:
                    continue
                if title:
                    st.markdown(f"#### {title}")
                st.write(text)

    # Indexing terms.
    mesh = payload.get("mesh_terms") or []
    keywords = payload.get("keywords") or []
    if mesh or keywords:
        c1, c2 = st.columns(2)
        with c1:
            if mesh:
                st.markdown("**MeSH terms**")
                st.write(", ".join(mesh))
        with c2:
            if keywords:
                st.markdown("**Author keywords**")
                st.write(", ".join(keywords))

    if meta.get("elapsed_seconds") is not None:
        st.caption(f"Structuring took {meta['elapsed_seconds']}s · response id: {meta.get('response_id') or 'n/a'}")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
st.title("🔬 PubMed PMID Collector")
st.caption(
    "Fetch a PubMed article by PMID, structure it with GPT-5.4-nano, store as JSON, and read it."
)

with st.form("collect"):
    pmid_in = st.text_input("PMID", placeholder="e.g. 42101477")
    want_full_text = st.checkbox(
        "Follow & retrieve full text (PMC, when open-access)", value=True
    )
    submitted = st.form_submit_button("Collect", type="primary")

if submitted:
    pmid = (pmid_in or "").strip()
    if not pmid:
        st.error("Enter a PMID first.")
        st.stop()
    if not openai_key:
        st.warning(
            "No OpenAI key set — the nano pass will fall back to deterministic "
            "structuring. Add a key in the sidebar for the full pattern."
        )

    try:
        spin_msg = (
            f"Fetching PMID {pmid} + full text from NCBI…"
            if want_full_text else f"Fetching PMID {pmid} from NCBI…"
        )
        with st.spinner(spin_msg):
            record = fetch_pubmed_record(
                pmid,
                include_full_text=want_full_text,
                api_key=ncbi_key or None,
                tool=ncbi_tool or None,
                email=ncbi_email or None,
            )
    except PubMedFetchError as exc:
        st.error(str(exc))
        st.stop()

    with st.spinner(f"Structuring with {model}…"):
        payload = structure_record(
            record,
            api_key=openai_key or "missing",
            model=model,
        )

    # Persist the JSON artifact.
    safe_pmid = re.sub(r"\D", "", payload.get("pmid", "record")) or "record"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = DATA_DIR / f"pmid_{safe_pmid}_{stamp}.json"
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    st.success(f"Collected and saved → `{out_path.relative_to(Path(__file__).parent)}`")

    render_record(payload)

    st.divider()
    dl_col, raw_col = st.columns([1, 3])
    with dl_col:
        st.download_button(
            "⬇️ Download JSON",
            data=json.dumps(payload, indent=2, ensure_ascii=False),
            file_name=out_path.name,
            mime="application/json",
        )
    with raw_col:
        with st.expander("View raw JSON"):
            st.json(payload)
