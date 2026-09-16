from __future__ import annotations

import unittest

from litsearch.models import Record


class RecordValidationTests(unittest.TestCase):
    def test_loads_legacy_row_and_ignores_unknown_fields(self) -> None:
        record = Record.from_dict({"title": "Paper", "doi": "10.1000/x", "legacy": 1})
        self.assertEqual(record.title, "Paper")
        self.assertFalse(hasattr(record, "legacy"))

    def test_rejects_wrong_list_type(self) -> None:
        with self.assertRaisesRegex(ValueError, "authors"):
            Record.from_dict({"title": "Paper", "authors": "One Author"})

    def test_clears_old_derived_values_but_keeps_source_metadata(self) -> None:
        record = Record(
            title="Paper",
            doi="10.1000/x",
            pdf_file="paper.pdf",
            final_yield="old",
            results_excerpt="old excerpt",
            method_result_cn="old summary",
            screening_reasons=["old reason"],
        )
        record.clear_derived_fields()
        self.assertEqual(record.title, "Paper")
        self.assertEqual(record.pdf_file, "paper.pdf")
        self.assertEqual(record.final_yield, "")
        self.assertEqual(record.results_excerpt, "")
        self.assertEqual(record.screening_reasons, [])


if __name__ == "__main__":
    unittest.main()
