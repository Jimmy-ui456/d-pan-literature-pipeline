from __future__ import annotations

import copy
import csv
import json
import tempfile
import unittest
from pathlib import Path

from litsearch.evidence import apply_review_decisions, review_template, write_review_bundle
from litsearch.models import Record
from litsearch.topics import load_topic


CONTEXT_FIELDS = (
    "product", "host", "strain", "intervention", "comparator", "cultivation",
    "medium", "metric", "value", "unit", "time", "scale", "limitations",
)


def _synthetic_card(index: int) -> dict:
    return {
        "id": f"SYNTHETIC_EVIDENCE_{index}",
        "eligible": True,
        "source_kind": f"synthetic_source_kind_{index}",
        "page_hint": index + 3,
        "pages": [index + 3, index + 5],
        "section": f"SYNTHETIC_SECTION_{index}",
        "start": index * 100 + 1,
        "end": index * 100 + 60,
        "quote": f"SYNTHETIC_SOURCE_QUOTE_{index}; this is a fixture, not a scientific finding.",
        "warning": f"SYNTHETIC_WARNING_{index}: fixture warning only.",
        "pdf_sha256": f"synthetic-pdf-hash-{index}",
        "material_grade": f"SYNTHETIC_EVIDENCE_{index}_MATERIAL_GRADE",
        "comparability": f"SYNTHETIC_EVIDENCE_{index}_COMPARABILITY",
    }


def _markdown_literal(value: str) -> str:
    return value.replace("_", r"\_")


def _markdown_claim_block(markdown: str, index: int) -> str:
    start = markdown.index(f"### Claim {index}（")
    end = markdown.find("\n### Claim ", start + 1)
    return markdown[start:] if end < 0 else markdown[start:end]


def _approved_fixture():
    topic = load_topic()
    included = Record(
        title="=SYNTHETIC_TITLE",
        doi="10.1234/synthetic-publication-output",
        identity_status="verified",
    )
    included.relevance_status = "direct_candidate"
    included.evidence = [_synthetic_card(1), _synthetic_card(2)]
    pending = Record(
        title="SYNTHETIC_PENDING_ONLY_TITLE",
        doi="10.1234/synthetic-pending-only",
        identity_status="verified",
    )
    pending.relevance_status = "direct_candidate"
    pending.evidence = [_synthetic_card(3)]

    payload = review_template([included, pending], topic)
    context = {field: f"SYNTHETIC_CONTEXT_{field.upper()}" for field in CONTEXT_FIELDS}
    context.update({
        "metric": "titer",
        "strain": "not_reported",
        "intervention": "not_checked",
        "comparator": "source_unavailable",
        "cultivation": "not_applicable",
        "material_grade": "SYNTHETIC_MATERIAL_GRADE_SENTINEL",
        "comparability": "SYNTHETIC_NOT_COMPARABLE_SENTINEL",
        "extra_context": "EXTRA_LINE\nAND|PIPE <ANGLE>",
    })
    payload["decisions"][0].update(
        decision="include",
        reviewer="Test reviewer",
        reason="Synthetic output fixture only",
        claims=[
            {
                "kind": "quantitative",
                "text": "Synthetic titer was 12 g/L.",
                "context": context,
                "evidence_ids": ["SYNTHETIC_EVIDENCE_1", "SYNTHETIC_EVIDENCE_2"],
                "material_grade": "SYNTHETIC_CLAIM_1_MATERIAL_GRADE",
                "comparability": "SYNTHETIC_CLAIM_1_COMPARABILITY",
            },
            {
                "kind": "qualitative",
                "text": "=SYNTHETIC_FORMULA_CLAIM\nline|segment <tag>",
                "evidence_ids": ["SYNTHETIC_EVIDENCE_2"],
                "material_grade": "SYNTHETIC_CLAIM_2_MATERIAL_GRADE",
                "comparability": "SYNTHETIC_CLAIM_2_COMPARABILITY",
            },
            {
                "kind": "qualitative",
                "text": "SYNTHETIC_NULL_CONTEXT_CLAIM",
                "context": None,
                "evidence_ids": ["SYNTHETIC_EVIDENCE_2"],
                "material_grade": "SYNTHETIC_CLAIM_3_MATERIAL_GRADE",
                "comparability": "SYNTHETIC_CLAIM_3_COMPARABILITY",
            },
            {
                "kind": "qualitative",
                "text": "SYNTHETIC_EMPTY_CONTEXT_CLAIM",
                "context": {},
                "evidence_ids": ["SYNTHETIC_EVIDENCE_1"],
                "material_grade": "SYNTHETIC_CLAIM_4_MATERIAL_GRADE",
                "comparability": "SYNTHETIC_CLAIM_4_COMPARABILITY",
            },
            {
                "kind": "qualitative",
                "text": "SYNTHETIC_PARTIAL_CONTEXT_CLAIM",
                "context": {
                    "comparability": "SYNTHETIC_PARTIAL_COMPARABILITY_SENTINEL",
                    "extra_field": "SYNTHETIC_PARTIAL_EXTRA_SENTINEL",
                },
                "evidence_ids": ["SYNTHETIC_EVIDENCE_1"],
                "material_grade": "SYNTHETIC_CLAIM_5_MATERIAL_GRADE",
                "comparability": "SYNTHETIC_CLAIM_5_COMPARABILITY",
            },
        ],
    )

    with tempfile.TemporaryDirectory() as directory:
        decisions_path = Path(directory) / "synthetic_decisions.json"
        decisions_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        approved = apply_review_decisions([included, pending], topic, decisions_path)
    return topic, [included, pending], approved


