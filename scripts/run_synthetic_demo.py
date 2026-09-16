"""Run the real offline pipeline over deliberately non-scientific sample records."""
from __future__ import annotations

import argparse
import json
import socket
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


ROOT = Path(__file__).resolve().parents[1]
INPUT = ROOT / "examples" / "synthetic" / "records.json"


class OfflineNetworkError(RuntimeError):
    """Raised if the offline demo tries to open a network connection."""


_network_guard_depth = 0
_network_attempts: list[str] = []
_network_originals: dict[str, Any] = {}


def _blocked_network(*_args: object, **_kwargs: object) -> None:
    """Record every attempted connection before making it fail."""

    _network_attempts.append("socket connection API")
    raise OfflineNetworkError("synthetic demo forbids network connections")


@contextmanager
def network_blocked() -> Iterator[None]:
    """Block common socket connection APIs only while the demo pipeline runs."""

    global _network_guard_depth, _network_attempts, _network_originals
    if _network_guard_depth == 0:
        _network_attempts = []
        _network_originals = {
            "connect": socket.socket.connect,
            "connect_ex": socket.socket.connect_ex,
            "create_connection": socket.create_connection,
        }
        socket.socket.connect = _blocked_network  # type: ignore[method-assign]
        socket.socket.connect_ex = _blocked_network  # type: ignore[method-assign]
        socket.create_connection = _blocked_network  # type: ignore[assignment]
    _network_guard_depth += 1
    try:
        yield
    except Exception:
        raise
    else:
        if _network_attempts:
            raise OfflineNetworkError("synthetic demo attempted network access")
    finally:
        _network_guard_depth -= 1
        if _network_guard_depth == 0:
            socket.socket.connect = _network_originals["connect"]  # type: ignore[method-assign]
            socket.socket.connect_ex = _network_originals["connect_ex"]  # type: ignore[method-assign]
            socket.create_connection = _network_originals["create_connection"]  # type: ignore[assignment]
            _network_originals = {}


def validate_input(path: Path = INPUT) -> list[dict]:
    records = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(records, list) or not records:
        raise ValueError("synthetic input must be a nonempty record list")
    allowed_fields = {"title", "abstract", "authors", "doi", "pmid", "pmcid", "url", "source_ids"}
    for index, record in enumerate(records, 1):
        if not isinstance(record, dict):
            raise ValueError(f"synthetic record {index} must be an object")
        unexpected = set(record) - allowed_fields
        if unexpected:
            raise ValueError(f"synthetic record {index} contains forbidden field(s): {', '.join(sorted(unexpected))}")
        if not isinstance(record.get("title"), str) or not record["title"].startswith("SYNTHETIC —"):
            raise ValueError("every demo title must begin with SYNTHETIC —")
        if not isinstance(record.get("abstract"), str) or "synthetic" not in record["abstract"].casefold():
            raise ValueError("every demo abstract must be marked synthetic")
        if any(not isinstance(record.get(field), str) for field in ("doi", "pmid", "pmcid", "url")):
            raise ValueError("synthetic identifiers and URL must be strings")
        if any(record[field] for field in ("doi", "pmid", "pmcid", "url")):
            raise ValueError("synthetic input must not contain real identifiers or URLs")
        if record.get("authors") != []:
            raise ValueError("synthetic input must contain an empty authors list")
        if not isinstance(record.get("source_ids"), list) or not all(
            isinstance(value, str) and value.startswith("synthetic-") for value in record["source_ids"]
        ):
            raise ValueError("synthetic source_ids must be synthetic string labels")
    return records


