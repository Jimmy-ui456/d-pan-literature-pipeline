from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import run_engineering_tools_demo as demo
from scripts.run_synthetic_demo import OfflineNetworkError


class EngineeringToolsDemoTests(unittest.TestCase):
    def test_real_offline_tools_keep_synthetic_results_pending(self):
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "demo"
            report = demo.run_demo(out)
            self.assertEqual(report["network_requests"], 0)
            self.assertEqual(report["ai_status"], "pending")
            self.assertEqual(report["science_approval"], "none")
            self.assertEqual(json.loads((out / "service_plan" / "probe_report.json").read_text())["status"], "planned")
            self.assertEqual(json.loads((out / "discovery_plan" / "discovery.json").read_text())["status"], "offline_plan")

    def test_existing_output_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp)
            sentinel = out / "keep.txt"
            sentinel.write_text("keep", encoding="utf-8")
            with self.assertRaises(ValueError):
                demo.run_demo(out)
            self.assertEqual(sentinel.read_text(), "keep")

    def test_even_swallowed_network_attempt_fails_demo(self):
        import socket
        from litsearch import service_probe
        original = service_probe.main

        def attempted_network(argv):
            try:
                socket.create_connection(("example.invalid", 443))
            except OfflineNetworkError:
                pass
            return original(argv)

        with tempfile.TemporaryDirectory() as temp, patch.object(service_probe, "main", attempted_network):
            with self.assertRaises(OfflineNetworkError):
                demo.run_demo(Path(temp) / "demo")

    def test_reparse_parent_is_rejected_without_writes(self):
        import stat
        from types import SimpleNamespace
        original = Path.lstat
        with tempfile.TemporaryDirectory() as temp:
            parent = Path(temp)

            def attributes(path, *args, **kwargs):
                if path == parent:
                    return SimpleNamespace(st_mode=stat.S_IFDIR,
                                           st_file_attributes=getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024))
                return original(path, *args, **kwargs)

            with patch.object(Path, "lstat", attributes), self.assertRaises(ValueError):
                demo.run_demo(parent / "demo")
            self.assertFalse((parent / "demo").exists())


if __name__ == "__main__":
    unittest.main()
