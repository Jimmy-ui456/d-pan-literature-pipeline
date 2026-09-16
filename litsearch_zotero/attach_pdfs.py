#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

from litsearch.core import norm_doi


def doi_filename(doi: str) -> str:
    normalized = norm_doi(doi)
    return re.sub(r"[^a-z0-9._-]+", "_", normalized)


def staging_match(doi: str, root: Path) -> Path | None:
    needle = doi_filename(doi)
    if not needle or not root.is_dir():
        return None
    for path in root.glob("*.pdf"):
        candidate = re.sub(r"[^a-z0-9._-]+", "_", path.stem.lower())
        if candidate != needle:
            continue
        try:
            with path.open("rb") as handle:
                valid = path.stat().st_size > 1_000 and handle.read(4) == b"%PDF"
        except OSError:
            valid = False
        if valid:
            return path.resolve()
    return None


def validate_local_api_url(api_url: str) -> str:
    parsed = urllib.parse.urlsplit(api_url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Zotero API 地址必须是本机 HTTP 地址，避免把本地文件路径发送到外部服务")
    if parsed.username or parsed.password:
        raise ValueError("Zotero API 地址不能包含用户名或密码")
    return api_url


def create_attachment(api_url: str, parent_key: str, path: Path) -> dict:
    validate_local_api_url(api_url)
    if not re.fullmatch(r"[A-Z0-9]{8}", parent_key, re.I):
        raise ValueError(f"非法 Zotero item key: {parent_key!r}")
    if not path.is_file():
        raise ValueError(f"PDF 文件不存在：{path}")
    payload = [
        {
            "itemType": "attachment",
            "linkMode": "linked_file",
            "title": path.name,
            "path": str(path),
            "contentType": "application/pdf",
            "parentItem": parent_key,
            "tags": [{"tag": "Codex补齐PDF"}],
        }
    ]
    request = urllib.request.Request(
        api_url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Zotero-API-Version": "3"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audit", default="outputs_zotero_audit/zotero_pdf_audit.json")
    parser.add_argument("--staging", default="zotero_pdf_staging")
    parser.add_argument("--report", default="outputs_zotero_audit/attachment_report.json")
    parser.add_argument("--api-url", default="http://127.0.0.1:23119/api/users/0/items")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="实际写入 Zotero；默认仅生成预演报告，防止误挂或重复挂附件。",
    )
    args = parser.parse_args()

    validate_local_api_url(args.api_url)
    rows = json.loads(Path(args.audit).read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise ValueError("审计JSON必须是记录数组")
    staging = Path(args.staging)
    available_by_doi: dict[str, Path] = {}
    for row in rows:
        doi = norm_doi(row.get("doi", ""))
        if doi and row.get("has_pdf") and row.get("zotero_pdf"):
            available_by_doi[doi] = Path(row["zotero_pdf"]).resolve()

    results = []
    for row in rows:
        if not row.get("core_relevant") or row.get("has_pdf"):
            continue
        doi = norm_doi(row.get("doi", ""))
        if not doi:
            results.append(
                {
                    "itemKey": row.get("itemKey", ""),
                    "doi": "",
                    "status": "skipped_missing_doi",
                }
            )
            continue
        source = available_by_doi.get(doi) or staging_match(doi, staging)
        if not source:
            results.append(
                {"itemKey": row.get("itemKey", ""), "doi": doi, "status": "no_pdf_source"}
            )
            continue
        if not args.apply:
            results.append(
                {
                    "itemKey": row.get("itemKey", ""),
                    "doi": doi,
                    "status": "would_attach",
                    "source": str(source),
                }
            )
            continue
        try:
            response = create_attachment(args.api_url, row["itemKey"], source)
            successful = response.get("successful", {})
            results.append(
                {
                    "itemKey": row["itemKey"],
                    "doi": doi,
                    "status": "attached" if successful else "api_response_without_success",
                    "source": str(source),
                    "response": response,
                }
            )
        except urllib.error.HTTPError as exc:
            results.append(
                {
                    "itemKey": row["itemKey"],
                    "doi": doi,
                    "status": f"http_error_{exc.code}",
                    "source": str(source),
                    "detail": exc.read().decode("utf-8", errors="replace"),
                }
            )
        except Exception as exc:
            results.append(
                {
                    "itemKey": row["itemKey"],
                    "doi": doi,
                    "status": "error",
                    "source": str(source),
                    "detail": repr(exc),
                }
            )

    report = Path(args.report)
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    counts: dict[str, int] = defaultdict(int)
    for result in results:
        counts[result["status"]] += 1
    print(json.dumps(dict(counts), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
