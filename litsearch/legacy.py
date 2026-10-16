#!/usr/bin/env python3
"""Compatibility API for historical workspace scripts, maintained in the source repo.

New code lives in the ``litsearch`` package.  The re-exports below keep older
workspace scripts (notably ``audit_zotero_missing_pdfs.py``) working.
"""

from litsearch.cli import main
from litsearch.core import (
    classify_and_score,
    extract_evidence,
    extract_results_like,
    find_terms,
    infer_strategy_cn,
    key_for,
    merge_records,
    norm_doi,
    norm_title,
    passes_core_relevance,
    passes_final_scope,
    safe_pdf_name,
    split_sentences,
    subsection_summary_cn,
)
from litsearch.models import Record
from litsearch.outputs import result_subsections, write_outputs, year_volume_issue
from litsearch.pdfs import (
    add_candidate_pdf_urls,
    download_pdfs,
    enrich_from_pdfs,
    extract_text_from_pdf,
    find_existing_pdf,
    link_existing_pdfs,
    parse_corresponding_author,
)
from litsearch.sources import (
    PUBMED_QUERIES,
    SEARCH_QUERIES,
    TITLE_QUERIES,
    add_pdf_url,
    fetch_openalex,
    fetch_pubmed,
    inverted_index_to_text,
    record_from_openalex,
)

__all__ = [
    "Record",
    "PUBMED_QUERIES",
    "SEARCH_QUERIES",
    "TITLE_QUERIES",
    "add_candidate_pdf_urls",
    "add_pdf_url",
    "classify_and_score",
    "download_pdfs",
    "enrich_from_pdfs",
    "extract_evidence",
    "extract_results_like",
    "extract_text_from_pdf",
    "fetch_openalex",
    "fetch_pubmed",
    "find_existing_pdf",
    "find_terms",
    "infer_strategy_cn",
    "inverted_index_to_text",
    "key_for",
    "link_existing_pdfs",
    "merge_records",
    "norm_doi",
    "norm_title",
    "parse_corresponding_author",
    "passes_core_relevance",
    "passes_final_scope",
    "record_from_openalex",
    "result_subsections",
    "safe_pdf_name",
    "split_sentences",
    "subsection_summary_cn",
    "write_outputs",
    "year_volume_issue",
]


if __name__ == "__main__":
    raise SystemExit(main())
