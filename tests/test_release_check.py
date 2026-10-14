from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "release_check.py"


class ReleaseCheckTests(unittest.TestCase):
    def run_check(self, root: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(SCRIPT), "--root", str(root)], text=True, capture_output=True, check=False)

    def prepared_root(self, temporary: Path) -> Path:
        root = temporary / "candidate"
        root.mkdir()
        (root / "release_allowlist.json").write_text((ROOT / "release_allowlist.json").read_text(encoding="utf-8"), encoding="utf-8")
        (root / "README.md").write_text("License status is pending.\n", encoding="utf-8")
        return root

    def test_license_placeholder_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            self.assertEqual(self.run_check(self.prepared_root(Path(temp))).returncode, 0)

    def test_unknown_file_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = self.prepared_root(Path(temp)); (root / "unknown.bin").write_bytes(b"x")
            self.assertNotEqual(self.run_check(root).returncode, 0)

    def test_absolute_path_and_credential_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = self.prepared_root(Path(temp)); (root / "pyproject.toml").write_text("C:" + "\\Users\\real\\secret\\napi_key=\"abcdefghijklmnop\"\n", encoding="utf-8")
            report = json.loads(self.run_check(root).stdout)
            self.assertNotEqual(report["result"], "pass")
            self.assertEqual({item["rule"] for item in report["findings"]}, {"absolute_local_path", "generic_assignment"})

    def test_readme_new_credential_is_not_exempt(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = self.prepared_root(Path(temp)); (root / "README.md").write_text("token=" + "\"abcdefghijklmnop\"\n", encoding="utf-8")
            self.assertIn("generic_assignment", {item["rule"] for item in json.loads(self.run_check(root).stdout)["findings"]})

    def test_net_nonfixture_absolute_path_is_not_exempt(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = self.prepared_root(Path(temp)); target = root / "tests" / "test_net.py"; target.parent.mkdir()
            target.write_text("where = \"C:" + "\\Users\\not_a_fixture\"\n", encoding="utf-8")
            self.assertIn("absolute_local_path", {item["rule"] for item in json.loads(self.run_check(root).stdout)["findings"]})

    def test_fixed_network_fixture_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = self.prepared_root(Path(temp)); target = root / "tests" / "test_net.py"; target.parent.mkdir()
            target.write_text("url = \"file:" + "/" + "/" + "/" + "etc/passwd\"\n", encoding="utf-8")
            self.assertEqual(self.run_check(root).returncode, 0)

    def test_fixture_prefixes_are_not_exempt(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = self.prepared_root(Path(temp))
            net = root / "tests" / "test_net.py"; net.parent.mkdir()
            net.write_text("url = \"file:" + "/" + "/" + "/" + "etc/company-private\"\n", encoding="utf-8")
            zotero = root / "tests" / "extensions" / "test_zotero_safety.py"; zotero.parent.mkdir()
            zotero.write_text("url = \"file:" + "/" + "/" + "/" + "tmp/private-research\"\n", encoding="utf-8")
            findings = json.loads(self.run_check(root).stdout)["findings"]
            self.assertEqual(2, sum(item["rule"] == "absolute_local_path" for item in findings))

    def test_fixed_zotero_fixture_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = self.prepared_root(Path(temp)); target = root / "tests" / "extensions" / "test_zotero_safety.py"; target.parent.mkdir(parents=True)
            target.write_text("url = \"file:" + "/" + "/" + "/" + "tmp/zotero\"\n", encoding="utf-8")
            self.assertEqual(self.run_check(root).returncode, 0)

    def test_prepare_stage_rejects_unknown_but_lists_generated_exclusions(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = self.prepared_root(Path(temp)); (root / ".validation").mkdir(); (root / ".validation" / "old.log").write_text("x", encoding="utf-8")
            (root / "unlisted.txt").write_text("x", encoding="utf-8")
            stage = Path(temp) / "stage"
            process = subprocess.run([sys.executable, str(SCRIPT), "--root", str(root), "--prepare-stage", str(stage)], text=True, capture_output=True, check=False)
            report = json.loads(process.stdout)
            self.assertNotEqual(process.returncode, 0)
            self.assertEqual("unlisted.txt", report["findings"][0]["path"])
            self.assertEqual("known_generated_directory", report["excluded"][0]["reason"])

    def test_unsafe_stage_target_keeps_sentinel(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = self.prepared_root(Path(temp)); sentinel = root / "sentinel.txt"; sentinel.write_text("keep", encoding="utf-8")
            process = subprocess.run([sys.executable, str(SCRIPT), "--root", str(root), "--prepare-stage", str(root)], text=True, capture_output=True, check=False)
            self.assertNotEqual(process.returncode, 0); self.assertTrue(sentinel.exists())


    def test_exact_openalex_redaction_sentinels_pass(self) -> None:
        for name in ("test_sources.py", "test_publication_sources.py"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp:
                root = self.prepared_root(Path(temp))
                target = root / "tests" / name
                target.parent.mkdir()
                target.write_text('api_' + 'key=' + json.dumps('KEY_SENTINEL') + '\n', encoding="utf-8")
                self.assertEqual(self.run_check(root).returncode, 0)

    def test_redaction_sentinel_exception_does_not_hide_other_credentials(self) -> None:
        for name, assignment in (
            ("test_sources.py", 'api_' + 'key=' + json.dumps('KEY_SENTINEL_CHANGED')),
            ("test_sources.py", 'to' + 'ken=' + json.dumps('KEY_SENTINEL')),
            ("test_other.py", 'api_' + 'key=' + json.dumps('KEY_SENTINEL')),
        ):
            with self.subTest(name=name, assignment=assignment), tempfile.TemporaryDirectory() as temp:
                root = self.prepared_root(Path(temp))
                target = root / "tests" / name
                target.parent.mkdir()
                target.write_text(assignment + "\n", encoding="utf-8")
                findings = json.loads(self.run_check(root).stdout)["findings"]
                self.assertIn("generic_assignment", {item["rule"] for item in findings})
