"""Opt-in API contract and adapter probe, independent of project papers/caches.

Without --online this only writes a plan. A successful online report proves the
bounded service contracts exercised in that run, not scientific accuracy/recall.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import html
import json
import os
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .core import norm_doi, norm_title
from .models import Record
from .net import HttpClient, NetworkError, Response, ResponseTooLarge, redact_url, validate_public_http_url
from .outputs import _atomic_write_text
from .pdfs import extract_text_from_pdf, pdf_matches_record
from .sources import _openalex_batch, fetch_europepmc, fetch_pubmed, record_from_openalex
from .verification import EPMC_SEARCH, _metadata_conflicts, epmc_record, verify_record


class ContractError(ValueError):
    """A response cannot substantiate the declared API contract."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def fresh_output(path: Path) -> Path:
    absolute = path.absolute()
    if any(part.is_symlink() for part in (absolute, *absolute.parents)):
        raise ValueError("Output path must not contain symlinks")
    if absolute.exists() and (not absolute.is_dir() or any(absolute.iterdir())):
        raise ValueError("Use a fresh empty output directory")
    absolute.mkdir(parents=True, exist_ok=True)
    return absolute


def integer(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not re.fullmatch(r"\d+", str(value)):
        raise ContractError("Invalid nonnegative count")
    return int(value)


def page_contract(total: int, items: list, offset: int, size: int) -> None:
    if len(items) != min(size, max(0, total - offset)):
        raise ContractError("Incomplete or overfull result page")


def epmc_contract(data: dict, size: int | None = None, offset: int = 0) -> tuple[list[dict], int]:
    if data.get("error") or not isinstance(data.get("resultList"), dict):
        raise ContractError("Invalid Europe PMC envelope")
    items = data["resultList"].get("result")
    total = integer(data.get("hitCount"))
    if not isinstance(items, list) or any(not isinstance(i, dict) for i in items):
        raise ContractError("Invalid Europe PMC result array")
    if size is not None:
        page_contract(total, items, offset, size)
    elif total != len(items):
        raise ContractError("Identity search is truncated")
    for item in items:
        if not isinstance(item.get("title"), str) or not norm_title(item["title"]) or any(
                not isinstance(item.get(field), str) or not item[field] for field in ("source", "id")):
            raise ContractError("Europe PMC item lacks identity/title")
        source, identifier = item["source"], item["id"]
        if (source not in {"MED", "PMC", "AGR", "CBA", "CTX", "ETH", "HIR", "NBK", "PAT", "PPR"}
                or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", identifier)
                or (source == "MED" and not re.fullmatch(r"[0-9]+", identifier))
                or (source == "PMC" and not re.fullmatch(r"PMC[0-9]+", identifier))):
            raise ContractError("Europe PMC source/identifier is invalid")
        for name in ("journalInfo", "authorList", "fullTextUrlList", "pubTypeList"):
            if name in item and not isinstance(item[name], dict):
                raise ContractError("Invalid Europe PMC nested object")
        epmc_record(item)  # Exercise the production adapter too.
    return items, total


class SnapshotClient:
    """Bounded public-only transport; no URL query or exception text is logged.

    Snapshot bytes are exact unless a configured credential/contact is echoed;
    both original and saved digests make such redaction explicit. No shared cache.
    """
    def __init__(self, client: Any, output: Path, *, page_size: int, max_bytes: int,
                 timeout: int, secrets: tuple[str, ...] = ()) -> None:
        self.client, self.output = client, output
        self.page_size, self.max_bytes, self.timeout = page_size, max_bytes, timeout
        self.secrets = tuple(s for s in secrets if s)
        self.snapshots: list[dict] = []
        self.memory: dict[str, tuple[dict, dict]] = {}
        self.epmc_offset = 0
        self.epmc_total: int | None = None
        self.epmc_ids: set[tuple[str, str]] = set()
        self.pubmed_offset = 0
        self.pubmed_size = page_size
        self.pubmed_total: int | None = None

    def redact_body(self, body: bytes) -> bytes:
        for secret in self.secrets:
            variants = {secret, urllib.parse.quote(secret, safe=""), urllib.parse.quote_plus(secret),
                        json.dumps(secret, ensure_ascii=True)[1:-1],
                        json.dumps(secret, ensure_ascii=False)[1:-1],
                        html.escape(secret, quote=True), html.escape(secret, quote=False),
                        html.escape(secret, quote=True).replace("&#x27;", "&apos;")}
            for token in sorted(variants, key=len, reverse=True):
                body = body.replace(token.encode(), b"[REDACTED]")
        return body

    def safe_label(self, value: str) -> str:
        return self.redact_body(value.encode("utf-8")).decode("utf-8")

    def request(self, url: str, *, data: bytes | None = None, accept: str = "application/json") -> Response:
        validate_public_http_url(url)
        response = self.client.request(url, data=data, accept=accept, timeout=self.timeout,
                                       max_bytes=self.max_bytes)
        validate_public_http_url(response.final_url)
        if len(response.body) > self.max_bytes:
            raise ResponseTooLarge("Probe response exceeds byte budget")
        body = self.redact_body(response.body)
        name = f"response_{len(self.snapshots) + 1:04d}.bin"
        (self.output / name).write_bytes(body)
        self.snapshots.append({"file": name, "fetched_at_utc": utc_now(), "endpoint": self.safe_label(redact_url(response.final_url)),
                               "status": response.status, "bytes": len(response.body),
                               "raw_sha256": hashlib.sha256(response.body).hexdigest(),
                               "saved_sha256": hashlib.sha256(body).hexdigest(), "redacted": body != response.body})
        if response.status != 200:
            raise NetworkError("Non-success HTTP status", status=response.status)
        return response

    def json(self, url: str) -> dict:
        response = self.request(url)
        try:
            value = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ContractError("Invalid JSON") from exc
        if not isinstance(value, dict):
            raise ContractError("JSON response must be an object")
        return value

    def get_json(self, url: str, **_: Any) -> dict:
        parts = urllib.parse.urlsplit(url)
        params = dict(urllib.parse.parse_qsl(parts.query))
        if parts.path.endswith("esearch.fcgi"):
            params.update(retstart=str(self.pubmed_offset), retmax=str(self.pubmed_size), sort="pub date")
            url = urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(params)))
            value = self.json(url)
            search = value.get("esearchresult")
            if value.get("error") or not isinstance(search, dict) or search.get("errorlist"):
                raise ContractError("Invalid PubMed search envelope")
            ids = search.get("idlist")
            total = integer(search.get("count"))
            if not isinstance(ids, list) or any(not isinstance(i, str) or not re.fullmatch(r"\d+", i) for i in ids):
                raise ContractError("Invalid PubMed IDs")
            if len(set(ids)) != len(ids):
                raise ContractError("Duplicate PubMed IDs")
            page_contract(total, ids, self.pubmed_offset, self.pubmed_size)
            if self.pubmed_total is not None and total != self.pubmed_total:
                raise ContractError("PubMed result count changed during pagination")
            self.pubmed_total = total
            return value
        if url.startswith(EPMC_SEARCH) and "cursorMark" in params:
            size = min(self.page_size, integer(params["pageSize"]))
            params["pageSize"] = str(size)
            url = urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(params)))
            value = self.json(url)
            items, total = epmc_contract(value, size, self.epmc_offset)
            if self.epmc_total is not None and total != self.epmc_total:
                raise ContractError("Europe PMC count changed during pagination")
            identities = {(str(i["source"]), str(i["id"])) for i in items}
            if len(identities) != len(items) or identities & self.epmc_ids:
                raise ContractError("Duplicate Europe PMC IDs across pages")
            self.epmc_total = total
            self.epmc_ids.update(identities)
            self.epmc_offset += len(items)
            return value
        return self.json(url)

    def post_bytes(self, url: str, data: bytes, **_: Any) -> Response:
        response = self.request(url, data=data, accept="application/xml")
        root = ET.fromstring(response.body)
        if root.tag != "PubmedArticleSet" or root.find(".//ERROR") is not None:
            raise ContractError("Invalid PubMed fetch envelope")
        return response

    def get(self, url: str) -> tuple[dict, dict]:
        # Production verify_record accepts a store protocol; keep it in memory.
        if url not in self.memory:
            value = self.json(url)
            if url.startswith(EPMC_SEARCH):
                epmc_contract(value)
            elif "/works?" in url:
                msg = value.get("message")
                if value.get("status") != "ok" or not isinstance(msg, dict) or not isinstance(msg.get("items"), list):
                    raise ContractError("Invalid Crossref updates envelope")
                page_contract(integer(msg.get("total-results")), msg["items"], 0, 100)
            elif "api.crossref.org/works/" in url:
                if value.get("status") != "ok" or not isinstance(value.get("message"), dict):
                    raise ContractError("Invalid Crossref identity envelope")
            self.memory[url] = (value, {"url": self.safe_label(redact_url(url)), "fetched_at_utc": utc_now(),
                                       "sha256": hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()})
        return self.memory[url]


