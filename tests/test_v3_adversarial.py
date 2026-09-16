from __future__ import annotations

import copy
import json
import socket
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from litsearch.cli import _load_records, build_parser, run
from litsearch.core import (classify_and_score, contains_term, extract_evidence, extract_results_like,
                            merge_records, norm_doi, safe_pdf_name, title_key)
from litsearch.evidence import apply_review_decisions, build_evidence_cards, review_template
from litsearch.models import AuditEvent, Record
from litsearch.net import NetworkError, _public_connection
from litsearch.outputs import _md_text, excel_safe
from litsearch.pdfs import pdf_matches_record
from litsearch.sources import fetch_europepmc
from litsearch.topics import load_topic
from litsearch.verification import MetadataStore, verify_record, _metadata_conflicts, _publication_years


TITLE = "Metabolic engineering of Escherichia coli for pantothenate production"
DOI = "10.1234/test-work"
ABSTRACT = "Engineered Escherichia coli produced pantothenate at a titer of 12 g/L in batch fermentation."


class RegistryClient:
    """Synthetic registry fixtures, deliberately NOT real citations."""
    def __init__(self, *, title: str = TITLE, absent: bool = False, unavailable: bool = False,
                 retract: bool = False, updates_fail: bool = False, correction: bool = False) -> None:
        self.title, self.absent, self.unavailable = title, absent, unavailable
        self.retract, self.updates_fail, self.correction = retract, updates_fail, correction
        self.calls = 0

    def get_json(self, url: str) -> dict:
        self.calls += 1
        if self.unavailable:
            raise NetworkError("temporary service failure", status=503)
        if "api.crossref.org/works?" in url:
            if self.updates_fail:
                raise NetworkError("updates endpoint unavailable", status=503)
            if self.retract or self.correction:
                return {"message": {"total-results": 1, "items": [{"update-to": [
                    {"DOI": DOI, "type": "retraction" if self.retract else "correction"}]}]}}
            return {"message": {"total-results": 0, "items": []}}
        if "api.crossref.org/works/" in url:
            if self.absent:
                raise NetworkError("not a Crossref DOI", status=404)
            return {"message": {"DOI": DOI, "title": [self.title], "type": "journal-article",
                                "published": {"date-parts": [[2025]]}, "author": [{"given": "A", "family": "Smith"}]}}
        if self.absent:
            return {"hitCount": 0, "resultList": {"result": []}}
        return {"hitCount": 1, "resultList": {"result": [{"id": "123", "source": "MED", "doi": DOI,
                 "title": self.title, "pubYear": "2025", "abstractText": ABSTRACT,
                 "authorList": {"author": [{"fullName": "Smith A"}]},
                 "pubTypeList": {"pubType": ["Journal Article"]}}]}}


class IdentityTests(unittest.TestCase):
    def test_typeset_subscript_does_not_create_false_title_conflict(self):
        a = Record(title="Microbial production of vitamin B5", doi=DOI)
        b = Record(title="Microbial production of vitamin B (5)", doi=DOI)
        self.assertEqual(_metadata_conflicts(a, b), [])

    def test_online_print_year_difference_requires_explicit_dates(self):
        a = Record(title=TITLE, doi=DOI, year="2024")
        b = Record(title=TITLE, doi=DOI, year="2025")
        years = _publication_years({"pubYear": "2025", "firstPublicationDate": "2024-12-20"})
        self.assertEqual(_metadata_conflicts(a, b, years), [])
        self.assertTrue(_metadata_conflicts(a, b))

    def test_deposit_and_index_dates_are_not_publication_dates(self):
        self.assertEqual(_publication_years({"created": {"date-parts": [[2024]]}, "firstIndexDate": "2024-01-01"}), set())

    def verify(self, client: RegistryClient, record: Record | None = None) -> Record:
        rec = record or Record(title=TITLE, doi=DOI, year="2025", authors=["A Smith"])
        with tempfile.TemporaryDirectory() as directory:
            verify_record(rec, MetadataStore(Path(directory), client))
        return rec

    def test_real_identifier_with_wrong_title_is_quarantined(self):
        rec = self.verify(RegistryClient(), Record(title="Fabricated incredible pantothenate study", doi=DOI))
        self.assertEqual(rec.identity_status, "metadata_conflict")

    def test_matching_metadata_is_verified_and_abstract_is_authoritative(self):
        rec = self.verify(RegistryClient(), Record(title=TITLE, doi=DOI, abstract="invented 999 g/L"))
        self.assertEqual(rec.identity_status, "verified")
        self.assertEqual(rec.verified_abstract, ABSTRACT)
        build_evidence_cards(rec, load_topic())
        self.assertNotIn("999", json.dumps(rec.evidence))

    def test_404_does_not_mean_fabricated(self):
        rec = self.verify(RegistryClient(absent=True))
        self.assertEqual(rec.identity_status, "not_indexed")

    def test_transient_error_stays_unverified(self):
        self.assertEqual(self.verify(RegistryClient(unavailable=True)).identity_status, "unverified")

    def test_retraction_blocks_verified_status(self):
        self.assertEqual(self.verify(RegistryClient(retract=True)).identity_status, "retracted")

    def test_correction_requires_manual_resolution(self):
        self.assertEqual(self.verify(RegistryClient(correction=True)).identity_status, "publication_update")

    def test_update_service_failure_does_not_silently_clear_retraction_risk(self):
        self.assertEqual(self.verify(RegistryClient(updates_fail=True)).identity_status, "verified_updates_unknown")

    def test_year_mismatch_is_not_silently_overwritten(self):
        self.assertEqual(self.verify(RegistryClient(), Record(title=TITLE, doi=DOI, year="1995")).identity_status, "metadata_conflict")

    def test_mismatched_pmid_is_detected(self):
        self.assertEqual(self.verify(RegistryClient(), Record(title=TITLE, doi=DOI, pmid="999")).identity_status, "metadata_conflict")

    def test_no_identifiers_never_verified_by_title_search(self):
        client = RegistryClient()
        self.assertEqual(self.verify(client, Record(title=TITLE)).identity_status, "unverified")
        self.assertEqual(client.calls, 0)

    def test_invalid_pmid_is_not_query_injection(self):
        client = RegistryClient()
        self.assertEqual(self.verify(client, Record(title=TITLE, pmid="1 OR *")).identity_status, "invalid_identifier")
        self.assertEqual(client.calls, 0)


