from __future__ import annotations

import csv
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from litsearch.evidence import (
    apply_review_decisions,
    build_evidence_cards,
    review_template,
    write_review_bundle,
)
from litsearch.cli import _load_supplement_manifest
from litsearch.core import key_for
from litsearch.models import Record
from litsearch.topics import load_topic


BODY_HASH_A = "a" * 64
BODY_HASH_B = "b" * 64
SUPPLEMENT_HASH_A = "c" * 64
SUPPLEMENT_HASH_B = "d" * 64
ABSTRACT = (
    "This synthetic abstract describes an engineered microbial strain and its production."
)
RESULTS = (
    "The engineered strain increased pantothenate production relative to its control."
)


def _record(pdf_hash: str = BODY_HASH_A, supplements: list[dict] | None = None) -> Record:
    record = Record(
        title="Synthetic material binding fixture",
        doi="10.1234/synthetic-material-binding",
        abstract=ABSTRACT,
        verified_abstract=ABSTRACT,
        identity_status="verified",
        pdf_identity_status="matched",
        pdf_sha256=pdf_hash,
        results_excerpt=RESULTS,
    )
    record.relevance_status = "direct_candidate"
    if supplements is None:
        build_evidence_cards(record, load_topic())
    else:
        build_evidence_cards(record, load_topic(), supplemental_dependencies=supplements)
    return record


def _card(record: Record, source_kind: str) -> dict:
    return next(card for card in record.evidence if card["source_kind"] == source_kind)


def _decision_payload(record: Record, topic, evidence_kind: str) -> dict:
    payload = review_template([record], topic)
    payload["decisions"][0].update(
        decision="include",
        reviewer="Synthetic test reviewer",
        reason="Synthetic binding regression only",
        claims=[{
            "kind": "qualitative",
            "text": "The synthetic study reports microbial pantothenate production.",
            "evidence_ids": [_card(record, evidence_kind)["id"]],
        }],
    )
    return payload


def _apply(record: Record, topic, payload: dict) -> list[dict]:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "synthetic_decisions.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return apply_review_decisions([record], topic, path)


