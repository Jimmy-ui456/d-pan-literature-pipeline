import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from litsearch.core import safe_pdf_name
from litsearch.models import Record
from litsearch.net import Response
from litsearch.pdfs import PdfIndex, download_pdfs, enrich_from_pdfs, pdf_matches_record


TITLE = "Metabolic engineering for pantothenate production in Escherichia coli"
DOI = "10.1234/pantothenate"
RIGHT_TEXT = f"{TITLE}\nDOI: {DOI}\nResults\nPantothenate production reached 1.2 g/L."


def fake_pdf(marker: bytes) -> bytes:
    return b"%PDF-1.7\n" + marker + b"x" * 1200


class FakeClient:
    def __init__(self, responses):
        self.responses = responses
        self.requested = []

    def get_json(self, url, timeout):
        raise AssertionError("discovery must not use a real client in this test")

    def get_bytes(self, url, timeout, max_bytes):
        self.requested.append(url)
        return Response(self.responses[url], "application/pdf", url + "?redirect=1", 200)


def fake_extract(path, max_pages=80):
    content = Path(path).read_bytes()
    if b"CORRUPT" in content:
        return ""
    if b"WRONG" in content:
        return "An unrelated paper\nDOI: 10.1234/other\nResults\nNo match."
    if b"SUPPLEMENT" in content:
        return f"Supporting Information\n{RIGHT_TEXT}"
    return RIGHT_TEXT