def validate_output(out_dir: Path, expected_count: int) -> dict:
    records = json.loads((out_dir / "all_screened_records.json").read_text(encoding="utf-8"))
    approved = json.loads((out_dir / "review_ready.json").read_text(encoding="utf-8"))
    manifest = json.loads((out_dir / "run_manifest.json").read_text(encoding="utf-8"))
    events = [json.loads(line) for line in (out_dir / "events.jsonl").read_text(encoding="utf-8").splitlines() if line]
    if not isinstance(manifest, dict) or manifest.get("status") != "completed":
        raise AssertionError("synthetic demo manifest must be completed")
    counts = manifest.get("counts")
    if not isinstance(counts, dict):
        raise AssertionError("synthetic demo manifest must contain counts")
    required_counts = {"raw": expected_count, "deduplicated": expected_count, "identity_verified": 0,
                       "review_ready": 0, "with_pdf": 0, "with_results": 0}
    if any(
        not isinstance(counts.get(name), int) or isinstance(counts.get(name), bool) or counts[name] != value
        for name, value in required_counts.items()
    ):
        raise AssertionError("synthetic demo manifest counts are inconsistent")
    if (
        not isinstance(counts.get("retained"), int)
        or isinstance(counts["retained"], bool)
        or not 0 <= counts["retained"] <= expected_count
    ):
        raise AssertionError("synthetic demo retained count is invalid")
    if not isinstance(records, list) or any(not isinstance(record, dict) for record in records):
        raise AssertionError("demo records must be a list of record objects")
    if len(records) != expected_count:
        raise AssertionError("demo output lost synthetic records")
    if not isinstance(approved, list) or approved != []:
        raise AssertionError("synthetic demo must never produce review-ready records")
    ris_path = out_dir / "review_references.ris"
    if not ris_path.is_file() or ris_path.read_text(encoding="utf-8").strip():
        raise AssertionError("synthetic demo must not produce RIS references")
    for record in records:
        required_string_fields = (
            "title", "abstract", "doi", "pmid", "pmcid", "url", "identity_status", "review_status",
            "pdf_identity_status", "pdf_file", "pdf_sha256", "results_excerpt",
        )
        required_list_fields = ("pdf_urls", "results_pages", "evidence")
        missing_fields = [field for field in (*required_string_fields, *required_list_fields) if field not in record]
        if missing_fields:
            raise AssertionError(f"synthetic record is missing required field(s): {', '.join(missing_fields)}")
        if any(not isinstance(record[field], str) for field in required_string_fields):
            raise AssertionError("synthetic record has an invalid required string field")
        if any(not isinstance(record[field], list) for field in required_list_fields):
            raise AssertionError("synthetic record has an invalid required list field")
        if not record["title"].startswith("SYNTHETIC —"):
            raise AssertionError("demo output contains a non-synthetic title")
        if "synthetic" not in record["abstract"].casefold():
            raise AssertionError("demo output contains an unmarked abstract")
        if record["identity_status"] != "unverified" or record["review_status"] != "pending":
            raise AssertionError("synthetic records must remain unverified and pending review")
        if any(record[field] for field in ("doi", "pmid", "pmcid", "url")):
            raise AssertionError("demo output contains an identifier or URL")
        if record["pdf_identity_status"] != "unverified":
            raise AssertionError("synthetic PDF identity must remain unverified")
        if any(record[field] for field in ("pdf_urls", "pdf_file", "pdf_sha256", "results_excerpt", "results_pages")):
            raise AssertionError("synthetic records must not contain PDF or results evidence")
        if any(not isinstance(card, dict) or card.get("source_kind") == "pdf_results" or card.get("pdf_sha256")
               for card in record["evidence"]):
            raise AssertionError("synthetic records must not contain PDF or results evidence")
    if any(not isinstance(event, dict) for event in events):
        raise AssertionError("synthetic demo events must be objects")
    if any(event.get("status") in {"failed", "rejected", "truncated"} for event in events):
        raise AssertionError("synthetic demo events contain a failed or unsafe outcome")
    return {"records": len(records), "review_ready": len(approved), "status": manifest["status"]}


def run_demo(out_dir: Path) -> dict:
    records = validate_input()
    if out_dir.exists() and any(out_dir.iterdir()):
        raise ValueError("--out-dir must not already contain output")
    # The core requires its output directory to begin empty, so per-run assets live
    # beside it under a name derived from this exact output directory.
    assets_dir = out_dir.parent / f".{out_dir.name}-synthetic-assets"
    if assets_dir.exists():
        raise ValueError("synthetic demo assets already exist for this --out-dir")
    cache_dir = assets_dir / "cache"
    pdf_dir = assets_dir / "pdfs"
    cache_dir.mkdir(parents=True)
    pdf_dir.mkdir(parents=True)
    from litsearch.cli import _validate_args, build_parser, run

    parser = build_parser()
    args = parser.parse_args([
        "--topic", "pantothenate", "--from-json", str(INPUT), "--out-dir", str(out_dir),
        "--cache-dir", str(cache_dir),
        "--pdf-dir", str(pdf_dir),
        "--skip-download", "--skip-pdf-extraction",
    ])
    _validate_args(parser, args)
    with network_blocked():
        run(args)
    return validate_output(out_dir, len(records))


def main() -> int:
    parser = argparse.ArgumentParser(description="运行不联网的合成输入演示。")
    parser.add_argument("--out-dir", required=True, help="新的空输出目录")
    args = parser.parse_args()
    summary = run_demo(Path(args.out_dir))
    print(json.dumps({"synthetic_demo": True, "network": "blocked", **summary}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
