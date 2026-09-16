from __future__ import annotations

import csv
import html
import hashlib
import os
import re
import urllib.parse
import uuid
from dataclasses import dataclass
from pathlib import Path

from .core import (
    extract_evidence,
    extract_results_like,
    key_for,
    safe_pdf_name,
    title_key,
    normalize_pdf_text,
)
from .models import AuditEvent, Record
from .net import HttpClient, NetworkError
from .sources import add_pdf_url


@dataclass
class PdfIndex:
    by_name: dict[str, Path]

    @classmethod
    def build(cls, root: Path) -> "PdfIndex":
        by_name: dict[str, Path] = {}
        if not root.exists():
            return cls(by_name)
        try:
            candidates = root.rglob("*.pdf")
            for path in candidates:
                try:
                    if path.is_file() and path.stat().st_size > 1_000:
                        by_name.setdefault(path.name.lower(), path)
                except OSError:
                    continue
        except OSError:
            pass
        return cls(by_name)

    def find(self, record: Record) -> Path | None:
        current = self.by_name.get(safe_pdf_name(record).lower())
        # Migration lookup only; a legacy filename never proves PDF identity.
        legacy = re.sub(r"[^A-Za-z0-9._-]+", "_", record.doi.replace("/", "_").replace(":", "_")) + ".pdf"
        return current or (self.by_name.get(legacy.lower()) if record.doi else None)

    def add(self, path: Path) -> None:
        self.by_name[path.name.lower()] = path


def find_existing_pdf(record: Record, pdf_dir: Path, index: PdfIndex | None = None) -> Path | None:
    return (index or PdfIndex.build(pdf_dir)).find(record)


def link_existing_pdfs(records: list[Record], pdf_dir: Path, index: PdfIndex | None = None) -> PdfIndex:
    pdf_index = index or PdfIndex.build(pdf_dir)
    for record in records:
        if record.pdf_file:
            try:
                path = Path(record.pdf_file).resolve()
                if path.is_relative_to(pdf_dir.resolve()) and path.is_file() and path.stat().st_size > 1_000:
                    continue
            except OSError:
                pass
            record.processing_warnings.append("旧JSON中的PDF路径已失效或不在指定PDF库内，已清除。")
            record.pdf_file = ""
        candidate = pdf_index.find(record)
        if candidate and candidate.resolve().is_relative_to(pdf_dir.resolve()):
            record.pdf_file = str(candidate.resolve())
    return pdf_index