class PublicationPdfRecoveryTests(unittest.TestCase):
    def record(self, urls):
        return Record(title=TITLE, doi=DOI, pdf_urls=list(urls))

    def recover(self, root, responses, record=None, **kwargs):
        client = FakeClient(responses)
        rec = record or self.record(list(responses))
        events = download_pdfs([rec], root / "pdfs", client=client, email="", **kwargs)
        return rec, events, client

    def assert_final_is_accepted(self, rec, events, expected_body):
        self.assertTrue(rec.pdf_file)
        path = Path(rec.pdf_file)
        self.assertEqual(path.read_bytes(), expected_body)
        self.assertTrue(pdf_matches_record(rec, fake_extract(path)))
        self.assertEqual(rec.pdf_identity_status, "matched")
        self.assertEqual(rec.pdf_sha256, hashlib.sha256(expected_body).hexdigest())
        rejected = [event for event in events if event.status == "rejected"]
        self.assertTrue(rejected)
        self.assertTrue(any("saved_file=" in event.message or "file=" in event.message or
                            "文件=" in event.message for event in rejected))

    def test_wrong_body_does_not_block_the_next_candidate(self):
        wrong, right = "https://example.test/wrong.pdf", "https://example.test/right.pdf"
        responses = {wrong: fake_pdf(b"WRONG"), right: fake_pdf(b"RIGHT")}
        with tempfile.TemporaryDirectory() as directory, patch(
            "litsearch.pdfs.extract_text_from_pdf", side_effect=fake_extract
        ):
            rec, events, client = self.recover(Path(directory), responses)
            self.assertEqual(client.requested, [wrong, right])
            self.assert_final_is_accepted(rec, events, responses[right])
            self.assertEqual(sum(event.status == "success" and event.stage == "pdf_download" for event in events), 1)
            self.assertNotEqual(Path(rec.pdf_file).parent.name, "_rejected")

    def test_supplement_is_rejected_before_the_body_candidate(self):
        supplement, body = "https://example.test/supplement.pdf", "https://example.test/body.pdf"
        responses = {supplement: fake_pdf(b"SUPPLEMENT"), body: fake_pdf(b"RIGHT")}
        with tempfile.TemporaryDirectory() as directory, patch(
            "litsearch.pdfs.extract_text_from_pdf", side_effect=fake_extract
        ):
            rec, events, client = self.recover(Path(directory), responses)
            self.assertEqual(client.requested, [supplement, body])
            self.assert_final_is_accepted(rec, events, responses[body])
            self.assertIn("补充", "".join(e.message for e in events))

    def test_corrupt_pdf_is_rejected_before_the_body_candidate(self):
        corrupt, body = "https://example.test/corrupt.pdf", "https://example.test/body.pdf"
        responses = {corrupt: fake_pdf(b"CORRUPT"), body: fake_pdf(b"RIGHT")}
        with tempfile.TemporaryDirectory() as directory, patch(
            "litsearch.pdfs.extract_text_from_pdf", side_effect=fake_extract
        ):
            rec, events, client = self.recover(Path(directory), responses)
            self.assertEqual(client.requested, [corrupt, body])
            self.assert_final_is_accepted(rec, events, responses[body])
            self.assertIn("解析", "".join(e.message for e in events))

    def test_invalid_historical_pdf_is_preserved_and_retried(self):
        old, new = "https://example.test/old.pdf", "https://example.test/new.pdf"
        responses = {new: fake_pdf(b"RIGHT")}
        with tempfile.TemporaryDirectory() as directory, patch(
            "litsearch.pdfs.extract_text_from_pdf", side_effect=fake_extract
        ):
            root = Path(directory)
            pdf_dir = root / "pdfs"
            pdf_dir.mkdir()
            rec = self.record([new])
            historical = pdf_dir / safe_pdf_name(rec)
            historical.write_bytes(fake_pdf(b"WRONG"))
            old_bytes = historical.read_bytes()
            rec.pdf_file = str(historical)
            rec.results_excerpt = "stale result"
            rec.pdf_sha256 = "f" * 64
            rec.evidence = [{"quote": "stale"}]
            rec.pdf_identity_status = "matched"
            events = download_pdfs([rec], pdf_dir, client=FakeClient(responses), email="")
            self.assertEqual(historical.read_bytes(), old_bytes)
            self.assertNotEqual(Path(rec.pdf_file), historical)
            self.assertNotIn(historical, PdfIndex.build(pdf_dir).paths)
            self.assertEqual(rec.results_excerpt, "")
            self.assertEqual(rec.evidence, [])
            self.assert_final_is_accepted(rec, events, responses[new])

    def test_failed_record_does_not_spend_success_budget(self):
        bad1, bad2, good = ("https://example.test/bad1.pdf", "https://example.test/bad2.pdf",
                            "https://example.test/good.pdf")
        responses = {bad1: fake_pdf(b"WRONG"), bad2: fake_pdf(b"SUPPLEMENT"), good: fake_pdf(b"RIGHT")}
        first, second = self.record([bad1, bad2]), self.record([good])
        second.doi = DOI
        with tempfile.TemporaryDirectory() as directory, patch(
            "litsearch.pdfs.extract_text_from_pdf", side_effect=fake_extract
        ):
            root = Path(directory)
            events = download_pdfs([first, second], root / "pdfs", client=FakeClient(responses), email="",
                                   max_pdfs=1, max_urls_per_record=2,
                                   log_path=root / "output" / "pdf_download_log.csv")
            self.assertEqual(first.pdf_file, "")
            self.assertTrue(second.pdf_file)
            self.assertEqual(Path(second.pdf_file).read_bytes(), responses[good])
            log = (root / "output" / "pdf_download_log.csv").read_text(encoding="utf-8-sig")
            self.assertIn(bad1, log)
            self.assertIn("rejected", log)
            self.assertEqual(sum(e.status == "success" and e.stage == "pdf_download" for e in events), 1)

    def test_all_failed_candidates_are_logged_without_a_formal_pdf(self):
        wrong = "https://example.test/wrong.pdf"
        responses = {wrong: fake_pdf(b"WRONG")}
        with tempfile.TemporaryDirectory() as directory, patch(
            "litsearch.pdfs.extract_text_from_pdf", side_effect=fake_extract
        ):
            rec, events, _ = self.recover(Path(directory), responses)
            self.assertEqual(rec.pdf_file, "")
            self.assertEqual(rec.pdf_sha256, "")
            rejected = [e for e in events if e.status == "rejected"]
            self.assertEqual(len(rejected), 1)
            self.assertIn(wrong, rejected[0].url)
            self.assertTrue(any(p.name.endswith(".rejected") for p in (Path(directory) / "pdfs").rglob("*.rejected")))

    def test_enrichment_hashes_the_same_accepted_body(self):
        url = "https://example.test/right.pdf"
        body = fake_pdf(b"RIGHT")
        with tempfile.TemporaryDirectory() as directory, patch(
            "litsearch.pdfs.extract_text_from_pdf", side_effect=fake_extract
        ):
            rec, events, _ = self.recover(Path(directory), {url: body})
            enrich_from_pdfs([rec])
            self.assertEqual(rec.pdf_sha256, hashlib.sha256(Path(rec.pdf_file).read_bytes()).hexdigest())
            self.assertEqual(rec.pdf_sha256, hashlib.sha256(body).hexdigest())
            self.assertTrue(rec.results_excerpt)


if __name__ == "__main__":
    unittest.main()