class PublicationBindingTests(unittest.TestCase):
    def setUp(self):
        self.topic = load_topic()

    def test_pdf_hash_change_invalidates_old_evidence_decision(self):
        original = _record(BODY_HASH_A)
        old_decision = _decision_payload(original, self.topic, "pdf_results")
        self.assertEqual(len(_apply(original, self.topic, old_decision)), 1)

        changed_body = _record(BODY_HASH_B)
        self.assertEqual(changed_body.results_excerpt, original.results_excerpt)
        self.assertEqual(_apply(changed_body, self.topic, old_decision), [])
        self.assertEqual(changed_body.review_status, "blocked_evidence")

    def test_supplement_hash_change_invalidates_old_evidence_decision(self):
        original = _record(supplements=[{
            "role": "supplemental_table", "sha256": SUPPLEMENT_HASH_A,
        }])
        old_decision = _decision_payload(original, self.topic, "pdf_results")
        self.assertEqual(len(_apply(original, self.topic, old_decision)), 1)

        changed_supplement = _record(supplements=[{
            "role": "supplemental_table", "sha256": SUPPLEMENT_HASH_B,
        }])
        self.assertEqual(changed_supplement.results_excerpt, original.results_excerpt)
        self.assertEqual(_apply(changed_supplement, self.topic, old_decision), [])
        self.assertEqual(changed_supplement.review_status, "blocked_evidence")

    def test_supplement_role_is_bound_and_dependency_order_is_stable(self):
        first = _record(supplements=[
            {"role": "supplemental_table", "sha256": SUPPLEMENT_HASH_A},
            {"role": "supplemental_figure", "sha256": SUPPLEMENT_HASH_B},
        ])
        reversed_order = _record(supplements=[
            {"role": "supplemental_figure", "sha256": SUPPLEMENT_HASH_B},
            {"role": "supplemental_table", "sha256": SUPPLEMENT_HASH_A},
        ])
        self.assertEqual(_card(first, "pdf_results")["id"], _card(reversed_order, "pdf_results")["id"])

        changed_role = _record(supplements=[
            {"role": "supplemental_dataset", "sha256": SUPPLEMENT_HASH_A},
            {"role": "supplemental_figure", "sha256": SUPPLEMENT_HASH_B},
        ])
        old_decision = _decision_payload(first, self.topic, "pdf_results")
        self.assertEqual(_apply(changed_role, self.topic, old_decision), [])
        self.assertEqual(changed_role.review_status, "blocked_evidence")

    def test_repeated_build_with_same_material_inputs_keeps_id_stable(self):
        first = _record(supplements=[{
            "role": "supplemental_table", "sha256": SUPPLEMENT_HASH_A,
        }])
        second = _record(supplements=[{
            "role": "supplemental_table", "sha256": SUPPLEMENT_HASH_A,
        }])
        self.assertEqual(first.evidence, second.evidence)

    def test_extractor_version_change_invalidates_old_evidence_decision(self):
        with patch("litsearch.evidence.EVIDENCE_EXTRACTOR_VERSION", "synthetic-v1", create=True):
            original = _record()
        old_decision = _decision_payload(original, self.topic, "pdf_results")
        self.assertEqual(len(_apply(original, self.topic, old_decision)), 1)

        with patch("litsearch.evidence.EVIDENCE_EXTRACTOR_VERSION", "synthetic-v2", create=True):
            changed_version = _record()
        self.assertEqual(_apply(changed_version, self.topic, old_decision), [])
        self.assertEqual(changed_version.review_status, "blocked_evidence")

    def test_abstract_decision_is_independent_of_pdf_and_supplement_changes(self):
        original = _record(BODY_HASH_A, supplements=[{
            "role": "supplemental_table", "sha256": SUPPLEMENT_HASH_A,
        }])
        old_decision = _decision_payload(original, self.topic, "verified_abstract")
        self.assertEqual(len(_apply(original, self.topic, old_decision)), 1)

        changed_pdf = _record(BODY_HASH_B, supplements=[{
            "role": "supplemental_table", "sha256": SUPPLEMENT_HASH_B,
        }])
        self.assertEqual(
            _card(changed_pdf, "verified_abstract")["id"],
            _card(original, "verified_abstract")["id"],
        )
        self.assertEqual(len(_apply(changed_pdf, self.topic, old_decision)), 1)
        self.assertEqual(changed_pdf.review_status, "approved")

    def test_missing_required_material_hash_cannot_be_approved(self):
        record = _record(supplements=[{
            "role": "supplemental_table",
        }])
        card = _card(record, "pdf_results")
        self.assertFalse(card["eligible"])
        payload = _decision_payload(record, self.topic, "pdf_results")
        self.assertEqual(_apply(record, self.topic, payload), [])
        self.assertEqual(record.review_status, "blocked_evidence")

        missing_body_hash = _record(pdf_hash="", supplements=[])
        body_card = _card(missing_body_hash, "pdf_results")
        self.assertFalse(body_card["eligible"])
        body_payload = _decision_payload(missing_body_hash, self.topic, "pdf_results")
        self.assertEqual(_apply(missing_body_hash, self.topic, body_payload), [])
        self.assertEqual(missing_body_hash.review_status, "blocked_evidence")

    def test_cli_supplement_manifest_hashes_the_explicit_local_file(self):
        record = _record()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            material_path = root / "synthetic-supplement.bin"
            material_bytes = b"synthetic supplemental material fixture only"
            material_path.write_bytes(material_bytes)
            manifest_path = root / "supplements.json"
            manifest_path.write_text(json.dumps({
                "schema_version": 1,
                "materials": [{
                    "record_key": key_for(record),
                    "role": "supplemental_table",
                    "path": material_path.name,
                }],
            }), encoding="utf-8")

            dependencies = _load_supplement_manifest(manifest_path, [record])

            self.assertEqual(dependencies, {
                key_for(record): [{
                    "role": "supplemental_table",
                    "sha256": hashlib.sha256(material_bytes).hexdigest(),
                }],
            })

            manifest_path.write_text(json.dumps({
                "schema_version": 1,
                "materials": [{
                    "record_key": key_for(record),
                    "role": "supplemental_table",
                    "path": material_path.name,
                    "sha256": "e" * 64,
                }],
            }), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "SHA-256由CLI计算"):
                _load_supplement_manifest(manifest_path, [record])

    def test_real_review_bundle_round_trip_preserves_binding_and_f3_fields(self):
        record = _record(supplements=[{
            "role": "supplemental_table", "sha256": SUPPLEMENT_HASH_A,
        }])
        card = _card(record, "pdf_results")
        card["material_grade"] = "SYNTHETIC_BODY_MATERIAL_GRADE"
        card["comparability"] = "SYNTHETIC_NOT_COMPARABLE"
        payload = _decision_payload(record, self.topic, "pdf_results")
        claim = payload["decisions"][0]["claims"][0]
        claim.update(
            context=None,
            material_grade="SYNTHETIC_CLAIM_MATERIAL_GRADE",
            comparability="SYNTHETIC_CLAIM_COMPARABILITY",
        )

        with tempfile.TemporaryDirectory() as directory:
            decisions_path = Path(directory) / "decisions.json"
            output = Path(directory) / "bundle"
            output.mkdir()
            decisions_path.write_text(json.dumps(payload), encoding="utf-8")
            approved = apply_review_decisions([record], self.topic, decisions_path)
            write_review_bundle(output, [record], self.topic, approved)

            ready = json.loads((output / "review_ready.json").read_text(encoding="utf-8"))
            cards_bundle = json.loads((output / "evidence_cards.json").read_text(encoding="utf-8"))
            markdown = (output / "review_materials.md").read_text(encoding="utf-8")
            with (output / "review_claims.csv").open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))

        self.assertEqual(len(approved), 1)
        self.assertEqual(ready, approved)
        self.assertEqual(len(rows), 1)
        ready_claim = ready[0]["claims"][0]
        ready_card = ready_claim["evidence"][0]
        self.assertEqual(ready_card["material_dependencies"], [
            {"role": "body_pdf", "sha256": BODY_HASH_A},
            {"role": "supplemental_table", "sha256": SUPPLEMENT_HASH_A},
        ])
        self.assertEqual(ready_card["extractor_version"], "1.0.0")
        self.assertEqual(ready_claim["material_grade"], "SYNTHETIC_CLAIM_MATERIAL_GRADE")
        self.assertEqual(ready_claim["comparability"], "SYNTHETIC_CLAIM_COMPARABILITY")
        self.assertIsNone(ready_claim["context"])
        self.assertEqual(cards_bundle[0]["cards"], record.evidence)
        self.assertEqual(json.loads(rows[0]["claim_json"]), ready_claim)
        self.assertEqual(json.loads(rows[0]["evidence_json"]), ready_claim["evidence"])
        self.assertIn("Context：显式 null", markdown)
        self.assertIn(r"claim.material_grade: SYNTHETIC\_CLAIM\_MATERIAL\_GRADE", markdown)
        self.assertIn(r"evidence.material_grade: SYNTHETIC\_BODY\_MATERIAL\_GRADE", markdown)
        self.assertIn(BODY_HASH_A, markdown)
        self.assertIn(SUPPLEMENT_HASH_A, markdown)
        self.assertIn("extractor_version: 1.0.0", markdown)


if __name__ == "__main__":
    unittest.main()
