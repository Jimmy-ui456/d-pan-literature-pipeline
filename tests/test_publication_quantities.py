from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from litsearch.evidence import (
    QuantitySignal,
    apply_review_decisions,
    classify_quantity_signal,
    review_template,
)
from litsearch.models import Record
from litsearch.topics import load_topic


SYNTHETIC_EVIDENCE_ID = "synthetic-quantity-fixture"
COMPLETE_QUANTITATIVE_CONTEXT = {
    "product": "pantothenate", "host": "Escherichia coli", "strain": "not_reported",
    "intervention": "not_reported", "comparator": "not_reported", "cultivation": "not_reported",
    "medium": "not_reported", "metric": "titer", "value": "12", "unit": "g/L",
    "time": "not_reported", "scale": "not_reported", "limitations": "synthetic fixture only",
}


def make_review(text: str, *, kind: str = "qualitative", context: dict | None = None):
    topic = load_topic()
    record = Record(
        title="Synthetic quantity gate fixture",
        doi="10.1234/synthetic-quantity-fixture",
        identity_status="verified",
    )
    record.relevance_status = "direct_candidate"
    record.evidence = [{
        "id": SYNTHETIC_EVIDENCE_ID,
        "eligible": True,
        "source_kind": "synthetic_fixture",
        "quote": "Synthetic test evidence only; not a scientific finding.",
    }]
    payload = review_template([record], topic)
    claim = {
        "kind": kind,
        "text": text,
        "evidence_ids": [SYNTHETIC_EVIDENCE_ID],
    }
    if context is not None:
        claim["context"] = context
    payload["decisions"][0].update(
        decision="include",
        reviewer="Test reviewer",
        reason="Synthetic gate fixture only",
        claims=[claim],
    )
    return topic, record, payload


def apply_synthetic_review(topic, record, payload):
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "synthetic_decisions.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return apply_review_decisions([record], topic, path)