class CacheTests(unittest.TestCase):
    def test_cache_offline_replay_and_tamper_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = RegistryClient()
            live = MetadataStore(root, client)
            url = "https://api.crossref.org/works/10.1234%2Ftest-work"
            data, _ = live.get(url)
            offline = MetadataStore(root, client, offline=True)
            self.assertEqual(offline.get(url)[0], data)
            self.assertEqual(client.calls, 1)
            path = next(root.glob("*.json"))
            snapshot = json.loads(path.read_text())
            snapshot["data"]["message"]["title"] = ["tampered"]
            path.write_text(json.dumps(snapshot), encoding="utf-8")
            with self.assertRaises(NetworkError):
                MetadataStore(root, client, offline=True).get(url)

    def test_expired_cache_cannot_verify_offline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            url = "https://api.crossref.org/works/10.1234%2Ftest-work"
            MetadataStore(root, RegistryClient()).get(url)
            path = next(root.glob("*.json"))
            data = json.loads(path.read_text())
            data["fetched_at_utc"] = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(NetworkError):
                MetadataStore(root, RegistryClient(), offline=True).get(url)


class TopicAndDedupTests(unittest.TestCase):
    def test_switch_topics_without_global_state_or_glutathione_blacklist(self):
        for topic_name, product, host in [("pantothenate", "pantothenate", "Escherichia coli"),
                                         ("lysine", "lysine", "Corynebacterium glutamicum"),
                                         ("glutathione", "glutathione", "Saccharomyces cerevisiae")]:
            rec = Record(title=f"Metabolic engineering of {host} for {product} production")
            classify_and_score(rec, load_topic(topic_name))
            self.assertEqual(rec.relevance_status, "direct_candidate")
            self.assertEqual(rec.topic_id, topic_name)
        classify_and_score(rec, load_topic("lysine"))
        self.assertEqual(rec.relevance_status, "out_of_scope")

    def test_phrase_boundaries_and_nonlatin_titles(self):
        self.assertFalse(contains_term("pantothenic acidification", "pantothenic acid"))
        self.assertNotEqual(title_key("微生物泛酸生产"), title_key("微生物赖氨酸生产"))
        self.assertNotEqual(title_key("word " * 250 + "A"), title_key("word " * 250 + "B"))

    def test_doi_normalization_does_not_extract_from_garbage_or_truncate_legacy(self):
        self.assertEqual(norm_doi("garbage 10.1234/foo"), "")
        self.assertEqual(norm_doi("10.1002/(SICI)123<1::AID-X>3.0.CO;2-1"), "10.1002/(sici)123<1::aid-x>3.0.co;2-1")

    def test_same_doi_different_title_preserved_and_inputs_unchanged(self):
        items = [Record(title=TITLE, doi=DOI), Record(title="Different work", doi=DOI)]
        before = copy.deepcopy(items)
        result = merge_records(items)
        self.assertEqual(len(result), 2)
        self.assertTrue(all(r.processing_warnings for r in result))
        self.assertEqual(items, before)
        self.assertEqual(result, merge_records(list(reversed(items))))

    def test_title_only_ambiguous_identifiers_stay_separate(self):
        items = [Record(title=TITLE, doi=DOI), Record(title=TITLE)]
        self.assertEqual(len(merge_records(items)), 2)

    def test_pdf_name_collision_and_length(self):
        self.assertNotEqual(safe_pdf_name(Record(title=TITLE, doi="10.1234/a:b")),
                            safe_pdf_name(Record(title=TITLE, doi="10.1234/a_b")))
        self.assertLess(len(safe_pdf_name(Record(title=TITLE, doi="10.1234/" + "a" * 400))), 160)

    def test_gene_mention_does_not_become_intervention(self):
        rec = Record(title=TITLE, abstract="PanC was not overexpressed. Glucose reached 80 g/L and pantothenate reached 2 g/L.")
        extract_evidence(rec)
        self.assertNotIn("强化 PanC", rec.method_result_cn)
        self.assertNotIn("80", rec.final_yield)
        self.assertNotIn("2 g/L", rec.final_yield)

    def test_results_repeated_heading_keeps_both_pages_and_stops_at_methods(self):
        text = "Results\n" + ABSTRACT * 2 + "\n[[PAGE 2]]\nResults\n" + ABSTRACT + "\nMethods\nnot evidence"
        result = extract_results_like(text)
        self.assertEqual(result.count(ABSTRACT), 3)
        self.assertNotIn("not evidence", result)

    def test_nonbreaking_spaces_in_pdf_results_heading(self):
        text = "Introduction\ntext\nResults and\u00a0discussion\n" + ABSTRACT + "\nReferences\nnot evidence"
        self.assertIn(ABSTRACT, extract_results_like(text))
        self.assertNotIn("not evidence", extract_results_like(text))

    def test_pdf_doi_typographic_dash_can_match_without_relaxing_title(self):
        rec = Record(title=TITLE, doi=DOI)
        self.assertTrue(pdf_matches_record(rec, TITLE + "\n" + DOI.replace("-", "–")))
        self.assertFalse(pdf_matches_record(Record(title="Another distinct paper with a sufficiently long title", doi=DOI), TITLE + "\n" + DOI))