def add_candidate_pdf_urls(
    record: Record,
    *,
    client: HttpClient,
    email: str,
    timeout: int = 12,
    include_landing: bool = False,
) -> list[AuditEvent]:
    events: list[AuditEvent] = []
    if not record.doi:
        return events
    doi = record.doi
    record_key = key_for(record)

    discovery_calls: list[tuple[str, str]] = [
        (
            "pmc",
            "https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/?"
            + urllib.parse.urlencode({"ids": doi, "format": "json"}),
        ),
        (
            "crossref",
            f"https://api.crossref.org/works/{urllib.parse.quote(doi)}?"
            + urllib.parse.urlencode({"mailto": email}),
        ),
    ]
    if email:
        discovery_calls.insert(
            1,
            (
                "unpaywall",
                f"https://api.unpaywall.org/v2/{urllib.parse.quote(doi)}?"
                + urllib.parse.urlencode({"email": email}),
            ),
        )
    else:
        events.append(
            AuditEvent(
                stage="pdf_discovery",
                status="skipped",
                source="unpaywall",
                record_key=record_key,
                message="缺少联系邮箱，按 Unpaywall API 规则跳过。",
            )
        )

    for source, url in discovery_calls:
        try:
            data = client.get_json(url, timeout=timeout)
            if source == "pmc":
                for item in data.get("records") or []:
                    pmcid = item.get("pmcid")
                    if pmcid:
                        record.pmcid = record.pmcid or str(pmcid)
                        add_pdf_url(record, f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/pdf/")
            elif source == "unpaywall":
                best = data.get("best_oa_location") or {}
                add_pdf_url(record, best.get("url_for_pdf"))
                for location in data.get("oa_locations") or []:
                    add_pdf_url(record, location.get("url_for_pdf"))
            else:
                message = data.get("message") or {}
                for link in message.get("link") or []:
                    target = link.get("URL")
                    content_type = str(link.get("content-type") or "").lower()
                    if target and ("pdf" in content_type or str(target).lower().endswith(".pdf")):
                        add_pdf_url(record, target)
        except Exception as exc:
            events.append(
                AuditEvent(
                    stage="pdf_discovery",
                    status="failed",
                    source=source,
                    record_key=record_key,
                    url=url,
                    message=f"{type(exc).__name__}: {exc}",
                )
            )

    if include_landing:
        landing_url = "https://doi.org/" + urllib.parse.quote(doi, safe="/")
        try:
            page, final_url = client.get_text(landing_url, timeout=timeout)
            patterns = (
                r'<meta[^>]+name=["\']citation_pdf_url["\'][^>]+content=["\']([^"\']+)["\']',
                r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']citation_pdf_url["\']',
                r'href=["\']([^"\']+\.pdf(?:\?[^"\']*)?)["\']',
            )
            for pattern in patterns:
                for match in re.findall(pattern, page, flags=re.I):
                    add_pdf_url(record, urllib.parse.urljoin(final_url, html.unescape(match)))
        except Exception as exc:
            events.append(
                AuditEvent(
                    stage="pdf_discovery",
                    status="failed",
                    source="doi_landing",
                    record_key=record_key,
                    url=landing_url,
                    message=f"{type(exc).__name__}: {exc}",
                )
            )
    return events


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    temporary = path.with_name(path.name + f".{uuid.uuid4().hex}.part")
    temporary.write_bytes(data)
    os.replace(temporary, path)


def download_pdfs(
    records: list[Record],
    pdf_dir: Path,
    *,
    client: HttpClient,
    email: str,
    max_pdfs: int | None = None,
    timeout: int = 20,
    max_urls_per_record: int = 4,
    max_pdf_bytes: int = 50 * 1024 * 1024,
    include_landing: bool = False,
    log_path: Path | None = None,
) -> list[AuditEvent]:
    pdf_dir.mkdir(parents=True, exist_ok=True)
    index = link_existing_pdfs(records, pdf_dir)
    downloaded = 0
    events: list[AuditEvent] = []
    for record in records:
        if max_pdfs is not None and downloaded >= max_pdfs:
            break
        if record.pdf_file:
            continue
        events.extend(
            add_candidate_pdf_urls(
                record,
                client=client,
                email=email,
                timeout=min(timeout, 12),
                include_landing=include_landing,
            )
        )
        output = pdf_dir / safe_pdf_name(record)
        for url in record.pdf_urls[:max_urls_per_record]:
            try:
                response = client.get_bytes(url, timeout=timeout, max_bytes=max_pdf_bytes)
                if len(response.body) < 1_000 or not response.body.startswith(b"%PDF"):
                    events.append(
                        AuditEvent(
                            stage="pdf_download",
                            status="rejected",
                            source="candidate_url",
                            record_key=key_for(record),
                            url=url,
                            message=f"响应不是有效PDF开头或小于1KB；Content-Type={response.content_type}",
                        )
                    )
                    continue
                _atomic_write_bytes(output, response.body)
                record.pdf_file = str(output.resolve())
                index.add(output)
                downloaded += 1
                events.append(
                    AuditEvent(
                        stage="pdf_download",
                        status="success",
                        source="candidate_url",
                        record_key=key_for(record),
                        url=response.final_url,
                        message=f"下载 {len(response.body)} 字节",
                    )
                )
                break
            except (NetworkError, OSError) as exc:
                events.append(
                    AuditEvent(
                        stage="pdf_download",
                        status="failed",
                        source="candidate_url",
                        record_key=key_for(record),
                        url=url,
                        message=f"{type(exc).__name__}: {exc}",
                    )
                )
    if log_path:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("w", encoding="utf-8-sig", newline="") as handle:
            fields = ["stage", "status", "source", "record_key", "url", "message"]
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            from .outputs import excel_safe
            writer.writerows({k: excel_safe(v) for k, v in event.to_dict().items()} for event in events)
    return events


def extract_text_from_pdf(path: Path, max_pages: int = 80) -> str:
    if path.stat().st_size > 50 * 1024 * 1024:
        return ""
    try:
        from pypdf import PdfReader
    except ImportError:
        return ""
    try:
        reader = PdfReader(str(path))
    except Exception:
        return ""
    chunks: list[str] = []
    for page_number, page in enumerate(reader.pages[:max_pages], 1):
        try:
            content = page.extract_text() or ""
            if content.strip():
                chunks.append(f"\n\n[[PAGE {page_number}]]\n{content}")
        except Exception:
            continue
    return "".join(chunks)


def _pages_for_excerpt(full_text: str, excerpt: str) -> list[int]:
    if not excerpt:
        return []
    normalized_full_text = normalize_pdf_text(full_text)
    start = normalized_full_text.find(excerpt)
    if start < 0:
        return []
    end = start + len(excerpt)
    markers = [
        (match.start(), int(match.group(1)))
        for match in re.finditer(r"\[\[PAGE (\d+)]]", normalized_full_text)
    ]
    current_page = 0
    pages: list[int] = []
    for position, page in markers:
        if position <= start:
            current_page = page
        elif position <= end:
            if current_page and current_page not in pages:
                pages.append(current_page)
            current_page = page
        else:
            break
    if current_page and current_page not in pages:
        pages.append(current_page)
    return pages


def parse_corresponding_author(record: Record, full_text: str) -> None:
    if not full_text:
        return
    text = re.sub(r"\s+", " ", full_text[:16_000])
    patterns = (
        r"(?i)(?:correspondence|corresponding author)[^.;]{0,220}(?:to|:)\s*([^.;]{3,140})",
        r"(?i)\*\s*corresponding author[^.;]{0,180}",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        snippet = match.group(0).strip()
        emails = re.findall(r"[\w.\-+]+@[\w.\-]+\.\w+", snippet)
        names = []
        for author in record.authors:
            last_name = author.split()[-1].lower() if author.split() else ""
            if last_name and contains_ascii_word(snippet.lower(), last_name):
                names.append(author)
        if names:
            record.corresponding_author = "英文全名：" + "; ".join(dict.fromkeys(names))
        elif emails:
            record.corresponding_author = "未可靠解析姓名；邮箱：" + ", ".join(emails)
        else:
            record.corresponding_author = snippet[:180]
        return


def contains_ascii_word(text: str, word: str) -> bool:
    return bool(re.search(rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])", text, re.I))


def pdf_matches_record(record: Record, text: str) -> bool:
    """Both identifier and full title must appear in the opening page, not references."""
    opening = re.split(r"\[\[PAGE (?:[2-9]|\d{2,})]]", text, maxsplit=1)[0][:12_000]
    opening = re.split(r"(?im)^\s*(?:\d+[. ]+)?(?:references|bibliography)\s*$", opening, maxsplit=1)[0]
    if re.search(r"(?i)supplementary (?:material|information)|supporting information|additional file", opening[:1500]):
        return False
    normalized = title_key(opening).replace(" ", "")
    title = title_key(record.title).replace(" ", "")
    if len(title) < 25 or title not in normalized or normalized.find(title) > 2000:
        return False
    if record.doi:
        doi_text = re.sub(r"[‐‑‒–—−]", "-", opening)
        return bool(re.search(re.escape(record.doi) + r"(?![a-z0-9/])", doi_text, re.I))
    return bool(record.pmid and re.search(r"(?i)PMID\s*:?\s*" + re.escape(record.pmid) + r"\b", opening))


def enrich_from_pdfs(records: list[Record], max_pages: int = 80) -> list[AuditEvent]:
    events: list[AuditEvent] = []
    for record in records:
        record.results_excerpt, record.results_pages = "", []
        record.pdf_identity_status, record.pdf_sha256 = "unverified", ""
        if record.pdf_file:
            path = Path(record.pdf_file)
            text = extract_text_from_pdf(path, max_pages=max_pages)
            if text and pdf_matches_record(record, text):
                record.pdf_identity_status = "matched"
                record.pdf_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
                record.results_excerpt = extract_results_like(text)
                record.results_pages = _pages_for_excerpt(text, record.results_excerpt)
                parse_corresponding_author(record, text)
                if not record.results_excerpt:
                    record.processing_warnings.append("PDF可解析，但没有可靠定位 Results 标题。")
            elif text:
                record.pdf_identity_status = "mismatch_or_unconfirmed"
                record.processing_warnings.append("PDF首页未同时匹配题名和DOI/PMID；不提取为该文证据，需人工核对。")
                events.append(AuditEvent("pdf_identity", "rejected", "PDF身份未确认", record_key=key_for(record)))
            else:
                record.processing_warnings.append("PDF存在，但文本提取失败；可能需要OCR或PDF已损坏。")
                events.append(
                    AuditEvent(
                        stage="pdf_extract",
                        status="failed",
                        source="pypdf",
                        record_key=key_for(record),
                        url=str(path),
                        message="未提取到文本",
                    )
                )
        if not record.corresponding_author and record.authors:
            record.corresponding_author = (
                "未由全文确认通讯作者；作者候选：" + "; ".join(record.authors[:3])
            )
        extract_evidence(record)
    return events