class Query:
    def __init__(self, query: str) -> None:
        self.value = query

    def query(self, source: str) -> str:
        return self.value


def crossref_search(client: SnapshotClient, query: str, page_size: int, max_pages: int) -> dict:
    cursor, seen, ids, total, count = "*", set(), set(), None, 0
    for _ in range(max_pages):
        if cursor in seen:
            raise ContractError("Crossref cursor repeated")
        seen.add(cursor)
        data = client.json("https://api.crossref.org/works?" + urllib.parse.urlencode(
            {"query": query, "rows": page_size, "cursor": cursor}))
        message = data.get("message")
        if data.get("status") != "ok" or not isinstance(message, dict) or not isinstance(message.get("items"), list):
            raise ContractError("Invalid Crossref search envelope")
        available = integer(message.get("total-results"))
        if total is not None and total != available:
            raise ContractError("Crossref count changed during pagination")
        total = available
        items = message["items"]
        page_contract(total, items, count, page_size)
        for item in items:
            if not isinstance(item, dict) or not norm_doi(item.get("DOI")) or not isinstance(item.get("title"), list) or not item["title"]:
                raise ContractError("Crossref result lacks DOI/title")
            if any(not isinstance(title, str) or not norm_title(title) for title in item["title"]):
                raise ContractError("Crossref invalid title")
            doi = norm_doi(item["DOI"])
            if doi in ids:
                raise ContractError("Duplicate Crossref DOI across pages")
            ids.add(doi)
        count += len(items)
        if count == total:
            return {"status": "pass", "available": total, "retrieved": count, "pages": len(seen)}
        cursor = message.get("next-cursor")
        if not isinstance(cursor, str) or not cursor:
            raise ContractError("Missing Crossref next cursor")
    return {"status": "truncated", "available": total, "retrieved": count, "pages": len(seen)}


