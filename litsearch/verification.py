"""Identifier + metadata verification. A missing index hit never proves fabrication."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from .core import key_for, norm_doi, norm_title, title_key
from .models import AuditEvent, Record
from .net import HttpClient, NetworkError
from .outputs import _atomic_write_text

EPMC_SEARCH = "https://www.ebi.ac.uk/europepmc/webservices/rest/search?"


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class MetadataStore:
    """Content-hashed response snapshots. Local cache is trusted storage, not a signature."""
    def __init__(self, directory: Path, client: HttpClient, *, offline: bool = False,
                 refresh: bool = False, max_age_days: int = 7) -> None:
        self.directory, self.client = directory, client
        self.offline, self.refresh, self.max_age_days = offline, refresh, max_age_days
        self.memory: dict[str, dict] = {}

    def get(self, url: str) -> tuple[dict, dict]:
        key = hashlib.sha256(url.encode()).hexdigest()
        path = self.directory / f"{key}.json"
        snapshot = self.memory.get(key)
        if snapshot is None and path.exists() and not self.refresh:
            try:
                if path.stat().st_size > 30 * 1024 * 1024:
                    raise ValueError("snapshot too large")
                snapshot = json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                snapshot = None
        if snapshot is not None:
            try:
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(snapshot["fetched_at_utc"])).total_seconds()
                valid = (snapshot["url"] == url and snapshot["sha256"] == _digest(snapshot["data"])
                         and isinstance(snapshot["data"], dict) and 0 <= age <= self.max_age_days * 86400)
            except (KeyError, TypeError, ValueError):
                valid = False
            if not valid:
                snapshot = None
        if snapshot is None:
            if self.offline:
                raise NetworkError("没有未过期、校验完整的本地元数据快照；身份保持未核实")
            data = self.client.get_json(url)
            snapshot = {"url": url, "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
                        "sha256": _digest(data), "data": data}
            self.directory.mkdir(parents=True, exist_ok=True)
            _atomic_write_text(path, json.dumps(snapshot, ensure_ascii=False, indent=2))
        self.memory[key] = snapshot
        provenance = {key: snapshot[key] for key in ("url", "fetched_at_utc", "sha256")}
        return snapshot["data"], provenance


def epmc_record(item: dict) -> Record:
    journal_info = item.get("journalInfo") or {}
    journal = journal_info.get("journal") or {}
    author_items = (item.get("authorList") or {}).get("author") or []
    authors = [str(author.get("fullName") or " ".join(filter(None, [author.get("firstName"), author.get("lastName")])))
               for author in author_items if isinstance(author, dict)]
    rec = Record(title=norm_title(item.get("title") or ""), doi=norm_doi(item.get("doi")),
                 year=str(item.get("pubYear") or ""), abstract=norm_title(item.get("abstractText") or ""),
                 authors=[a for a in authors if a], journal=str(journal.get("title") or ""),
                 volume=str(journal_info.get("volume") or ""), issue=str(journal_info.get("issue") or ""),
                 pages=str(item.get("pageInfo") or ""),
                 pmid=str(item.get("id") or "") if item.get("source") == "MED" else "",
                 pmcid=str(item.get("pmcid") or ""),
                 publication_types=(item.get("pubTypeList") or {}).get("pubType") or [],
                 source_ids=[f"EuropePMC:{item.get('source', '')}:{item.get('id', '')}"])
    rec.url = "https://doi.org/" + quote(rec.doi, safe="/") if rec.doi else (
        f"https://europepmc.org/article/{item.get('source', '')}/{item.get('id', '')}")
    for link in (item.get("fullTextUrlList") or {}).get("fullTextUrl") or []:
        if link.get("documentStyle", "").lower() == "pdf" and link.get("availabilityCode") == "OA":
            if isinstance(link.get("url"), str):
                rec.pdf_urls.append(link["url"])
    return rec


def _identity_title(title: str) -> str:
    key = title_key(title)
    # XML subscript spacing: B5, B 5 and B(5) are the same typography.
    # Do not remove arbitrary words, chapter numbers, or allow fuzzy titles.
    return re.sub(r"\b([a-z])\s+(\d+)\b", r"\1\2", key)


def _publication_years(payload: dict) -> set[str]:
    years = set()
    for key in ("published", "published-print", "published-online", "issued"):
        date = payload.get(key) or {}
        if isinstance(date, dict):
            parts = date.get("date-parts") or [[]]
            if parts and parts[0]:
                years.add(str(parts[0][0]))
    for key in ("pubYear", "firstPublicationDate", "electronicPublicationDate"):
        value = str(payload.get(key) or "")
        if re.match(r"^\d{4}(?:-|$)", value):
            years.add(value[:4])
    journal = payload.get("journalInfo") or {}
    for key in ("yearOfPublication", "printPublicationDate"):
        value = str(journal.get(key) or "")
        if re.match(r"^\d{4}(?:-|$)", value):
            years.add(value[:4])
    return years


def _metadata_conflicts(rec: Record, remote: Record, known_years: set[str] | None = None) -> list[str]:
    reasons = []
    if not _identity_title(remote.title) or _identity_title(rec.title) != _identity_title(remote.title):
        reasons.append("标识符对应的标题不一致（含小差异也须人工校正，不自动猜测）")
    for field in ("doi", "pmid", "pmcid"):
        if getattr(rec, field) and getattr(remote, field) and getattr(rec, field) != getattr(remote, field):
            reasons.append(f"{field} 与权威记录不一致")
    if rec.year and remote.year and rec.year not in (known_years or {remote.year}):
        # Online-first / print years can differ. Keep this visible and conservative.
        reasons.append("年份不一致，可能是在线/印刷年份差异；须核实版本")
    if rec.authors and remote.authors:
        def tokens(names: list[str]) -> set[str]:
            return {token for name in names for token in title_key(name).split() if len(token) > 2}
        if tokens(rec.authors).isdisjoint(tokens(remote.authors)):
            reasons.append("作者未能交叉匹配")
    return reasons


def verify_record(rec: Record, store: MetadataStore) -> list[AuditEvent]:
    rec.identity_status, rec.identity_checks, rec.verified_abstract = "unverified", [], ""
    doi = norm_doi(rec.doi)
    if (rec.doi and not doi) or (rec.pmid and not re.fullmatch(r"\d+", rec.pmid)):
        rec.identity_status = "invalid_identifier"
        return [AuditEvent("identity", "rejected", "标识符格式无效", record_key=key_for(rec))]
    if not doi and not rec.pmid:
        return [AuditEvent("identity", "skipped", "缺少 DOI/PMID；不能用相似标题替代身份核实", record_key=key_for(rec))]
    rec.doi = doi
    checks: list[dict] = []
    matched, conflicts, retracted, update_warning = False, False, False, False
    matched_doi, matched_pmid = not bool(doi), not bool(rec.pmid)
    updates_checked = not bool(doi)

    def check_remote(remote: Record, provenance: dict, source: str, payload: dict) -> None:
        nonlocal matched, conflicts, retracted, update_warning, matched_doi, matched_pmid
        remote = Record.from_dict(remote.to_dict())
        publication_years = _publication_years(payload) | ({remote.year} if remote.year else set())
        reasons = _metadata_conflicts(rec, remote, publication_years)
        # Crossref supplies the full container title. Do not compare Europe PMC
        # abbreviations, or silently overwrite an incorrect supplied journal.
        # Unresolved abbreviations are also held for explicit correction.
        if (source == "crossref" and rec.journal and remote.journal
                and title_key(rec.journal) != title_key(remote.journal)):
            reasons.append("期刊名称与Crossref不一致；缩写或曾用名也须明确核对")
        conflict = bool(reasons)
        conflicts |= conflict
        matched |= not conflict
        types = " ".join(remote.publication_types).casefold()
        flagged = "retracted publication" in types or "retraction of publication" in types
        corrections = (payload.get("commentCorrectionList") or {}).get("commentCorrection") or []
        correction_types = " ".join(str(c.get("type", "")) for c in corrections).casefold()
        flagged |= "retract" in correction_types
        update_warning |= any(word in correction_types for word in ("erratum", "correction", "concern"))
        retracted |= flagged
        if not conflict:
            matched_doi |= bool(doi and remote.doi == doi)
            matched_pmid |= bool(rec.pmid and remote.pmid == rec.pmid)
            if remote.abstract:
                rec.verified_abstract = remote.abstract
            rec.publication_types = sorted(set(rec.publication_types + remote.publication_types))
        checks.append({**provenance, "source": source, "status": "conflict" if conflict else "matched",
                       "reasons": reasons, "remote_title": remote.title, "remote_year": remote.year,
                       "remote_doi": remote.doi, "remote_authors": remote.authors,
                       "publication_years": sorted(publication_years),
                       "typography_or_date_note": "已按公开发表日期/字母数字排版核对" if (
                           rec.year != remote.year or title_key(rec.title) != title_key(remote.title)) and not conflict else "",
                       "remote_journal": remote.journal, "remote_volume": remote.volume,
                       "remote_issue": remote.issue, "remote_pages": remote.pages, "retracted_flag": flagged})

    if doi:
        url = "https://api.crossref.org/works/" + quote(doi, safe="")
        try:
            data, provenance = store.get(url)
            msg = data.get("message")
            if not isinstance(msg, dict) or not isinstance(msg.get("title"), list) or not msg.get("DOI"):
                raise NetworkError("Crossref 返回结构无效")
            date_parts = (msg.get("published") or msg.get("issued") or {}).get("date-parts") or [[]]
            remote = Record(title=norm_title(" ".join(msg["title"])), doi=norm_doi(msg["DOI"]),
                            year=str(date_parts[0][0]) if date_parts[0] else "",
                            authors=[" ".join(str(a.get(k) or "") for k in ("given", "family")).strip()
                                     for a in msg.get("author") or []],
                            publication_types=[str(msg.get("type") or "")],
                            journal=" ".join(msg.get("container-title") or []),
                            volume=str(msg.get("volume") or ""), issue=str(msg.get("issue") or ""),
                            pages=str(msg.get("page") or ""),
                            abstract=norm_title(msg.get("abstract") or ""))
            if remote.doi != doi:
                raise NetworkError("Crossref 返回了非请求 DOI")
            check_remote(remote, provenance, "crossref", msg)
            if msg.get("update-to"):
                update_warning = True  # The record itself is an update notice.
        except (NetworkError, ValueError, TypeError, KeyError, AttributeError, IndexError) as exc:
            checks.append({"source": "crossref", "status": "not_indexed" if getattr(exc, "status", None) == 404 else "unavailable",
                           "reason": str(exc)})
        # Original articles do not necessarily list update notices in /works/{doi}.
        # Query reverse updates explicitly; any correction/concern blocks automatic release.
        try:
            data, provenance = store.get("https://api.crossref.org/works?" + urlencode({"filter": f"updates:{doi}", "rows": 100}))
            message = data.get("message")
            if not isinstance(message, dict) or not isinstance(message.get("items"), list) or "total-results" not in message:
                raise NetworkError("Crossref 更新查询返回结构无效")
            updates_checked = True
            if int(message["total-results"]) > 0:
                update_warning = True
                for notice in message["items"]:
                    for update in notice.get("update-to") or []:
                        if norm_doi(update.get("DOI")) == doi and "retract" in str(update.get("type", "")).casefold():
                            retracted = True
            checks.append({**provenance, "source": "crossref_updates", "status": "checked",
                           "notice_count": int(message["total-results"])})
        except (NetworkError, ValueError, TypeError, KeyError, AttributeError, IndexError) as exc:
            checks.append({"source": "crossref_updates", "status": "unavailable", "reason": str(exc)})

    # When both identifiers are present, query BOTH to catch mismatched DOI/PMID pairs.
    queries = ([f'DOI:"{doi.replace(chr(34), chr(92) + chr(34))}"'] if doi else [])
    if rec.pmid:
        queries.append(f"EXT_ID:{rec.pmid} AND SRC:MED")
    for query in queries:
        if query.startswith("EXT_ID:") and matched_pmid:
            continue  # DOI response already corroborated this exact PMID.
        try:
            data, provenance = store.get(EPMC_SEARCH + urlencode({"query": query, "format": "json", "resultType": "core", "pageSize": 100}))
            items = (data.get("resultList") or {}).get("result")
            if not isinstance(items, list) or "hitCount" not in data:
                raise NetworkError("Europe PMC 返回结构无效")
            exact = [item for item in items if (norm_doi(item.get("doi")) == doi if query.startswith("DOI:")
                                              else item.get("source") == "MED" and str(item.get("id")) == rec.pmid)]
            if not exact:
                checks.append({**provenance, "source": "europepmc", "status": "not_indexed"})
            for item in exact:
                check_remote(epmc_record(item), provenance, "europepmc", item)
        except (NetworkError, ValueError, TypeError, KeyError, AttributeError, IndexError) as exc:
            checks.append({"source": "europepmc", "status": "unavailable", "reason": str(exc)})
    if retracted:
        rec.identity_status = "retracted"
    elif conflicts:
        rec.identity_status = "metadata_conflict"
    elif update_warning:
        rec.identity_status = "publication_update"
    elif matched and matched_doi and matched_pmid:
        rec.identity_status = "verified" if updates_checked else "verified_updates_unknown"
    elif any(c["status"] == "unavailable" for c in checks):
        rec.identity_status = "unverified"
    else:
        rec.identity_status = "not_indexed"
    rec.identity_checks = checks
    return [AuditEvent("identity", "success" if rec.identity_status == "verified" else "rejected",
                       rec.identity_status, record_key=key_for(rec))]
