from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from openpyxl import load_workbook

from litsearch.models import Record
from litsearch.outputs import excel_safe, write_outputs


class OutputSafetyTests(unittest.TestCase):
    def test_excel_safe_prefixes_formula_characters(self) -> None:
        for value in ("=cmd()", "+1+1", "-2+3", "@SUM(A1:A2)", "  =1+1"):
            with self.subTest(value=value):
                self.assertTrue(str(excel_safe(value)).startswith("'"))
        self.assertEqual(excel_safe("ordinary title"), "ordinary title")

    def test_csv_and_xlsx_do_not_create_formula_cells(self) -> None:
        record = Record(
            title="=HYPERLINK(\"https://evil.invalid\")",
            doi="10.1000/safe",
            library_tier="核心生产/合成文献",
            screening_reasons=["test"],
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            write_outputs([record], output, all_screened_records=[record])
            with (output / "literature_candidates.csv").open(
                encoding="utf-8-sig", newline=""
            ) as handle:
                row = next(csv.DictReader(handle))
            self.assertTrue(row["标题"].startswith("'="))
            workbook = load_workbook(
                output / "literature_candidates.xlsx",
                data_only=False,
            )
            self.assertTrue(str(workbook["文献总结"]["C2"].value).startswith("'="))

    def test_empty_result_set_still_writes_valid_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            write_outputs([], output, all_screened_records=[])
            self.assertTrue((output / "literature_records.json").is_file())
            self.assertEqual((output / "literature_records.json").read_text(encoding="utf-8").strip(), "[]")
            self.assertTrue((output / "literature_candidates.xlsx").is_file())


if __name__ == "__main__":
    unittest.main()