def pubmed_search(client: SnapshotClient, query: str, page_size: int, max_pages: int, email: str,
                  sleeper: Callable[[float], None]) -> dict:
    records: list[Record] = []
    ids: set[str] = set()
    client.pubmed_total = None
    for page in range(max_pages):
        client.pubmed_offset, client.pubmed_size = page * page_size, page_size
        batch = fetch_pubmed(page_size, client=client, email=email, topic=Query(query), sleeper=sleeper)
        if any(rec.pmid in ids for rec in batch):
            raise ContractError("Duplicate PubMed IDs across pages")
        ids.update(rec.pmid for rec in batch)
        records.extend(batch)
        total = client.pubmed_total
        if total is None:
            raise ContractError("PubMed count missing")
        if len(records) == total:
            return {"status": "pass", "available": total, "retrieved": len(records), "pages": page + 1}
        if (page + 1) * page_size >= 10000:
            break  # ESearch PubMed ceiling; never call an inaccessible page.
    return {"status": "truncated", "available": client.pubmed_total, "retrieved": len(records), "pages": page + 1}


def openalex_search(client: SnapshotClient, query: str, key: str, page_size: int, max_pages: int) -> dict:
    cursor, seen, ids, count, total = "*", set(), set(), 0, None
    for page in range(max_pages):
        if cursor in seen:
            raise ContractError("OpenAlex cursor repeated")
        seen.add(cursor)
        data = client.json("https://api.openalex.org/works?" + urllib.parse.urlencode(
            {"search": query, "cursor": cursor, "per-page": page_size, "api_key": key}))
        available = integer((data.get("meta") or {}).get("count"))
        if total is not None and total != available:
            raise ContractError("OpenAlex count changed during pagination")
        total = available
        # Production helper expects single-page count; retain global count above.
        local = copy.deepcopy(data)
        local["meta"]["count"] = max(0, total - count)
        items, _ = _openalex_batch(local, page_size)
        for item in items:
            if not isinstance(item.get("id"), str) or not re.fullmatch(r"https://openalex\.org/W[0-9]+", item["id"]) or item["id"] in ids:
                raise ContractError("OpenAlex missing or duplicated identity")
            ids.add(item["id"])
            record_from_openalex(item, "probe")
        count += len(items)
        if count == total:
            return {"status": "pass", "available": total, "retrieved": count, "pages": page + 1}
        cursor = data["meta"].get("next_cursor")
        if not isinstance(cursor, str) or not cursor:
            raise ContractError("Missing OpenAlex next cursor")
    return {"status": "truncated", "available": total, "retrieved": count, "pages": max_pages}


