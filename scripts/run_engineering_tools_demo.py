"""Offline tool smoke demonstration using hand-written, non-scientific HTML."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import stat
import sys


ROOT = Path(__file__).absolute().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.run_synthetic_demo import network_blocked


SYNTHETIC_HTML = """<!doctype html><html><body>
<table><caption>SYNTHETIC Table 1 — layout test only</caption>
<thead><tr><th rowspan="2">Sample</th><th colspan="2">SYNTHETIC reading (arbitrary units)</th></tr>
<tr><th>Initial</th><th>Final</th></tr></thead>
<tbody><tr><td>SYNTHETIC A</td><td>1.2</td><td>3.4*</td></tr>
<tr><td>SYNTHETIC B</td><td></td><td>5.6</td></tr></tbody>
<tfoot><tr><td colspan="3">* SYNTHETIC footnote; no experimental finding.</td></tr></tfoot>
</table></body></html>"""


def run_demo(out_dir: Path) -> dict:
    from litsearch import discovery, document_parse, service_probe

    out_dir = out_dir.absolute()
    def reparse(path: Path) -> bool:
        try:
            details = path.lstat()
        except FileNotFoundError:
            return False
        return stat.S_ISLNK(details.st_mode) or bool(
            getattr(details, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024))

    if out_dir.exists() or any(reparse(p) for p in (out_dir, *out_dir.parents)):
        raise ValueError("Choose a new nonsymlink output directory")
    out_dir.mkdir(parents=True, exist_ok=False)
    source = out_dir / "synthetic_table.html"
    source.write_text(SYNTHETIC_HTML, encoding="utf-8")
    source_digest = hashlib.sha256(source.read_bytes()).hexdigest()
    with network_blocked():
        if service_probe.main(["--out-dir", str(out_dir / "service_plan")]) != 0:
            raise AssertionError("Offline service plan failed")
        if discovery.main(["--out-dir", str(out_dir / "discovery_plan"),
                           "--alias", "synthetic alternative term"]) != 0:
            raise AssertionError("Offline discovery plan failed")
        # This trusted, hand-written HTML needs no OCR or optional native adapter.
        parsed = document_parse.parse_document(source)
    if parsed["source"]["sha256"] != source_digest or parsed["ai_status"] != "pending":
        raise AssertionError("Parser source binding or review status changed")
    table = parsed["tables"][0]
    if table["cells"][0]["rowspan"] != 2 or table["cells"][1]["colspan"] != 2:
        raise AssertionError("Merged synthetic header lost")
    if not any(cell["raw_text"] == "" for cell in table["cells"]):
        raise AssertionError("Explicit blank synthetic cell was not preserved")
    if not any(note["association"] == "explicit_tfoot" for note in table["notes"]):
        raise AssertionError("Synthetic footnote lost")
    if hashlib.sha256(source.read_bytes()).hexdigest() != source_digest:
        raise AssertionError("Original synthetic source was modified")
    (out_dir / "document.json").write_text(json.dumps(parsed, ensure_ascii=False, indent=2) + "\n",
                                           encoding="utf-8")
    report = {"status": "synthetic_offline_checks_passed", "network_requests": 0,
              "source_sha256": source_digest, "tables": len(parsed["tables"]),
              "ai_status": "pending", "science_approval": "none", "human_review": None,
              "not_exercised": ["real API services", "real papers", "native OCR engine"]}
    (out_dir / "demo_report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(run_demo(args.out_dir)))
    except (ValueError, OSError, AssertionError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
