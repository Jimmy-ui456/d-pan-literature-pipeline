import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from litsearch.cli import _validate_args, build_parser, run
from litsearch.models import AuditEvent, Record
from litsearch.outputs import write_records_json


class CliIntegrationTests(unittest.TestCase):
    def args(self, root: Path, extra: list[str] | None = None):
        parser = build_parser()
        args = parser.parse_args(["--out-dir", str(root / "output"), "--cache-dir", str(root / "cache"),
                                  "--pdf-dir", str(root / "pdfs"), "--skip-pdf-extraction", *(extra or [])])
        _validate_args(parser, args)
        return args

    def test_offline_default_never_contacts_network_or_trusts_imported_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "input.json"
            write_records_json(source, [Record(title="Metabolic engineering for pantothenate production in Escherichia coli",
                                               doi="10.1234/unproven", identity_status="verified", review_status="approved")])
            args = self.args(root, ["--from-json", str(source), "--reuse-derived-fields"])
            with patch("litsearch.net.HttpClient.get_json", side_effect=AssertionError("unexpected network")):
                result = run(args)
            self.assertEqual(result["review_ready"], 0)
            self.assertEqual(result["identity_verified"], 0)
            self.assertEqual(json.loads((root / "output/review_ready.json").read_text()), [])
            self.assertTrue((root / "output/all_screened_records.json").exists())
            self.assertEqual((root / "output/review_references.ris").read_text(encoding="utf-8"), "")

    def test_failure_preserves_partial_raw_data_and_failed_manifest(self):
        def partial(*args, **kwargs):
            kwargs["on_batch"]([Record(title="received before failure", doi="10.1234/one")])
            raise RuntimeError("simulated second-page failure")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("litsearch.cli.fetch_europepmc", side_effect=partial), self.assertRaises(RuntimeError):
                run(self.args(root))
            self.assertEqual(len(json.loads((root / "output/raw_records.json").read_text())), 1)
            manifest = json.loads((root / "output/run_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "failed")
            self.assertEqual(manifest["counts"]["raw_before_failure"], 1)

    def test_pubmed_failure_keeps_partial_checkpoint_and_id_diagnostic(self):
        def partial_pubmed(*args, **kwargs):
            kwargs["on_batch"]([Record(title="received PMID 1", doi="10.1234/one", pmid="1")])
            kwargs["events"].append(AuditEvent("efetch_coverage", "failed",
                                                '{"requested":["1","2"],"received":["1"],"missing":["2"]}',
                                                source="pubmed"))
            raise ValueError('PubMed efetch batch is incomplete: missing ["2"]')

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("litsearch.cli.fetch_pubmed", side_effect=partial_pubmed), self.assertRaises(RuntimeError):
                run(self.args(root, ["--source", "pubmed", "--email", "a@example.org"]))
            self.assertEqual(len(json.loads((root / "output/raw_records.json").read_text())), 1)
            events = (root / "output/events.jsonl").read_text(encoding="utf-8")
            self.assertIn('missing', events)
            self.assertIn('2', events)

    def test_all_sources_failed_not_completed_even_with_partial_allowed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("litsearch.cli.fetch_europepmc", side_effect=RuntimeError("unavailable")), self.assertRaisesRegex(RuntimeError, "全部"):
                run(self.args(root, ["--allow-partial-sources"]))

    def test_truncated_search_is_visible_in_status(self):
        def capped(*args, **kwargs):
            kwargs["events"].append(AuditEvent("search_coverage", "truncated", "budget reached"))
            return []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("litsearch.cli.fetch_europepmc", side_effect=capped):
                result = run(self.args(root))
            self.assertEqual(result["status"], "completed_with_warnings")

    def test_xlsx_failure_is_non_success_not_silent_degradation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "input.json"
            source.write_text("[]")
            with patch("litsearch.outputs._write_workbook", side_effect=OSError("locked")), self.assertRaises(RuntimeError):
                run(self.args(root, ["--from-json", str(source)]))
            self.assertEqual(json.loads((root / "output/run_manifest.json").read_text(encoding="utf-8"))["status"], "failed")


if __name__ == "__main__":
    unittest.main()