def identity(client: SnapshotClient, doi: str, title: str) -> tuple[dict, Record]:
    rec = Record(title=title, doi=doi)
    verify_record(rec, client)
    sources = {check["source"]: check["status"] for check in rec.identity_checks}
    if rec.identity_status != "verified" or sources.get("crossref") != "matched" or sources.get("europepmc") != "matched" or sources.get("crossref_updates") != "checked":
        return {"status": "fail", "reason": "expected_identity_not_corroborated",
                "identity_status": rec.identity_status, "source_checks": sources}, rec
    impostor = copy.deepcopy(rec)
    impostor.title = title + " deliberately substituted unrelated title"
    verify_record(impostor, client)
    if impostor.identity_status != "metadata_conflict":
        raise ContractError("Swapped-title adversarial record was accepted")
    return {"status": "pass", "positive": rec.identity_status, "swapped_title": impostor.identity_status}, rec


def openalex_identity(client: SnapshotClient, doi: str, title: str, key: str) -> dict:
    value = client.json("https://api.openalex.org/works/" + urllib.parse.quote("https://doi.org/" + doi, safe="")
                        + "?" + urllib.parse.urlencode({"api_key": key}))
    local = {"meta": {"count": 1}, "results": [value]}
    items, _ = _openalex_batch(local, 1)
    if not isinstance(items[0].get("id"), str) or not re.fullmatch(r"https://openalex\.org/W[0-9]+", items[0]["id"]):
        raise ContractError("OpenAlex work identity is invalid")
    remote = record_from_openalex(items[0], "probe")
    if remote.doi != doi or _metadata_conflicts(Record(title, doi=doi), remote):
        raise ContractError("OpenAlex exact DOI identity was not corroborated")
    if not _metadata_conflicts(Record(title + " substituted title", doi=doi), remote):
        raise ContractError("OpenAlex swapped title was accepted")
    return {"status": "pass", "adapter": "record_from_openalex", "swapped_title": "rejected"}


def pdf_stage(client: SnapshotClient, record: Record, max_candidates: int = 3) -> dict:
    # Only the Europe PMC OA metadata explicitly supplies permitted PDF links.
    values, _ = client.get(EPMC_SEARCH + urllib.parse.urlencode(
        {"query": f'DOI:"{record.doi}"', "format": "json", "resultType": "core", "pageSize": 100}))
    candidates = []
    for item in values["resultList"]["result"]:
        remote = epmc_record(item)
        if norm_doi(item.get("doi")) == record.doi and not _metadata_conflicts(record, remote):
            candidates.extend(remote.pdf_urls)
    if not candidates:
        return {"status": "fail", "reason": "no_explicit_oa_pdf_candidate"}
    candidates = list(dict.fromkeys(candidates))
    attempts = []
    for index, candidate in enumerate(candidates[:max_candidates], 1):
        try:
            response = client.request(candidate, accept="application/pdf")
            if not response.body.startswith(b"%PDF-") or len(response.body) < 1000:
                attempts.append({"candidate": index, "status": "rejected", "reason": "response_not_pdf"})
                continue
            if client.redact_body(response.body) != response.body:
                attempts.append({"candidate": index, "status": "rejected",
                                 "reason": "configured_secret_echo_only_redacted_snapshot_saved"})
                continue
            path = client.output / f"oa_candidate_{index:02d}.pdf"
            path.write_bytes(response.body)
            if not pdf_matches_record(record, extract_text_from_pdf(path, max_pages=1)):
                attempts.append({"candidate": index, "status": "rejected", "reason": "pdf_identity_not_confirmed", "file": path.name})
                continue
            attempts.append({"candidate": index, "status": "pass", "file": path.name})
            return {"status": "pass", "file": path.name, "sha256": hashlib.sha256(response.body).hexdigest(),
                    "identity": "opening_page_title_and_identifier_matched", "attempts": attempts}
        except (NetworkError, ValueError, OSError) as exc:
            attempt = {"candidate": index, "status": "rejected", "error_type": type(exc).__name__}
            if isinstance(exc, NetworkError) and exc.status is not None:
                attempt["http_status"] = exc.status
            attempts.append(attempt)
    return {"status": "truncated" if len(candidates) > max_candidates else "fail",
            "reason": "no_identity_confirmed_pdf_in_budget", "candidate_count": len(candidates), "attempts": attempts}


