from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from litsearch_zotero.attach_pdfs import staging_match, validate_local_api_url
from litsearch.models import Record
from litsearch.pdfs import link_existing_pdfs


class ZoteroAttachmentSafetyTests(unittest.TestCase):
    def test_missing_doi_never_matches_arbitrary_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / "anything.pdf"
            pdf.write_bytes(b"%PDF" + b"x" * 2_000)
            self.assertIsNone(staging_match("", Path(directory)))

    def test_staging_match_requires_exact_normalized_doi(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wrong = root / "10.1000_abc-extra.pdf"
            right = root / "10.1000_abc.pdf"
            wrong.write_bytes(b"%PDF" + b"x" * 2_000)
            right.write_bytes(b"%PDF" + b"x" * 2_000)
            self.assertEqual(staging_match("10.1000/abc", root), right.resolve())

    def test_zotero_api_must_be_local(self) -> None:
        self.assertEqual(
            validate_local_api_url("http://127.0.0.1:23119/api/users/0/items"),
            "http://127.0.0.1:23119/api/users/0/items",
        )
        for url in (
            "https://127.0.0.1/api",
            "http://example.org/api",
            "file:///tmp/zotero",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_local_api_url(url)

    def test_stale_pdf_path_is_cleared(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            record = Record(title="Paper", doi="10.1000/x", pdf_file=str(Path(directory) / "gone.pdf"))
            link_existing_pdfs([record], Path(directory))
            self.assertEqual(record.pdf_file, "")
            self.assertIn("已失效", "；".join(record.processing_warnings))


if __name__ == "__main__":
    unittest.main()
