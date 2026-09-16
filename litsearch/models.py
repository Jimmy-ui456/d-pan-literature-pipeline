from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import Any


@dataclass
class Record:
    """One bibliographic work after source-specific fields are normalized."""

    title: str
    doi: str = ""
    year: str = ""
    journal: str = ""
    volume: str = ""
    issue: str = ""
    pages: str = ""
    authors: list[str] = field(default_factory=list)
    abstract: str = ""
    url: str = ""
    source_ids: list[str] = field(default_factory=list)
    pdf_urls: list[str] = field(default_factory=list)
    pmid: str = ""
    pmcid: str = ""
    relevance_score: int = 0
    category: str = ""
    product: str = ""
    final_yield: str = ""
    corresponding_author: str = ""
    pdf_file: str = ""
    results_excerpt: str = ""
    method_result_cn: str = ""
    reliability_cn: str = ""
    library_tier: str = ""
    screening_reasons: list[str] = field(default_factory=list)
    results_pages: list[int] = field(default_factory=list)
    duplicate_keys: list[str] = field(default_factory=list)
    processing_warnings: list[str] = field(default_factory=list)
    identity_status: str = "unverified"
    identity_checks: list[dict[str, Any]] = field(default_factory=list)
    verified_abstract: str = ""
    topic_id: str = ""
    topic_hash: str = ""
    relevance_status: str = "unscreened"
    pdf_identity_status: str = "unverified"
    pdf_sha256: str = ""
    evidence: list[dict[str, Any]] = field(default_factory=list)
    review_status: str = "pending"
    review_notes: str = ""
    publication_types: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, item: dict[str, Any]) -> "Record":
        """Load old and new JSON rows while rejecting malformed field types."""

        allowed = {item_field.name for item_field in fields(cls)}
        values = {key: value for key, value in item.items() if key in allowed}
        if not isinstance(values.get("title"), str) or not values["title"].strip():
            raise ValueError("record.title must be a nonempty string")
        sample = cls(title="sample")
        for key, value in values.items():
            default = getattr(sample, key)
            if type(value) is not type(default):
                raise ValueError(f"record.{key} must be {type(default).__name__}")
            if isinstance(default, list):
                element_type = int if key == "results_pages" else (
                    dict if key in {"identity_checks", "evidence"} else str
                )
                if any(type(element) is not element_type for element in value):
                    raise ValueError(f"record.{key} contains invalid elements")
                if key == "results_pages" and any(page < 1 for page in value):
                    raise ValueError("results_pages must contain positive page numbers")
        return cls(**values)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def clear_derived_fields(self) -> None:
        """Drop values produced by an older pipeline run, keeping source metadata."""

        self.relevance_score = 0
        self.category = ""
        self.product = ""
        self.final_yield = ""
        self.corresponding_author = ""
        self.results_excerpt = ""
        self.method_result_cn = ""
        self.reliability_cn = ""
        self.library_tier = ""
        self.screening_reasons = []
        self.results_pages = []
        self.duplicate_keys = []
        self.processing_warnings = []
        self.clear_trust_fields()

    def clear_trust_fields(self) -> None:
        """Imported JSON is not an authority; re-establish these fields on every run."""
        self.identity_status = "unverified"
        self.identity_checks = []
        self.verified_abstract = ""
        self.topic_id = ""
        self.topic_hash = ""
        self.relevance_status = "unscreened"
        self.pdf_identity_status = "unverified"
        self.pdf_sha256 = ""
        self.evidence = []
        self.review_status = "pending"
        self.review_notes = ""


@dataclass(frozen=True)
class AuditEvent:
    stage: str
    status: str
    message: str
    source: str = ""
    record_key: str = ""
    url: str = ""

    def to_dict(self) -> dict[str, str]:
        from .net import redact_url
        values = asdict(self)
        if values["url"].startswith(("http://", "https://")):
            values["url"] = redact_url(values["url"])
        return values