def run_probe(args: argparse.Namespace, *, client: Any | None = None,
              sleeper: Callable[[float], None] = time.sleep) -> dict:
    output = fresh_output(Path(args.out_dir))
    report: dict = {"schema_version": 1, "started_at_utc": utc_now(), "mode": "online" if args.online else "plan",
                    "scope": "Bounded API contracts and production adapters; not recall, scientific approval, or continuous availability.",
                    "stages": [], "all_services_validated": False, "snapshots": []}
    def checkpoint() -> None:
        _atomic_write_text(output / "probe_report.json", json.dumps(report, ensure_ascii=False, indent=2))
    selected = ["europepmc", "crossref", "pubmed"] + (["openalex"] if args.openalex else [])
    if not args.online:
        report["status"] = "planned"
        report["plan"] = {"services": selected, "page_size": args.page_size, "max_pages": args.max_pages,
                          "max_bytes_per_response": args.max_bytes, "pdf_requested": args.download_pdf,
                          "max_pdf_candidates": args.max_pdf_candidates,
                          "online_requires": ["--online", "--query", "--doi", "--title", "--email"],
                          "network_requests": 0}
    else:
        key = os.environ.get("OPENALEX_API_KEY", "") if args.openalex else ""
        transport = SnapshotClient(client or HttpClient(email=args.email, retries=args.retries), output,
                                   page_size=args.page_size, max_bytes=args.max_bytes, timeout=args.timeout,
                                   secrets=(key, args.email))
        report["status"] = "in_progress_incomplete"
        report["snapshots"] = transport.snapshots
        checkpoint()
        def stage(name: str, action: Callable[[], dict]) -> dict:
            try:
                result = action()
            except (NetworkError, ValueError, TypeError, KeyError, AttributeError, IndexError, ET.ParseError, OSError) as exc:
                result = {"status": "fail", "error_type": type(exc).__name__}
                if isinstance(exc, NetworkError) and exc.status is not None:
                    result["http_status"] = exc.status
            result.update(stage=name, completed_at_utc=utc_now())
            report["stages"].append(result)
            checkpoint()
            return result
        def europe() -> dict:
            events = []
            records = fetch_europepmc(args.page_size * args.max_pages, client=transport,
                                     topic=Query(args.query), events=events, sleeper=sleeper)
            return {"status": "truncated" if any(e.status == "truncated" for e in events) else "pass",
                    "available": transport.epmc_total, "retrieved": len(records),
                    "adapter": "fetch_europepmc/epmc_record"}
        stage("europepmc_search", europe)
        stage("crossref_search", lambda: crossref_search(transport, args.query, args.page_size, args.max_pages))
        stage("pubmed_search", lambda: pubmed_search(transport, args.query, args.page_size, args.max_pages, args.email, sleeper))
        if args.openalex:
            if key:
                stage("openalex_search", lambda: openalex_search(transport, args.query, key, args.page_size, args.max_pages))
                stage("openalex_metadata_identity", lambda: openalex_identity(transport, norm_doi(args.doi), args.title, key))
            else:
                stage("openalex_search", lambda: {"status": "fail", "reason": "OPENALEX_API_KEY_missing"})
                stage("openalex_metadata_identity", lambda: {"status": "fail", "reason": "OPENALEX_API_KEY_missing"})
        else:
            stage("openalex_search", lambda: {"status": "skipped", "reason": "not_requested"})
            stage("openalex_metadata_identity", lambda: {"status": "skipped", "reason": "not_requested"})
        holder: list[Record] = []
        def verify() -> dict:
            result, record = identity(transport, norm_doi(args.doi), args.title)
            if result["status"] == "pass":
                holder.append(record)
            return result
        stage("metadata_identity_and_swapped_title", verify)
        # PubMed exact-ID metadata is independently checked through its own XML adapter.
        def pubmed_identity() -> dict:
            transport.pubmed_total, transport.pubmed_offset = None, 0
            transport.pubmed_size = args.page_size
            records = fetch_pubmed(args.page_size, client=transport, email=args.email,
                                   topic=Query(f'"{norm_doi(args.doi)}"[DOI]'), sleeper=sleeper)
            if transport.pubmed_total != 1 or len(records) != 1 or records[0].doi != norm_doi(args.doi) or _metadata_conflicts(Record(args.title, doi=norm_doi(args.doi)), records[0]):
                raise ContractError("PubMed exact DOI identity was not corroborated")
            return {"status": "pass", "adapter": "fetch_pubmed"}
        stage("pubmed_metadata_identity", pubmed_identity)
        if args.download_pdf:
            stage("oa_pdf", lambda: pdf_stage(transport, holder[0], args.max_pdf_candidates) if holder else {"status": "fail", "reason": "metadata_identity_failed"})
        else:
            stage("oa_pdf", lambda: {"status": "skipped", "reason": "not_requested"})
        report["snapshots"] = transport.snapshots
        required = [s for s in report["stages"] if s["status"] != "skipped"]
        report["status"] = "pass" if required and all(s["status"] == "pass" for s in required) else "incomplete_or_failed"
        report["all_services_validated"] = all(s["status"] == "pass" for s in report["stages"])
    report["completed_at_utc"] = utc_now()
    checkpoint()
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", required=True, help="Fresh empty directory; all snapshots/report stay here")
    parser.add_argument("--online", action="store_true", help="Explicitly permit public API requests")
    parser.add_argument("--query", default="", help="Public non-secret search terms sent to selected services")
    parser.add_argument("--doi", default="", help="Known public DOI for exact identity check")
    parser.add_argument("--title", default="", help="Independently known exact title for that DOI")
    parser.add_argument("--email", default="", help="Contact required for online PubMed requests")
    parser.add_argument("--openalex", action="store_true", help="Include OpenAlex; read OPENALEX_API_KEY from environment")
    parser.add_argument("--download-pdf", action="store_true", help="Request explicitly OA-marked PDF and verify first-page identity")
    parser.add_argument("--max-pdf-candidates", type=int, default=3, help="Bound OA PDF fallback attempts, 1..10")
    parser.add_argument("--page-size", type=int, default=2)
    parser.add_argument("--max-pages", type=int, default=3)
    parser.add_argument("--max-bytes", type=int, default=5 * 1024 * 1024)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--retries", type=int, default=2)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not 1 <= args.page_size <= 100 or not 1 <= args.max_pages <= 100:
        parser.error("page-size must be 1..100 and max-pages 1..100")
    if not 1 <= args.max_pdf_candidates <= 10:
        parser.error("max-pdf-candidates must be 1..10")
    if not 1024 <= args.max_bytes <= 50 * 1024 * 1024 or not 1 <= args.timeout <= 120 or not 0 <= args.retries <= 5:
        parser.error("Invalid byte/timeout/retry budget")
    if args.online and (not args.query.strip() or not norm_doi(args.doi) or not args.title.strip() or not args.email.strip()):
        parser.error("Online mode requires --query, valid --doi, --title and --email")
    try:
        report = run_probe(args)
    except (ValueError, OSError):
        print("Unable to create a fresh probe output directory")
        return 2
    print("Probe: " + report["status"] + "; report: probe_report.json")
    return 0 if report["status"] in {"planned", "pass"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
