from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from litsearch.engineering import (CONTEXT, audit_report, canonical_url, digest, load_protocol,
                                  main, quantity_hints, search_crossref, text_digest)
from litsearch.models import Record
from litsearch.net import NetworkError
from litsearch.verification import MetadataStore, verify_record


class JournalClient:
    """Bibliographic regression only; not a live registry response or experiment."""
    def __init__(self, journal="ACS Omega", doi="10.1021/acsomega.4c09272", year=2025):
        self.journal, self.doi, self.year = journal, doi, year

    def get_json(self, url):
        if "works?" in url:
            return {"message": {"total-results": 0, "items": []}}
        if "api.crossref.org/works/" in url:
            return {"message": {"DOI": self.doi, "title": ["Regression title"],
                                "container-title": [self.journal], "published": {"date-parts": [[self.year]]}}}
        return {"hitCount": 0, "resultList": {"result": []}}


class BibliographyRegressionTests(unittest.TestCase):
    def test_two_real_wrong_journals_are_blocked(self):
        for journal, doi, year in [("ACS Omega", "10.1021/acsomega.4c09272", 2025),
                                   ("Nanomaterials", "10.3390/nano11061408", 2021)]:
            with self.subTest(journal=journal), tempfile.TemporaryDirectory() as td:
                rec = Record(title="Regression title", doi=doi, year=str(year), journal="Micromachines")
                verify_record(rec, MetadataStore(Path(td), JournalClient(journal, doi, year)))
                self.assertEqual(rec.identity_status, "metadata_conflict")
                self.assertEqual(rec.journal, "Micromachines")
                self.assertEqual(rec.identity_checks[0]["remote_journal"], journal)

    def test_correct_journal_and_punctuation_pass(self):
        with tempfile.TemporaryDirectory() as td:
            rec = Record(title="Regression title", doi="10.1021/acsomega.4c09272", journal="ACS   Omega.")
            verify_record(rec, MetadataStore(Path(td), JournalClient()))
            self.assertEqual(rec.identity_status, "verified")

    def test_doi_suffix_year_is_not_publication_year(self):
        with tempfile.TemporaryDirectory() as td:
            doi = "10.1016/j.apsusc.2025.164983"
            rec = Record(title="Regression title", doi=doi, year="2026", journal="Applied Surface Science")
            verify_record(rec, MetadataStore(Path(td), JournalClient("Applied Surface Science", doi, 2026)))
            self.assertEqual(rec.identity_status, "verified")


class EngineeringAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.doc = self.root / "draft.md"
        self.catalog = self.root / "catalog.json"
        self.source = {"id": "s1", "citation": "Regression source", "journal": "Nanomaterials",
                       "doi": "10.3390/nano11061408", "urls": ["https://example.org/source"],
                       "kind": "research", "access_level": "full_text", "audit_note": "test fixture"}
        self.write_catalog()
        self.protocol = load_protocol("wet_etch")
        self.paragraph = "Ge removal 3.7 nm/cycle at 20 °C. [1]"
        self.doc.write_text(self.paragraph + "\n参考文献\n[1] Nanomaterials. https://example.org/source\n", encoding="utf-8")

    def write_catalog(self):
        self.catalog.write_text(json.dumps({"schema_version": 1, "sources": [self.source]}), encoding="utf-8")

    def ledger(self):
        snap = self.root / "source.txt"
        snap.write_text(self.paragraph, encoding="utf-8")
        c = {"paragraph_sha256": text_digest(self.paragraph), "source_ids": ["s1"],
             "context": {k: "explicit fixture" for k in CONTEXT},
             "quote": self.paragraph, "locator": "fixture section 1", "snapshot_path": "source.txt",
             "snapshot_sha256": digest(snap), "source_url": "https://example.org/source"}
        c["context"].update(value="3.7", unit="nm/cycle", metric="planar_epc")
        p = self.root / "claims.json"
        p.write_text(json.dumps({"schema_version": 1, "document_sha256": digest(self.doc), "checks": [c]}), encoding="utf-8")
        return p

    def test_engineering_units_detected_but_identifiers_ignored(self):
        for text in ("3.7 nm/cycle", "8 Å/cycle", "155 °C", "1000 wph", "50 µm/h", "20:1", "75%"):
            self.assertTrue(quantity_hints(text), text)
        self.assertFalse(quantity_hints("DOI 10.1016/j.apsusc.2025.164983 [16]"))

    def test_scope_mentions_are_not_certified_as_complete(self):
        a = audit_report(self.doc, self.catalog, self.protocol)
        self.assertFalse(a["scientific_approval"])
        self.assertEqual(a["coverage"][6]["status"], "missing")
        self.assertEqual(a["coverage"][6]["semantic_completeness"], "not_assessed")

    def test_quantity_without_ledger_needs_review(self):
        a = audit_report(self.doc, self.catalog, self.protocol)
        self.assertIn("structured_data_check_missing", a["quantity_checks"][0]["issues"])

    def test_bound_quote_passes_only_mechanical_checks(self):
        a = audit_report(self.doc, self.catalog, self.protocol, self.ledger())
        self.assertEqual(a["quantity_checks"][0]["status"], "mechanical_checks_complete")
        self.assertFalse(a["scientific_approval"])

    def test_changed_document_invalidates_claim_ledger(self):
        p = self.ledger()
        self.doc.write_text(self.doc.read_text(encoding="utf-8") + "new paragraph", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "stale"):
            audit_report(self.doc, self.catalog, self.protocol, p)

    def test_changed_snapshot_is_detected(self):
        p = self.ledger()
        (self.root / "source.txt").write_text("modified", encoding="utf-8")
        a = audit_report(self.doc, self.catalog, self.protocol, p)
        self.assertIn("source_snapshot_missing_or_changed", a["quantity_checks"][0]["issues"])

    def test_changed_structured_value_is_not_supported_by_valid_quote(self):
        p = self.ledger()
        d = json.loads(p.read_text(encoding="utf-8"))
        d["checks"][0]["context"]["value"] = "999"
        p.write_text(json.dumps(d), encoding="utf-8")
        a = audit_report(self.doc, self.catalog, self.protocol, p)
        self.assertIn("context_value_or_unit_not_in_quote", a["quantity_checks"][0]["issues"])

    def test_bibliographic_access_cannot_support_experimental_numbers(self):
        self.source["access_level"] = "bibliographic"
        self.write_catalog()
        a = audit_report(self.doc, self.catalog, self.protocol, self.ledger())
        self.assertIn("access_level_cannot_support_data", a["quantity_checks"][0]["issues"])

    def test_not_reported_conditions_stay_visible(self):
        p = self.ledger()
        d = json.loads(p.read_text(encoding="utf-8"))
        d["checks"][0]["context"]["temperature"] = "not_reported"
        p.write_text(json.dumps(d), encoding="utf-8")
        a = audit_report(self.doc, self.catalog, self.protocol, p)
        self.assertIn("condition_or_measurement_not_reported", a["quantity_checks"][0]["issues"])

    def test_wrong_journal_and_undefined_reference_are_found(self):
        self.doc.write_text("3.7 nm/cycle [2]\n参考文献\n[1] Micromachines https://example.org/source", encoding="utf-8")
        a = audit_report(self.doc, self.catalog, self.protocol)
        self.assertIn("undefined_citation:2", a["hard_issues"])
        self.assertIn("reference_1:journal_missing_or_conflicting", a["hard_issues"])

    def test_version_query_parameters_are_not_deduplicated(self):
        self.assertNotEqual(canonical_url("https://example.org/catalog?v=2023"), canonical_url("https://example.org/catalog?v=2024"))

    def test_doi_and_url_only_duplicates_have_one_identity(self):
        self.doc.write_text("Data [1][2]\n附录 B｜参考文献与可核验原始来源\n"
                            "[1] Nanomaterials DOI 10.3390/nano11061408\n"
                            "[2] Nanomaterials https://example.org/source", encoding="utf-8")
        a = audit_report(self.doc, self.catalog, self.protocol)
        self.assertEqual(a["duplicate_groups"], [["1", "2"]])

    def test_soft_wrapped_hyperlinks_can_match_catalog(self):
        self.doc.write_text("Data [1]\n参考文献\n[1] Nanomaterials https:/\u200b/example.org/source", encoding="utf-8")
        a = audit_report(self.doc, self.catalog, self.protocol)
        self.assertEqual(a["references"][0]["source_ids"], ["s1"])

    def test_cli_returns_review_status_and_csv(self):
        out = self.root / "out"
        code = main(["audit", "--document", str(self.doc), "--catalog", str(self.catalog), "--out-dir", str(out)])
        self.assertEqual(code, 2)
        self.assertTrue((out / "quantity_checks.csv").exists())
        self.assertFalse(json.loads((out / "run_manifest.json").read_text(encoding="utf-8"))["network_used"])


