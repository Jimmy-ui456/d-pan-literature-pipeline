"""Opt-in public-API integration probe; not part of offline unittest discovery.

Run: python -m tests.live_probe --output outputs_v3/live_probe
No PDFs downloaded, no Zotero changes, no project data uploaded.
"""
from __future__ import annotations

import argparse
import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

from litsearch.core import classify_and_score
from litsearch.evidence import build_evidence_cards
from litsearch.net import HttpClient
from litsearch.outputs import _atomic_write_text, write_records_json
from litsearch.topics import load_topic
from litsearch.verification import EPMC_SEARCH, MetadataStore, epmc_record, verify_record


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    output = Path(args.output)
    if output.exists() and any(output.iterdir()):
        raise ValueError("Use a new output directory")
    output.mkdir(parents=True, exist_ok=True)
    store = MetadataStore(Path(".litsearch-cache"), HttpClient(email="", retries=1))
    report = {"checked_at_utc": datetime.now(timezone.utc).isoformat(), "cases": [],
              "scope": "Small integration probe, not a gold-standard recall/precision benchmark."}
    records = []
    for name in ("pantothenate", "lysine", "glutathione"):
        topic = load_topic(name)
        query = f'{topic.query("europepmc")} AND TITLE_ABS:"metabolic engineering" AND SRC:MED'
        data, source = store.get(EPMC_SEARCH + urlencode({"query": query, "format": "json", "resultType": "core", "pageSize": 2}))
        items = (data.get("resultList") or {}).get("result") or []
        if len(items) < 1:
            raise RuntimeError(f"No probe records for {name}")
        for item in items:
            rec = epmc_record(item)
            verify_record(rec, store)
            classify_and_score(rec, topic)
            build_evidence_cards(rec, topic)
            records.append(rec)
            report["cases"].append({"topic": name, "case": "live_metadata", "doi": rec.doi, "title": rec.title,
                                    "identity": rec.identity_status, "relevance": rec.relevance_status,
                                    "source": source, "eligible_cards": sum(c["eligible"] for c in rec.evidence)})
        impostor = copy.deepcopy(records[-1])
        impostor.title = "Fabricated title deliberately substituted for adversarial testing"
        verify_record(impostor, store)
        report["cases"].append({"topic": name, "case": "swapped_title", "doi": impostor.doi,
                                "identity": impostor.identity_status, "expected": "metadata_conflict"})
        _atomic_write_text(output / "probe_report.json", json.dumps(report, ensure_ascii=False, indent=2))
        if impostor.identity_status != "metadata_conflict":
            raise AssertionError("A substituted title was not quarantined")
        print(name + ": " + ", ".join(r.identity_status for r in records if r.topic_id == name), flush=True)
    write_records_json(output / "live_records.json", records)
    _atomic_write_text(output / "probe_report.json", json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