class PublicationQuantityClassificationTests(unittest.TestCase):
    def test_supported_quantity_forms_are_classified_and_block_qualitative_approval(self):
        examples = (
            "Pantothenate increased 3.5-fold.",
            "Pantothenate increased 3.5 fold.",
            "The medium contained 8 mM precursor.",
            "The substrate yield was 0.28 g/g.",
            "产量提高3.5倍。",
            "Pantothenate reached 12 gL-1.",
            "Pantothenate reached 12 gL−1.",
            "The concentration was 8 mgL-1.",
            "The concentration was 8 gl−1.",
            "The concentration was 3 mmolL-1.",
            "The concentration was 2 moll−1.",
            "The concentration was 8 mmol/L.",
            "The concentration was 2 mol/l.",
            "Production increased 3.5‐fold.",
            "Production increased 3.5‑fold.",
            "The concentration was 1.2e3 mg/L.",
            "The concentration was 1.2 × 10^3 mg/L.",
            "The titer was 12 ± 0.4 g/L.",
            "The concentration was 8–10 mM.",
            "The concentration was 8-10 mM.",
            "The increase was 25%.",
            "The reported titer was 12 g/L.",
            "In 2024, the ilv2-engineered strain's production increased by 3.5-fold.",
        )
        for text in examples:
            with self.subTest(text=text):
                self.assertIs(classify_quantity_signal(text), QuantitySignal.QUANTITATIVE)
                topic, record, payload = make_review(text)
                with self.assertRaises(ValueError) as error:
                    apply_synthetic_review(topic, record, payload)
                self.assertEqual(
                    str(error.exception),
                    "含定量信号的结论必须标为quantitative并填写完整context",
                )
                self.assertEqual(record.review_status, "pending")

    def test_year_gene_strain_and_non_numeric_qualitative_text_can_be_approved(self):
        examples = (
            "Studies published in 2024 and 2025 examined microbial production.",
            "Studies in 2024 and 2025 examined yield.",
            "The panB gene and ilv2 pathway were discussed.",
            "The strains were E. coli K-12 and MG1655.",
            "The yield of E. coli K-12 was discussed.",
            "The yield of MG1655 was discussed.",
            "The yield of the panB strain was discussed.",
            "The yield of the ilv2 strain was discussed.",
            "The study qualitatively describes microbial pantothenate production.",
        )
        for text in examples:
            with self.subTest(text=text):
                self.assertIs(classify_quantity_signal(text), QuantitySignal.NONE)
                topic, record, payload = make_review(text)
                approved = apply_synthetic_review(topic, record, payload)
                self.assertEqual(len(approved), 1)
                self.assertEqual(record.review_status, "approved")

    def test_from_and_between_years_near_metrics_are_not_automatically_exempt(self):
        examples = (
            "The titer increased from 2024 to 2025.",
            "The yield was between 2024 and 2025.",
        )
        for text in examples:
            with self.subTest(text=text):
                for kind in ("qualitative", "quantitative"):
                    with self.subTest(kind=kind):
                        topic, record, payload = make_review(
                            text,
                            kind=kind,
                            context=COMPLETE_QUANTITATIVE_CONTEXT if kind == "quantitative" else None,
                        )
                        with self.assertRaises(ValueError) as error:
                            apply_synthetic_review(topic, record, payload)
                        self.assertEqual(str(error.exception), "疑似数量表达需核对后才能批准")
                        self.assertEqual(record.review_status, "pending")
                self.assertIs(classify_quantity_signal(text), QuantitySignal.NEEDS_REVIEW)

    def test_unrecognized_or_incomplete_measurement_is_explicitly_blocked(self):
        examples = (
            "The pantothenate titer reached 8 arbitrary units.",
            "The pantothenate titer reached 3.5.",
            "The titer reached 2024.",
            "In 2024, the ilv2-engineered strain's titer reached 8 arbitrary units.",
        )
        for text in examples:
            with self.subTest(text=text):
                self.assertIs(classify_quantity_signal(text), QuantitySignal.NEEDS_REVIEW)
                topic, record, payload = make_review(text)
                with self.assertRaises(ValueError) as error:
                    apply_synthetic_review(topic, record, payload)
                self.assertEqual(str(error.exception), "疑似数量表达需核对后才能批准")
                self.assertEqual(record.review_status, "pending")

    def test_suspected_quantity_is_blocked_even_when_claim_is_labeled_quantitative(self):
        context = {
            "product": "pantothenate", "host": "Escherichia coli", "strain": "not_reported",
            "intervention": "not_reported", "comparator": "not_reported", "cultivation": "not_reported",
            "medium": "not_reported", "metric": "titer", "value": "8", "unit": "unknown",
            "time": "not_reported", "scale": "not_reported", "limitations": "synthetic fixture",
        }
        topic, record, payload = make_review(
            "The pantothenate titer reached 8 arbitrary units.", kind="quantitative", context=context
        )
        with self.assertRaises(ValueError) as error:
            apply_synthetic_review(topic, record, payload)
        self.assertEqual(str(error.exception), "疑似数量表达需核对后才能批准")
        self.assertEqual(record.review_status, "pending")

    def test_unknown_quantity_after_known_quantity_still_blocks_approval(self):
        context = {
            "product": "pantothenate", "host": "Escherichia coli", "strain": "not_reported",
            "intervention": "not_reported", "comparator": "not_reported", "cultivation": "not_reported",
            "medium": "not_reported", "metric": "titer", "value": "12", "unit": "g/L",
            "time": "not_reported", "scale": "not_reported", "limitations": "synthetic fixture",
        }
        text = "The titer was 12 g/L and the activity was 8 arbitrary units."
        for kind in ("qualitative", "quantitative"):
            with self.subTest(kind=kind):
                topic, record, payload = make_review(
                    text, kind=kind, context=context if kind == "quantitative" else None
                )
                with self.assertRaises(ValueError) as error:
                    apply_synthetic_review(topic, record, payload)
                self.assertEqual(str(error.exception), "疑似数量表达需核对后才能批准")
                self.assertEqual(record.review_status, "pending")

    def test_bare_measurement_number_after_known_quantity_still_blocks_approval(self):
        text = "The titer was 12 g/L and the yield was 8."
        for kind in ("qualitative", "quantitative"):
            with self.subTest(kind=kind):
                topic, record, payload = make_review(
                    text,
                    kind=kind,
                    context=COMPLETE_QUANTITATIVE_CONTEXT if kind == "quantitative" else None,
                )
                with self.assertRaises(ValueError) as error:
                    apply_synthetic_review(topic, record, payload)
                self.assertEqual(str(error.exception), "疑似数量表达需核对后才能批准")
                self.assertEqual(record.review_status, "pending")
        self.assertIs(classify_quantity_signal(text), QuantitySignal.NEEDS_REVIEW)

    def test_two_known_quantities_in_one_claim_are_not_overblocked(self):
        text = "The titer was 12 g/L and the yield was 0.8 g/g."
        self.assertIs(classify_quantity_signal(text), QuantitySignal.QUANTITATIVE)
        topic, record, payload = make_review(
            text, kind="quantitative", context=COMPLETE_QUANTITATIVE_CONTEXT
        )
        approved = apply_synthetic_review(topic, record, payload)
        self.assertEqual(len(approved), 1)
        self.assertEqual(record.review_status, "approved")

    def test_complete_quantitative_context_passes_but_missing_context_fields_do_not(self):
        context = {
            "product": "pantothenate", "host": "Escherichia coli", "strain": "not_reported",
            "intervention": "engineered", "comparator": "not_reported", "cultivation": "batch fermentation",
            "medium": "not_reported", "metric": "titer", "value": "12", "unit": "g/L",
            "time": "not_reported", "scale": "not_reported", "limitations": "synthetic fixture only",
        }
        topic, record, payload = make_review(
            "The synthetic pantothenate titer was 12 g/L.", kind="quantitative", context=context
        )
        approved = apply_synthetic_review(topic, record, payload)
        self.assertEqual(len(approved), 1)
        self.assertEqual(record.review_status, "approved")

        topic, record, payload = make_review(
            "The synthetic pantothenate titer was 12 g/L.", kind="quantitative"
        )
        with self.assertRaises(ValueError) as error:
            apply_synthetic_review(topic, record, payload)
        self.assertEqual(
            str(error.exception),
            "定量 claim 必须填写产物、宿主、菌株、改造、对照、培养、培养基、指标、数值、单位、时间、规模和局限；缺失写 not_reported",
        )
        self.assertEqual(record.review_status, "pending")

        incomplete = dict(context)
        del incomplete["medium"]
        topic, record, payload = make_review(
            "The synthetic pantothenate titer was 12 g/L", kind="quantitative", context=incomplete
        )
        with self.assertRaises(ValueError) as error:
            apply_synthetic_review(topic, record, payload)
        self.assertEqual(
            str(error.exception),
            "定量 claim 必须填写产物、宿主、菌株、改造、对照、培养、培养基、指标、数值、单位、时间、规模和局限；缺失写 not_reported",
        )
        self.assertEqual(record.review_status, "pending")


if __name__ == "__main__":
    unittest.main()
