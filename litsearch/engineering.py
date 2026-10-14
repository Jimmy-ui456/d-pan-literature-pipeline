"""Engineering search planning and report preflight; never scientific approval.

Separate from the microbial screening/review schema. A report cannot acquire
evidence merely by mentioning a topic, finding a DOI, or importing old claims.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import unicodedata
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, urlsplit, urlunsplit
from zipfile import ZipFile

from .core import norm_doi, norm_title, title_key
from .models import Record
from .net import HttpClient, NetworkError
from .outputs import _atomic_write_text, _write_csv

VERSION = "engineering-preflight-1.0"
KINDS = {"research", "review", "facility", "vendor", "institution", "hazard", "bibliography"}
LEVELS = {"full_text", "official_document", "abstract", "excerpt", "bibliographic", "unconfirmed"}
CONTEXT = ("target_material", "protected_material", "composition", "chemistry", "temperature",
           "duration", "geometry", "metric", "value", "unit", "measurement", "uncertainty", "limitations")
NUM = r"\d+(?:\.\d+)?"
MEASURE_RE = re.compile(
    rf"(?<![\w.]){NUM}(?:\s*(?:±|[-–—]|至|to)\s*{NUM})?\s*"
    r"(?:nm\s*/\s*(?:min|s|cycle)|[ÅÅ]\s*/\s*(?:min|s|cycle)|"
    r"[uμµ]m\s*/\s*(?:h|min)|nm|[uμµ]m|mm|[ÅÅ]|°\s*C|℃|"
    r"wph|片\s*/\s*(?:小时|h)|nm\s*/\s*循环|%|％|英寸|腔室|腔|片\s*/\s*批|"
    r"LPM|M(?![a-z])|:\s*\d+(?:\.\d+)?)", re.I)
URL_RE = re.compile(r"https?://[^\s<>]+")
DOI_RE = re.compile(r"10\.\d{4,9}/[^\s<>]+")
NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def text_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_json(path: Path) -> dict:
    if path.stat().st_size > 20 * 1024 * 1024:
        raise ValueError("JSON input exceeds 20 MB")
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("JSON root must be an object")
    return data


def load_protocol(name: str) -> dict:
    path = Path(name)
    if not path.is_file():
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", name):
            raise ValueError("invalid protocol name")
        path = Path(__file__).with_name("profiles") / (name + ".json")
    p = read_json(path)
    if p.get("schema_version") != 1 or p.get("domain") != "engineering":
        raise ValueError("engineering protocol schema_version=1 required")
    if not isinstance(p.get("facets"), list) or not p["facets"]:
        raise ValueError("protocol requires facets")
    ids = set()
    for f in p["facets"]:
        if not isinstance(f, dict) or set(f) != {"id", "label", "terms", "query", "source_kinds"}:
            raise ValueError("invalid facet fields")
        if not isinstance(f["id"], str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", f["id"]) or f["id"] in ids:
            raise ValueError("invalid or duplicate facet ID")
        ids.add(f["id"])
        for k in ("label", "query"):
            if not isinstance(f[k], str) or not f[k].strip():
                raise ValueError("facet label/query required")
        for k in ("terms", "source_kinds"):
            if not isinstance(f[k], list) or not f[k] or any(not isinstance(x, str) or not x for x in f[k]):
                raise ValueError("invalid facet vocabulary")
        if not set(f["source_kinds"]) <= KINDS:
            raise ValueError("unknown source kind")
    return p


def canonical_url(url: str) -> str:
    parts = urlsplit(url.rstrip(".,;。；"))
    # Keep query parameters: release versions and J-GLOBAL identities may live
    # there. Never merge two catalogs solely by a vendor/domain name.
    return urlunsplit((parts.scheme.casefold(), parts.netloc.casefold(), parts.path.rstrip("/"), parts.query, ""))


def load_catalog(path: Path) -> list[dict]:
    data = read_json(path)
    if data.get("schema_version") != 1 or not isinstance(data.get("sources"), list):
        raise ValueError("catalog schema_version=1 with sources required")
    seen = set()
    for s in data["sources"]:
        if (not isinstance(s, dict) or not isinstance(s.get("id"), str) or not s["id"]
                or s["id"] in seen or s.get("kind") not in KINDS or s.get("access_level") not in LEVELS):
            raise ValueError("invalid source ID/kind/access level")
        seen.add(s["id"])
        for k in ("citation", "journal", "doi", "audit_note"):
            if not isinstance(s.get(k, ""), str):
                raise ValueError("invalid source metadata")
        if not isinstance(s.get("urls"), list) or not s["urls"] or any(
                not isinstance(u, str) or urlsplit(u).scheme not in {"https", "http"} or not urlsplit(u).netloc
                for u in s["urls"]):
            raise ValueError("source URL list required")
        if s.get("doi") and not norm_doi(s["doi"]):
            raise ValueError("invalid catalog DOI")
        if (not isinstance(s.get("journal_aliases", []), list)
                or any(not isinstance(x, str) or not x.strip() for x in s.get("journal_aliases", []))):
            raise ValueError("invalid journal alias list")
    return data["sources"]


def document_paragraphs(path: Path) -> list[str]:
    if path.suffix.lower() == ".docx":
        with ZipFile(path) as z:
            info = z.getinfo("word/document.xml")
            if info.file_size > 30 * 1024 * 1024:
                raise ValueError("DOCX XML exceeds 30 MB")
            root = ET.fromstring(z.read(info))
            return ["".join(
                t.text or "" for t in p.findall(".//w:t", NS)) for p in root.findall(".//w:body//w:p", NS)]
    if path.suffix.lower() not in {".md", ".txt"}:
        raise ValueError("document must be DOCX/Markdown/plain text")
    return path.read_text(encoding="utf-8-sig").splitlines()


def split_document(paragraphs: list[str]) -> tuple[list[str], dict[str, str], list[str]]:
    body, refs, errors = [], {}, []
    in_refs, current = False, ""
    for raw in paragraphs:
        line = raw.replace("\u200b", "").strip()
        if not line:
            continue
        if re.fullmatch(r"(?:#+\s*)?(?:附录\s*[A-Z一二三0-9]+\s*[|｜:：、]?\s*)?参考文献(?:与(?:来源|可核验原始来源))?", line):
            in_refs = True
            continue
        if not in_refs:
            body.append(line)
            continue
        m = re.match(r"^\[(\d+)\]\s*(.*)", line)
        if m:
            current = m[1]
            if current in refs:
                errors.append("duplicate_reference_number:" + current)
            refs[current] = m[2]
        elif current:
            refs[current] += " " + line
    if not in_refs:
        errors.append("bibliography_heading_missing")
    return body, refs, errors


def _citations(text: str) -> set[str]:
    ids = set(re.findall(r"\[(\d+)\]", text))
    for start, end in re.findall(r"\[(\d+)\]\s*[-–—]\s*\[(\d+)\]", text):
        a, b = int(start), int(end)
        if 0 < b - a <= 1000:
            ids.update(str(n) for n in range(a, b + 1))
    return ids


def _contains(text: str, term: str) -> bool:
    text, term = (unicodedata.normalize("NFKC", x).casefold() for x in (text, term))
    return bool(re.search(r"(?<![A-Za-z0-9])" + re.escape(term) + r"(?![A-Za-z0-9])", text))


def quantity_hints(text: str) -> list[str]:
    # Exclude identifiers, URLs and citation numbering; hints never become data.
    clean = DOI_RE.sub(" ", URL_RE.sub(" ", text))
    clean = re.sub(r"\[\d+\]", " ", clean)
    return [m[0] for m in MEASURE_RE.finditer(clean)]


def audit_report(document: Path, catalog_path: Path, protocol: dict, claims_path: Path | None = None) -> dict:
    sources = load_catalog(catalog_path)
    by_id = {s["id"]: s for s in sources}
    body, refs, hard = split_document(document_paragraphs(document))
    used = set().union(*(_citations(x) for x in body)) if body else set()
    hard.extend("undefined_citation:" + n for n in sorted(used - refs.keys(), key=int))
    references, map_ids = [], {}
    identity_groups: dict[str, list[str]] = {}
    for n, entry in refs.items():
        dois = {norm_doi(x.rstrip(".,;。；")) for x in DOI_RE.findall(entry)}
        urls = {canonical_url(x) for x in URL_RE.findall(entry)}
        matched = [s for s in sources if (s.get("doi") and norm_doi(s["doi"]) in dois)
                   or urls.intersection(canonical_url(u) for u in s["urls"])]
        map_ids[n] = {s["id"] for s in matched}
        notes = []
        if not matched:
            notes.append("catalog_match_missing")
        for s in matched:
            if s.get("journal") and title_key(s["journal"]) not in title_key(entry):
                # A known journal alias can be explicit in the catalog. No fuzzy
                # guess turns Micromachines into ACS Omega or Nanomaterials.
                aliases = s.get("journal_aliases", [])
                if not any(title_key(x) in title_key(entry) for x in aliases):
                    notes.append("journal_missing_or_conflicting:" + s["journal"])
                    hard.append("reference_" + n + ":journal_missing_or_conflicting")
            if s["access_level"] in {"bibliographic", "unconfirmed"}:
                notes.append("limited_to_" + s["access_level"])
        # One entry may supply a DOI and its duplicate only a publisher URL.
        # Resolve both to the same catalog identity before counting duplicates.
        keys = ["doi:" + norm_doi(s["doi"]) if s.get("doi") else "source:" + s["id"] for s in matched]
        if not keys:
            keys = ["doi:" + d for d in sorted(dois)]
        for key in keys:
            identity_groups.setdefault(key, []).append(n)
        references.append({"number": n, "source_ids": sorted(map_ids[n]), "cited": n in used,
                           "access_levels": sorted({s["access_level"] for s in matched}), "issues": notes,
                           "entry": entry})
    duplicates = [list(v) for v in dict.fromkeys(tuple(sorted(set(v), key=int)) for v in identity_groups.values()
                                               if len(set(v)) > 1)]
    coverage = []
    for f in protocol["facets"]:
        hits = [i for i, p in enumerate(body) if any(_contains(p, term) for term in f["terms"])]
        cited_sources = set().union(*(map_ids.get(n, set()) for i in hits for n in _citations(body[i]))) if hits else set()
        kinds = {by_id[s]["kind"] for s in cited_sources}
        supported = bool(kinds.intersection(f["source_kinds"]))
        coverage.append({"id": f["id"], "label": f["label"], "mention_count": len(hits),
                         "cited_source_kinds": sorted(kinds), "has_matching_source_kind": supported,
                         "status": "mentioned_with_source" if hits and supported else (
                             "mentioned_without_matching_source" if hits else "missing"),
                         "semantic_completeness": "not_assessed"})
    claim_checks: dict[str, dict] = {}
    if claims_path:
        claims = read_json(claims_path)
        if claims.get("schema_version") != 1 or claims.get("document_sha256") != digest(document):
            raise ValueError("claim ledger is stale or has invalid schema")
        if not isinstance(claims.get("checks"), list):
            raise ValueError("claim ledger requires checks")
        for c in claims["checks"]:
            key = c.get("paragraph_sha256") if isinstance(c, dict) else None
            if not isinstance(key, str) or key in claim_checks or key not in {text_digest(p) for p in body}:
                raise ValueError("claim ledger has duplicate/unmatched paragraph hash")
            claim_checks[key] = c
    quantities = []
    for i, paragraph in enumerate(body, 1):
        hints = quantity_hints(paragraph)
        if not hints:
            continue
        check = claim_checks.get(text_digest(paragraph))
        issues = []
        if check is None:
            issues.append("structured_data_check_missing")
        else:
            context = check.get("context", {})
            if not isinstance(context, dict) or any(not isinstance(context.get(k), str) or not context[k] for k in CONTEXT):
                issues.append("engineering_context_incomplete")
            elif any(context[k] == "not_reported" for k in CONTEXT[:-1]):
                issues.append("condition_or_measurement_not_reported")
            cids = check.get("source_ids", [])
            cited = set().union(*(map_ids.get(n, set()) for n in _citations(paragraph)))
            if (not isinstance(cids, list) or not cids or any(not isinstance(s, str) for s in cids)
                    or not set(cids) <= cited):
                issues.append("claim_source_not_cited_here")
            elif any(by_id[s]["access_level"] in {"bibliographic", "unconfirmed"} for s in cids):
                issues.append("access_level_cannot_support_data")
            elif (len(cids) != 1 or canonical_url(str(check.get("source_url", ""))) not in {
                    canonical_url(u) for u in by_id[cids[0]]["urls"]}):
                issues.append("snapshot_source_binding_missing")
            snap = check.get("snapshot_path", "")
            path = (claims_path.parent / snap).resolve() if snap else None
            if not path or not path.is_file() or digest(path) != check.get("snapshot_sha256"):
                issues.append("source_snapshot_missing_or_changed")
            else:
                quote = check.get("quote", "")
                content = path.read_text(encoding="utf-8-sig")
                if not quote or norm_title(quote) not in norm_title(content) or not check.get("locator"):
                    issues.append("quote_or_locator_not_replayable")
                elif any(norm_title(x) not in norm_title(quote) for x in hints):
                    issues.append("numeric_hint_not_in_evidence_quote")
                elif (isinstance(context, dict) and isinstance(context.get("value"), str)
                      and isinstance(context.get("unit"), str)):
                    value = norm_title(context["value"])
                    unit = norm_title(context["unit"])
                    if (not re.search(r"(?<![\d.])" + re.escape(value) + r"(?![\d.])", norm_title(quote))
                            or unit not in norm_title(quote)):
                        issues.append("context_value_or_unit_not_in_quote")
        quantities.append({"paragraph": i, "paragraph_sha256": text_digest(paragraph), "hints": hints,
                           "text": paragraph, "issues": issues,
                           "status": "needs_review" if issues else "mechanical_checks_complete",
                           "scientific_support": "not_automatically_assessed"})
    return {"schema_version": 1, "tool_version": VERSION, "status": "preflight_only",
            "scientific_approval": False,
            "inputs": {"document": {"path": str(document.resolve()), "sha256": digest(document)},
                       "catalog": {"path": str(catalog_path.resolve()), "sha256": digest(catalog_path)},
                       "claims_sha256": digest(claims_path) if claims_path else None,
                       "protocol_sha256": text_digest(json.dumps(protocol, sort_keys=True, ensure_ascii=False))},
            "hard_issues": hard, "references": references, "duplicate_groups": duplicates,
            "uncited_reference_numbers": sorted(refs.keys() - used, key=int),
            "coverage": coverage, "quantity_checks": quantities,
            "review_items": sum(bool(x["issues"]) for x in references) + sum(bool(x["issues"]) for x in quantities)
                            + sum(x["status"] != "mentioned_with_source" for x in coverage) + len(duplicates)}


def search_crossref(protocol: dict, client: HttpClient, out: Path, budget: int = 30, page_size: int = 20) -> dict:
    """Bounded keyword candidate retrieval with raw page checkpoints.

    The bounded relevance-ranked search uses offsets (maximum 1000 records),
    rather than deep cursors which may use index order instead of relevance.
    Completion uses item count/total-results. Repeated DOI-only pages fail.
    No keyword hit is promoted to verified identity or relevant evidence.
    """
    records, facets, identities = [], [], {}
    for facet in protocol["facets"]:
        received, page, seen_pages = 0, 0, set()
        result = {"facet": facet["id"], "query": facet["query"], "status": "running", "received": 0}
        while received < budget:
            rows = min(page_size, budget - received)
            url = "https://api.crossref.org/works?" + urlencode({"query": facet["query"], "sort": "score",
                                                               "order": "desc", "rows": rows, "offset": received})
            try:
                data = client.get_json(url)
                page += 1
                raw_path = out / f"raw_{facet['id']}_{page:03}.json"
                _atomic_write_text(raw_path, json.dumps(data, ensure_ascii=False, indent=2))
                _atomic_write_text(out / f"provenance_{facet['id']}_{page:03}.json", json.dumps({
                    "url": url, "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
                    "raw_file": raw_path.name, "sha256": digest(raw_path)}, ensure_ascii=False, indent=2))
                msg = data.get("message")
                if not isinstance(msg, dict) or not isinstance(msg.get("items"), list) or type(msg.get("total-results")) is not int:
                    raise ValueError("invalid Crossref result shape")
                echoed = msg.get("query")
                if (not isinstance(echoed, dict) or not isinstance(echoed.get("search-terms"), str)
                        or norm_title(echoed["search-terms"]) != norm_title(facet["query"])):
                    raise ValueError("Crossref did not confirm the requested search terms")
                items, total = msg["items"], msg["total-results"]
                if total < received + len(items) or len(items) > rows:
                    raise ValueError("invalid Crossref counts")
                batch = []
                for item in items:
                    if (not isinstance(item, dict) or not norm_doi(item.get("DOI")) or not isinstance(item.get("title"), list)
                            or not item["title"] or any(not isinstance(x, str) for x in item["title"])):
                        raise ValueError("Crossref candidate DOI/title missing")
                    date = (item.get("published") or item.get("issued") or {}).get("date-parts") or [[]]
                    rec = Record(title=norm_title(" ".join(item["title"])), doi=norm_doi(item["DOI"]),
                                 year=str(date[0][0]) if date[0] else "",
                                 journal=norm_title(" ".join(item.get("container-title") or [])),
                                 authors=[" ".join(str(a.get(k) or "") for k in ("given", "family")).strip()
                                          for a in item.get("author") or []],
                                 volume=str(item.get("volume") or ""), issue=str(item.get("issue") or ""),
                                 pages=str(item.get("page") or item.get("article-number") or ""),
                                 publication_types=[str(item.get("type") or "")],
                                 url="https://doi.org/" + norm_doi(item["DOI"]),
                                 abstract=norm_title(item.get("abstract") or ""),
                                 source_ids=["Crossref-query:" + facet["id"]])
                    batch.append(rec)
                sig = tuple(sorted(r.doi for r in batch))
                if batch and sig in seen_pages:
                    raise ValueError("repeated Crossref DOI page")
                seen_pages.add(sig)
                for r in batch:
                    existing = identities.get(r.doi)
                    if existing and title_key(existing.title) != title_key(r.title):
                        records.append(r)  # preserve conflicting identities for later verification
                    elif existing:
                        existing.source_ids = sorted(set(existing.source_ids + r.source_ids))
                    else:
                        identities[r.doi] = r
                        records.append(r)
                received += len(batch)
                result.update(received=received, total=total)
                _atomic_write_text(out / "raw_records.json", json.dumps([r.to_dict() for r in records], ensure_ascii=False, indent=2))
                if received >= total or len(items) < rows:
                    if received < total:
                        raise ValueError("short Crossref page before total exhausted")
                    result["status"] = "complete_for_query"
                    break
                if received >= budget:
                    result["status"] = "budget_truncated"
                    break
            except (NetworkError, ValueError, TypeError, KeyError, AttributeError, IndexError) as exc:
                result.update(status="failed", error=str(exc))
                break
        facets.append(result)
        _atomic_write_text(out / "search_progress.json", json.dumps(facets, ensure_ascii=False, indent=2))
    return {"schema_version": 1, "source": "crossref", "facets": facets,
            "candidate_count": len(records), "identity_verified": False, "relevance_verified": False,
            "protocol_sha256": text_digest(json.dumps(protocol, sort_keys=True, ensure_ascii=False)),
            "status": "failed" if any(x["status"] == "failed" for x in facets) else (
                "budget_truncated" if any(x["status"] == "budget_truncated" for x in facets) else "complete_for_queries")}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="工程主题分面检索、数据条件与引用预检；不代替专业核查")
    parser.add_argument("action", choices=("plan", "search", "audit"))
    parser.add_argument("--protocol", default="wet_etch")
    parser.add_argument("--document")
    parser.add_argument("--catalog")
    parser.add_argument("--claims")
    parser.add_argument("--budget", type=int, default=30, help="每个主题Crossref候选预算；非穷尽检索")
    parser.add_argument("--page-size", type=int, default=20)
    parser.add_argument("--email", default="", help="可选真实联系邮箱；不写入运行记录")
    parser.add_argument("--out-dir", required=True)
    args = parser.parse_args(argv)
    if not 1 <= args.budget <= 1000 or not 1 <= args.page_size <= 100:
        parser.error("budget 1–1000; page-size 1–100 required")
    if args.action == "audit" and (not args.document or not args.catalog):
        parser.error("audit requires --document and --catalog")
    out = Path(args.out_dir)
    if out.exists() and any(out.iterdir()):
        parser.error("output directory must be new/empty")
    protocol = load_protocol(args.protocol)
    out.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(out / "protocol_snapshot.json", json.dumps(protocol, ensure_ascii=False, indent=2))
    manifest = {"tool_version": VERSION, "action": args.action, "started_at_utc": datetime.now(timezone.utc).isoformat(),
                "network_used": args.action == "search", "status": "running", "scientific_approval": False}
    manifest["code_sha256"] = {p.name: digest(p) for p in (Path(__file__), Path(__file__).with_name("verification.py"))}
    _atomic_write_text(out / "run_manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    try:
        if args.action == "plan":
            result = {"facets": protocol["facets"], "official_queries": protocol.get("official_queries", []),
                      "status": "planned_not_executed"}
        elif args.action == "search":
            result = search_crossref(protocol, HttpClient(email=args.email), out, args.budget, args.page_size)
        else:
            result = audit_report(Path(args.document), Path(args.catalog), protocol, Path(args.claims) if args.claims else None)
            fields = ["id", "label", "mention_count", "status", "semantic_completeness"]
            _write_csv(out / "coverage.csv", [{k: x[k] for k in fields} for x in result["coverage"]], fields)
            _write_csv(out / "quantity_checks.csv", [{"paragraph": x["paragraph"], "hints": " ; ".join(x["hints"]),
                       "issues": " ; ".join(x["issues"]), "text": x["text"]} for x in result["quantity_checks"]],
                       ["paragraph", "hints", "issues", "text"])
        _atomic_write_text(out / "result.json", json.dumps(result, ensure_ascii=False, indent=2))
        code = 2 if result.get("hard_issues") or result.get("review_items") or result["status"] == "failed" else (
            3 if result["status"] == "budget_truncated" else 0)
        manifest.update(status=result["status"], exit_code=code, finished_at_utc=datetime.now(timezone.utc).isoformat())
        _atomic_write_text(out / "run_manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        print(json.dumps({"output": str(out.resolve()), "status": result["status"], "exit_code": code}, ensure_ascii=False))
        return code
    except (ValueError, OSError) as exc:
        manifest.update(status="failed", error=str(exc))
        _atomic_write_text(out / "run_manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        parser.exit(2, str(exc) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
