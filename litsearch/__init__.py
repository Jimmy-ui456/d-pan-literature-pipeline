"""Reliable, auditable literature retrieval for the D-pan project."""

from .core import (
    classify_and_score,
    extract_evidence,
    extract_results_like,
    merge_records,
    norm_doi,
    norm_title,
    passes_core_relevance,
    passes_final_scope,
)
from .models import Record

__all__ = [
    "Record",
    "classify_and_score",
    "extract_evidence",
    "extract_results_like",
    "merge_records",
    "norm_doi",
    "norm_title",
    "passes_core_relevance",
    "passes_final_scope",
]

__version__ = "3.0.0"
