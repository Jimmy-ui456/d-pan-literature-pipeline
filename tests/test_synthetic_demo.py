from __future__ import annotations

import hashlib
import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import run_synthetic_demo


class SyntheticDemoTests(unittest.TestCase):
    def test_demo_is_offline_and_keeps_all_records_unverified(self) -> None:
        before = hashlib.sha256(run_synthetic_demo.INPUT.read_bytes()).hexdigest()
        with tempfile.TemporaryDirectory() as temporary:
            out_dir = Path(temporary) / "new-output"
            summary = run_synthetic_demo.run_demo(out_dir)
            output = json.loads((out_dir / "all_screened_records.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["records"], 2)
        self.assertEqual(summary["review_ready"], 0)
        self.assertTrue(all(row["title"].startswith("SYNTHETIC —") for row in output))
        self.assertTrue(all(row["identity_status"] == "unverified" for row in output))
        self.assertTrue(all(row["review_status"] == "pending" for row in output))
        self.assertEqual(before, hashlib.sha256(run_synthetic_demo.INPUT.read_bytes()).hexdigest())

    def test_network_guard_fails_closed(self) -> None:
        with self.assertRaises(run_synthetic_demo.OfflineNetworkError):
            with run_synthetic_demo.network_blocked():
                with self.assertRaises(run_synthetic_demo.OfflineNetworkError):
                    socket.create_connection(("127.0.0.1", 9))

    def test_swallowed_network_attempt_still_fails_and_restores_all_apis(self) -> None:
        originals = (socket.socket.connect, socket.socket.connect_ex, socket.create_connection)
        from litsearch import cli

        real_run = cli.run

        def run_with_swallowed_connection(args: object) -> object:
            try:
                socket.create_connection(("127.0.0.1", 9))
            except Exception:
                pass
            return real_run(args)

        with tempfile.TemporaryDirectory() as temporary, patch("litsearch.cli.run", run_with_swallowed_connection):
            out_dir = Path(temporary) / "output"
            with self.assertRaises(run_synthetic_demo.OfflineNetworkError):
                run_synthetic_demo.run_demo(out_dir)
            manifest = json.loads((out_dir / "run_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "completed")
        self.assertEqual(originals, (socket.socket.connect, socket.socket.connect_ex, socket.create_connection))

    def test_network_guard_covers_each_api_and_nested_restore(self) -> None:
        originals = (socket.socket.connect, socket.socket.connect_ex, socket.create_connection)
        def connect() -> None:
            with socket.socket(socket.AF_INET) as sock:
                sock.connect(("127.0.0.1", 9))

        def connect_ex() -> None:
            with socket.socket(socket.AF_INET) as sock:
                sock.connect_ex(("127.0.0.1", 9))

        for call in (connect, connect_ex, lambda: socket.create_connection(("127.0.0.1", 9))):
            with self.assertRaises(run_synthetic_demo.OfflineNetworkError):
                with run_synthetic_demo.network_blocked():
                    with self.assertRaises(run_synthetic_demo.OfflineNetworkError):
                        call()
            self.assertEqual(originals, (socket.socket.connect, socket.socket.connect_ex, socket.create_connection))
        with run_synthetic_demo.network_blocked():
            with run_synthetic_demo.network_blocked():
                self.assertIsNot(socket.create_connection, originals[2])
            self.assertIsNot(socket.create_connection, originals[2])
        self.assertEqual(originals, (socket.socket.connect, socket.socket.connect_ex, socket.create_connection))

    def test_rejects_trusted_or_pdf_input_and_bad_output(self) -> None:
        base = json.loads(run_synthetic_demo.INPUT.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            bad_input = temporary_path / "bad.json"
            base[0]["identity_status"] = "verified"
            bad_input.write_text(json.dumps(base), encoding="utf-8")
            with self.assertRaises(ValueError):
                run_synthetic_demo.validate_input(bad_input)
            base = json.loads(run_synthetic_demo.INPUT.read_text(encoding="utf-8"))
            base[0]["pdf_url"] = "https://example.invalid/fake.pdf"
            bad_input.write_text(json.dumps(base), encoding="utf-8")
            with self.assertRaises(ValueError):
                run_synthetic_demo.validate_input(bad_input)
            out_dir = temporary_path / "output"
            run_synthetic_demo.run_demo(out_dir)
            manifest_path = out_dir / "run_manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["status"] = "failed"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaises(AssertionError):
                run_synthetic_demo.validate_output(out_dir, 2)
            manifest["status"] = "completed"
            manifest["counts"]["identity_verified"] = 1
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaises(AssertionError):
                run_synthetic_demo.validate_output(out_dir, 2)
            manifest["counts"]["identity_verified"] = 0
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            records_path = out_dir / "all_screened_records.json"
            output_records = json.loads(records_path.read_text(encoding="utf-8"))
            output_records[0]["review_status"] = "approved"
            records_path.write_text(json.dumps(output_records), encoding="utf-8")
            with self.assertRaises(AssertionError):
                run_synthetic_demo.validate_output(out_dir, 2)

    def test_output_validation_rejects_each_missing_or_unsafe_demo_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            out_dir = Path(temporary) / "output"
            run_synthetic_demo.run_demo(out_dir)
            records_path = out_dir / "all_screened_records.json"
            approved_path = out_dir / "review_ready.json"
            ris_path = out_dir / "review_references.ris"
            original_records = records_path.read_text(encoding="utf-8")
            original_approved = approved_path.read_text(encoding="utf-8")
            original_ris = ris_path.read_text(encoding="utf-8")

            def reject_records(mutate: object) -> None:
                records = json.loads(original_records)
                mutate(records)
                records_path.write_text(json.dumps(records), encoding="utf-8")
                with self.assertRaises(AssertionError):
                    run_synthetic_demo.validate_output(out_dir, 2)
                records_path.write_text(original_records, encoding="utf-8")

            reject_records(lambda records: records[0].__setitem__("abstract", "ordinary test text"))
            for field in ("doi", "pmid", "pmcid", "url", "pdf_identity_status"):
                reject_records(lambda records, field=field: records[0].pop(field))
            reject_records(lambda records: records[0].__setitem__("pdf_urls", ""))

            for invalid_value in ("{}", "null"):
                approved_path.write_text(invalid_value, encoding="utf-8")
                with self.assertRaises(AssertionError):
                    run_synthetic_demo.validate_output(out_dir, 2)
            approved_path.write_text(original_approved, encoding="utf-8")

            ris_path.write_text("TY  - JOUR\nTI  - Not synthetic\nER  -\n", encoding="utf-8")
            with self.assertRaises(AssertionError):
                run_synthetic_demo.validate_output(out_dir, 2)
            ris_path.write_text(original_ris, encoding="utf-8")
            ris_path.unlink()
            with self.assertRaises(AssertionError):
                run_synthetic_demo.validate_output(out_dir, 2)

    def test_same_parent_runs_use_private_empty_cache_and_pdf_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            legacy_cache = parent / "synthetic-cache"
            legacy_pdf = parent / "synthetic-pdfs"
            legacy_cache.mkdir()
            legacy_pdf.mkdir()
            marker = legacy_cache / "keep.txt"
            marker.write_text("old", encoding="utf-8")
            one, two = parent / "one", parent / "two"
            run_synthetic_demo.run_demo(one)
            run_synthetic_demo.run_demo(two)
            self.assertEqual(marker.read_text(encoding="utf-8"), "old")
            self.assertTrue((parent / ".one-synthetic-assets" / "cache").is_dir())
            self.assertTrue((parent / ".two-synthetic-assets" / "cache").is_dir())
            self.assertTrue((parent / ".one-synthetic-assets" / "pdfs").is_dir())
            self.assertTrue((parent / ".two-synthetic-assets" / "pdfs").is_dir())