class ReviewGateTests(unittest.TestCase):
    def setUp(self):
        self.topic = load_topic()
        self.rec = Record(title=TITLE, doi=DOI, identity_status="verified", verified_abstract=ABSTRACT)
        classify_and_score(self.rec, self.topic)
        build_evidence_cards(self.rec, self.topic)
        self.payload = review_template([self.rec], self.topic)
        self.payload["decisions"][0].update(decision="include", reviewer="Test reviewer", reason="Target production studied",
                                            claims=[{"kind": "qualitative", "text": "The study concerns microbial pantothenate production.",
                                                     "evidence_ids": [self.rec.evidence[0]["id"]]}])

    def apply(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "decisions.json"
            path.write_text(json.dumps(self.payload), encoding="utf-8")
            return apply_review_decisions([self.rec], self.topic, path)

    def test_approved_evidence_requires_explicit_human_decision(self):
        self.assertEqual(apply_review_decisions([self.rec], self.topic, None), [])
        self.assertEqual(len(self.apply()), 1)
        self.assertEqual(self.rec.review_status, "approved")

    def test_invalid_evidence_id_blocked(self):
        self.payload["decisions"][0]["claims"][0]["evidence_ids"] = ["invented"]
        self.assertEqual(self.apply(), [])
        self.assertEqual(self.rec.review_status, "blocked_evidence")

    def test_review_cannot_override_identity_conflict(self):
        self.rec.identity_status = "metadata_conflict"
        self.assertEqual(self.apply(), [])
        self.assertEqual(self.rec.review_status, "blocked_identity")

    def test_cross_topic_decision_rejected(self):
        self.payload["topic_hash"] = load_topic("lysine").fingerprint
        with self.assertRaises(ValueError):
            self.apply()

    def test_stale_record_decision_rejected(self):
        self.rec.abstract = "changed imported abstract"
        with self.assertRaises(ValueError):
            self.apply()

    def test_quantitative_claim_requires_context_and_metric(self):
        self.payload["decisions"][0]["claims"][0]["kind"] = "quantitative"
        with self.assertRaises(ValueError):
            self.apply()

    def test_numeric_claim_cannot_disguise_as_qualitative(self):
        self.payload["decisions"][0]["claims"][0]["text"] = "Pantothenate reached 12 g/L."
        with self.assertRaises(ValueError):
            self.apply()

    def test_quantitative_claim_with_explicit_context_can_be_reviewed(self):
        claim = self.payload["decisions"][0]["claims"][0]
        claim.update(kind="quantitative", text="Pantothenate titer was 12 g/L.", context={
            "product": "pantothenate", "host": "Escherichia coli", "strain": "not_reported",
            "intervention": "engineered", "comparator": "not_reported", "cultivation": "batch fermentation",
            "medium": "not_reported", "metric": "titer", "value": "12", "unit": "g/L", "time": "not_reported",
            "scale": "not_reported", "limitations": "Synthetic fixture; abstract-only assertion, not a real finding"})
        self.assertEqual(len(self.apply()), 1)

    def test_role_prevents_review_from_becoming_primary_experiment(self):
        self.rec.relevance_status = "background_review"
        self.assertEqual(self.apply(), [])
        self.assertEqual(self.rec.review_status, "blocked_document_type")

    def test_supplement_pdf_is_not_parent_paper(self):
        text = "[[PAGE 1]]\nSupporting information\n" + TITLE + "\n" + DOI
        self.assertFalse(pdf_matches_record(self.rec, text))

    def test_imported_verified_flag_and_excerpt_are_not_trusted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "records.json"
            self.rec.results_excerpt = "fake result"
            path.write_text(json.dumps([self.rec.to_dict()]), encoding="utf-8")
            loaded = _load_records(path)[0]
            self.assertEqual(loaded.identity_status, "unverified")
            self.assertFalse(loaded.results_excerpt)
            self.assertFalse(loaded.evidence)

    def test_doi_in_references_cannot_authenticate_wrong_pdf(self):
        text = "[[PAGE 1]]\nUnrelated study\n" + "irrelevant text " * 200 + f"\nReferences\n{TITLE}\n{DOI}"
        self.assertFalse(pdf_matches_record(self.rec, text))


class PaginationAndSecurityTests(unittest.TestCase):
    def test_epmc_pagination_and_cap_are_reported(self):
        class Client:
            calls = 0
            def get_json(self, url):
                self.calls += 1
                return {"hitCount": 5, "nextCursorMark": str(self.calls), "resultList": {"result": [
                    {"id": str(self.calls), "source": "MED", "title": TITLE}]}}
        client, events = Client(), []
        result = fetch_europepmc(2, client=client, topic=load_topic(), events=events, sleeper=lambda _: None)
        self.assertEqual(len(result), 2)
        self.assertEqual(events[-1].status, "truncated")

    def test_epmc_loop_is_failure_not_success(self):
        class Client:
            def get_json(self, url):
                return {"hitCount": 5, "nextCursorMark": "*", "resultList": {"result": [
                    {"id": "1", "source": "MED", "title": TITLE}]}}
        with self.assertRaises(ValueError):
            fetch_europepmc(5, client=Client(), topic=load_topic(), events=[])

    def test_dns_alias_or_numeric_host_resolving_local_is_blocked_before_connect(self):
        entries = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))]
        with patch("litsearch.net.socket.getaddrinfo", return_value=entries), patch("litsearch.net.socket.socket") as sock:
            with self.assertRaises(NetworkError):
                _public_connection(("2130706433", 80))
            sock.assert_not_called()

    def test_dns_rebinding_uses_same_resolved_ip(self):
        entries = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 443))]
        with patch("litsearch.net.socket.getaddrinfo", return_value=entries) as resolve, patch("litsearch.net.socket.socket") as sock:
            _public_connection(("public.test", 443))
            resolve.assert_called_once()
            sock.return_value.connect.assert_called_once_with(("1.1.1.1", 443))

    def test_audit_does_not_leak_key_or_email(self):
        event = AuditEvent("network", "failed", "failed", url="https://example.org/a?api_key=secret&email=private@example.org")
        self.assertNotIn("secret", json.dumps(event.to_dict()))
        self.assertNotIn("private@", json.dumps(event.to_dict()))

    def test_markdown_and_excel_injections_are_literal(self):
        self.assertNotIn("![x](", _md_text("![x](https://evil.invalid/pixel)"))
        self.assertEqual(excel_safe("\x00=1+1"), "'=1+1")

    def test_schema_rejects_malformed_scalar_and_array_elements(self):
        for value in ({"title": TITLE, "doi": []}, {"title": TITLE, "authors": [42]},
                      {"title": TITLE, "results_pages": [True]}, {"title": TITLE, "relevance_score": True}):
            with self.assertRaises(ValueError):
                Record.from_dict(value)

    def test_nonempty_output_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "important.txt").write_text("keep")
            args = build_parser().parse_args(["--out-dir", str(root)])
            with self.assertRaisesRegex(ValueError, "输出目录"):
                run(args)
            self.assertEqual((root / "important.txt").read_text(), "keep")


if __name__ == "__main__":
    unittest.main()