class PublicationOutputFidelityTests(unittest.TestCase):
    def test_approved_claim_projection_preserves_json_markdown_and_csv_fields(self):
        topic, records, approved = _approved_fixture()
        before = copy.deepcopy(approved)
        records_before = copy.deepcopy(records)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            write_review_bundle(output, records, topic, approved)

            json_items = json.loads((output / "review_ready.json").read_text(encoding="utf-8"))
            markdown = (output / "review_materials.md").read_text(encoding="utf-8")
            self.assertIn("- product: SYNTHETIC\\_CONTEXT\\_PRODUCT", markdown)
            with (output / "review_claims.csv").open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))

        self.assertEqual(json_items, before)
        self.assertEqual(approved, before)
        self.assertEqual(len(json_items), 1)
        self.assertEqual(len(rows), 5)
        self.assertNotIn("SYNTHETIC_PENDING_ONLY_TITLE", markdown)
        self.assertTrue(all(row["record_key"] == approved[0]["record_key"] for row in rows))
        self.assertEqual(records, records_before)
        self.assertEqual([row["claim_index"] for row in rows], ["1", "2", "3", "4", "5"])
        self.assertEqual([row["kind"] for row in rows], ["quantitative", "qualitative", "qualitative", "qualitative", "qualitative"])
        self.assertEqual(rows[0]["title"], "'=SYNTHETIC_TITLE")
        self.assertEqual(rows[0]["doi"], approved[0]["doi"])
        self.assertIn("Claim 1（quantitative）", markdown)

        quantitative = json_items[0]["claims"][0]
        self.assertEqual(set(quantitative["context"]), set(approved[0]["claims"][0]["context"]))
        self.assertEqual(len(set(quantitative["context"][field] for field in CONTEXT_FIELDS)), len(CONTEXT_FIELDS))
        csv_context = json.loads(rows[0]["context_json"])
        for field in CONTEXT_FIELDS:
            value = quantitative["context"][field]
            self.assertEqual(csv_context[field], value)
            self.assertEqual(rows[0][field], value)
            self.assertIn(f"- {field}: {_markdown_literal(value)}", markdown)

        for key, value in (
            ("material_grade", "SYNTHETIC_MATERIAL_GRADE_SENTINEL"),
            ("comparability", "SYNTHETIC_NOT_COMPARABLE_SENTINEL"),
        ):
            self.assertEqual(csv_context[key], value)
            self.assertIn(f"- {_markdown_literal(key)}: {_markdown_literal(value)}", markdown)
            self.assertEqual(json_items[0]["claims"][0]["context"][key], value)

        self.assertEqual(
            [csv_context[field] for field in ("strain", "intervention", "comparator", "cultivation")],
            ["not_reported", "not_checked", "source_unavailable", "not_applicable"],
        )
        self.assertEqual(json.loads(rows[0]["evidence_json"]), quantitative["evidence"])
        self.assertEqual(json.loads(rows[0]["source_kinds_json"]), [
            "synthetic_source_kind_1", "synthetic_source_kind_2",
        ])
        self.assertEqual(json.loads(rows[0]["evidence_ids_json"]), [
            "SYNTHETIC_EVIDENCE_1", "SYNTHETIC_EVIDENCE_2",
        ])
        for card in quantitative["evidence"]:
            for value in (
                card["id"], card["source_kind"], card["section"], card["quote"], card["warning"],
                str(card["page_hint"]), str(card["start"]), str(card["end"]),
            ):
                self.assertIn(_markdown_literal(value), markdown)

        qualitative = json_items[0]["claims"][1]
        self.assertNotIn("context", qualitative)
        self.assertEqual(rows[1]["context_json"], "")
        self.assertIn("Context：未提供（输入对象中不存在 context 字段）。", markdown)
        self.assertIn(_markdown_literal("SYNTHETIC_PARTIAL_COMPARABILITY_SENTINEL"), markdown)
        self.assertIn("SYNTHETIC_PARTIAL_EXTRA_SENTINEL", rows[4]["context_json"])
        self.assertEqual(rows[4]["product"], "")
        self.assertNotIn("not_reported", rows[4]["context_json"])

    def test_f3_claim_and_evidence_metadata_remain_scoped_per_claim_and_csv_row(self):
        topic, records, approved = _approved_fixture()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            write_review_bundle(output, records, topic, approved)
            markdown = (output / "review_materials.md").read_text(encoding="utf-8")
            with (output / "review_claims.csv").open(encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                rows = list(reader)
                fieldnames = reader.fieldnames

        self.assertIn("claim_json", fieldnames)
        self.assertEqual(len(rows), 5)
        for row_index, row in enumerate(rows, start=1):
            claim = json.loads(row["claim_json"])
            self.assertEqual(claim, approved[0]["claims"][row_index - 1])
            claim_block = _markdown_claim_block(markdown, row_index)
            for field in ("material_grade", "comparability"):
                value = claim[field]
                self.assertIn(f"- claim.{field}: {_markdown_literal(value)}", claim_block)
                for other_index, other in enumerate(rows, start=1):
                    if other_index != row_index:
                        other_claim = json.loads(other["claim_json"])
                        self.assertNotIn(other_claim[field], claim_block)

            evidence = json.loads(row["evidence_json"])
            expected_ids = claim["evidence_ids"]
            self.assertEqual(evidence, claim["evidence"])
            self.assertEqual([card["id"] for card in evidence], expected_ids)
            for card in evidence:
                for field in ("material_grade", "comparability"):
                    value = card[field]
                    self.assertIn(f"- evidence.{field}: {_markdown_literal(value)}", claim_block)
                    self.assertNotIn(f"- claim.{field}: {_markdown_literal(value)}", claim_block)
            for other_index in (1, 2):
                if f"SYNTHETIC_EVIDENCE_{other_index}" not in expected_ids:
                    for field in ("material_grade", "comparability"):
                        value = f"SYNTHETIC_EVIDENCE_{other_index}_{field.upper()}"
                        self.assertNotIn(f"- evidence.{field}: {_markdown_literal(value)}", claim_block)

    def test_f3_context_absent_null_and_empty_object_are_distinct(self):
        topic, records, approved = _approved_fixture()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            write_review_bundle(output, records, topic, approved)
            ready = json.loads((output / "review_ready.json").read_text(encoding="utf-8"))
            markdown = (output / "review_materials.md").read_text(encoding="utf-8")
            with (output / "review_claims.csv").open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))

        with self.subTest(context_state="absent"):
            self.assertNotIn("context", ready[0]["claims"][1])
            self.assertEqual(rows[1]["context_json"], "")
            self.assertIn("Context：未提供（输入对象中不存在 context 字段）。", _markdown_claim_block(markdown, 2))

        with self.subTest(context_state="null"):
            self.assertIsNone(ready[0]["claims"][2]["context"])
            self.assertEqual(rows[2]["context_json"], "null")
            null_block = _markdown_claim_block(markdown, 3)
            self.assertIn("Context：显式 null（输入值为 null）。", null_block)
            self.assertNotIn("not_reported", null_block)

        with self.subTest(context_state="empty object"):
            self.assertEqual(ready[0]["claims"][3]["context"], {})
            self.assertEqual(rows[3]["context_json"], "{}")
            empty_block = _markdown_claim_block(markdown, 4)
            self.assertIn("Context：空对象（输入为 {}）。", empty_block)
            self.assertNotIn("not_reported", empty_block)

    def test_markdown_escaping_and_csv_formula_safety_retain_claim_content(self):
        topic, records, approved = _approved_fixture()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            write_review_bundle(output, records, topic, approved)
            markdown = (output / "review_materials.md").read_text(encoding="utf-8")
            with (output / "review_claims.csv").open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))

        self.assertIn("=SYNTHETIC\\_FORMULA\\_CLAIM line\\|segment &lt;tag&gt;", markdown)
        self.assertIn("EXTRA\\_LINE AND\\|PIPE &lt;ANGLE&gt;", markdown)
        self.assertEqual(rows[1]["text"], "'=SYNTHETIC_FORMULA_CLAIM\nline|segment <tag>")
        self.assertEqual(rows[0]["title"], "'=SYNTHETIC_TITLE")
        self.assertIn("line|segment <tag>", rows[1]["text"])

    def test_empty_approved_set_still_writes_claim_csv_header(self):
        topic, records, _approved = _approved_fixture()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            write_review_bundle(output, records, topic, [])
            ready = json.loads((output / "review_ready.json").read_text(encoding="utf-8"))
            markdown = (output / "review_materials.md").read_text(encoding="utf-8")
            with (output / "review_claims.csv").open(encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                header = reader.fieldnames
                rows = list(reader)

        self.assertEqual(ready, [])
        self.assertTrue(header)
        self.assertIn("record_key", header)
        self.assertIn("context_json", header)
        self.assertTrue(set(CONTEXT_FIELDS).issubset(header))
        self.assertEqual(rows, [])
        self.assertNotIn("SYNTHETIC_PENDING_ONLY_TITLE", markdown)


if __name__ == "__main__":
    unittest.main()