def crossref_page(ids, total, cursor="next"):
    return {"message": {"total-results": total, "next-cursor": cursor,
                        "query": {"search-terms": "semiconductor wet etching reaction kinetics mass transport"},
                        "items": [{"DOI": "10.1234/fixture" + str(n), "title": ["Synthetic item " + str(n)]} for n in ids]}}


class PagesClient:
    def __init__(self, pages):
        self.pages = iter(pages)
        self.urls = []

    def get_json(self, url):
        self.urls.append(url)
        p = next(self.pages)
        if isinstance(p, Exception):
            raise p
        return p


class EngineeringSearchTests(unittest.TestCase):
    def search(self, pages, budget=3):
        with tempfile.TemporaryDirectory() as td:
            p = copy.deepcopy(load_protocol("wet_etch"))
            p["facets"] = p["facets"][:1]
            result = search_crossref(p, PagesClient(pages), Path(td), budget, 2)
            raws = sorted(x.name for x in Path(td).glob("raw_*.json"))
            return result, raws

    def test_budget_truncation_is_explicit(self):
        r, _ = self.search([crossref_page([1, 2], 100), crossref_page([3], 100)])
        self.assertEqual(r["status"], "budget_truncated")
        self.assertEqual(r["candidate_count"], 3)
        self.assertFalse(r["identity_verified"])

    def test_later_failure_preserves_earlier_candidate_checkpoint(self):
        r, paths = self.search([crossref_page([1, 2], 4), NetworkError("fixture 403", status=403)])
        self.assertEqual(r["status"], "failed")
        self.assertEqual(r["candidate_count"], 2)
        self.assertIn("raw_records.json", paths)

    def test_offset_pagination_can_have_different_pages(self):
        r, _ = self.search([crossref_page([1, 2], 3, "same"), crossref_page([3], 3, "same")])
        self.assertEqual(r["status"], "complete_for_queries")

    def test_repeated_doi_page_is_failure(self):
        r, _ = self.search([crossref_page([1, 2], 4), crossref_page([1, 2], 4)], 4)
        self.assertEqual(r["status"], "failed")

    def test_short_incomplete_page_is_not_success(self):
        r, _ = self.search([crossref_page([1], 100)])
        self.assertEqual(r["status"], "failed")

    def test_invalid_registry_shape_is_preserved_and_failed(self):
        r, paths = self.search([{"message": {"items": "bad", "total-results": 1}}])
        self.assertEqual(r["status"], "failed")
        self.assertIn("raw_mechanism_001.json", paths)

    def test_silently_ignored_query_is_rejected(self):
        p = crossref_page([1, 2], 100)
        p["message"]["query"]["search-terms"] = None
        r, _ = self.search([p])
        self.assertEqual(r["status"], "failed")
        self.assertEqual(r["candidate_count"], 0)

    def test_search_explicitly_ranks_by_relevance_and_uses_offsets(self):
        from urllib.parse import parse_qs, urlsplit
        with tempfile.TemporaryDirectory() as td:
            p = copy.deepcopy(load_protocol("wet_etch")); p["facets"] = p["facets"][:1]
            client = PagesClient([crossref_page([1, 2], 3), crossref_page([3], 3)])
            search_crossref(p, client, Path(td), 3, 2)
            first, second = (parse_qs(urlsplit(u).query) for u in client.urls)
            self.assertEqual(first["sort"], ["score"])
            self.assertEqual(first["order"], ["desc"])
            self.assertEqual(second["offset"], ["2"])
            self.assertNotIn("cursor", first)


if __name__ == "__main__":
    unittest.main()
