from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from .core import classify_and_score, extract_evidence, key_for, merge_records, passes_final_scope, record_fingerprint
from .evidence import apply_review_decisions, build_evidence_cards, write_review_bundle
from .models import AuditEvent, Record
from .net import HttpClient
from .outputs import _atomic_write_text, write_outputs, write_records_json, write_run_audit
from .pdfs import download_pdfs, enrich_from_pdfs, link_existing_pdfs
from .sources import fetch_europepmc, fetch_openalex, fetch_pubmed
from .topics import load_topic
from .verification import MetadataStore, verify_record

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MAX_INPUT_JSON_BYTES = 200 * 1024 * 1024


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="可换课题、可核身份、证据驱动的微生物代谢工程文献管线。")
    parser.add_argument("--topic", default="pantothenate", help="内置 pantothenate/lysine/glutathione 或课题JSON路径。")
    parser.add_argument("--out-dir", default="", help="新的空输出目录；默认自动生成独立运行目录。")
    parser.add_argument("--pdf-dir", default="pdf_library")
    parser.add_argument("--cache-dir", default=".litsearch-cache", help="权威元数据快照目录；本地可信存储。")
    parser.add_argument("--source", dest="sources", action="append", choices=("europepmc", "openalex", "pubmed"),
                        help="可重复；默认 europepmc（无需API key）。")
    parser.add_argument("--search-max", type=int, default=500, help="Europe PMC 检索预算，超出会报告截断。")
    parser.add_argument("--per-query", type=int, default=100, help="OpenAlex 单页上限1-200，非穷尽检索。")
    parser.add_argument("--pubmed-max", type=int, default=300)
    parser.add_argument("--score-threshold", type=int, default=0, help="仅排序过滤，不是相关性概率；默认不按分数删文。")
    parser.add_argument("--core-only", action="store_true")
    parser.add_argument("--max-pdfs", type=int, default=20, help="新增PDF预算；0表示不限。")
    parser.add_argument("--download-timeout", type=int, default=20)
    parser.add_argument("--max-urls-per-record", type=int, default=4)
    parser.add_argument("--max-pdf-mb", type=int, default=50)
    parser.add_argument("--max-pdf-pages", type=int, default=80)
    parser.add_argument("--download-pdfs", action="store_true", help="明确启用联网PDF获取；默认只关联本地库。")
    parser.add_argument("--skip-download", action="store_true", help="兼容旧命令，关闭下载。")
    parser.add_argument("--skip-pdf-extraction", action="store_true")
    parser.add_argument("--discover-landing-pdfs", action="store_true", help="启用额外的落地页候选发现；链接不代表OA许可或PDF身份。")
    parser.add_argument("--skip-landing-discovery", action="store_true")
    parser.add_argument("--allow-partial-sources", action="store_true")
    parser.add_argument("--from-json", default="", help="离线重新审查；默认只用未过期核验缓存，不偷偷联网。")
    parser.add_argument("--verify-online", action="store_true", help="对导入JSON重新联网核实身份。")
    parser.add_argument("--refresh-verification", action="store_true", help="重新请求核验元数据，跳过缓存。")
    parser.add_argument("--cache-max-age-days", type=int, default=7)
    parser.add_argument("--review-decisions", default="", help="人工填写的审阅JSON；须匹配课题、记录和证据。")
    parser.add_argument(
        "--supplement-manifest", default="",
        help="可选JSON，显式列出本地补充材料文件及记录角色；不自动发现或下载补充材料。",
    )
    parser.add_argument("--reuse-derived-fields", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--email", default=os.environ.get("LIT_SEARCH_EMAIL", ""))
    parser.add_argument("--openalex-api-key", default=os.environ.get("OPENALEX_API_KEY", ""))
    parser.add_argument("--ncbi-api-key", default=os.environ.get("NCBI_API_KEY", ""))
    parser.add_argument("--network-retries", type=int, default=2)
    parser.add_argument("--verification-workers", type=int, default=3, help="身份核验并发数1-4；共享请求节流。")
    return parser


def _validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    args.sources = list(dict.fromkeys(args.sources or ["europepmc"]))
    ranges = {"per_query": (1, 200), "pubmed_max": (1, 10_000), "search_max": (1, 100_000),
              "score_threshold": (0, 100), "max_pdfs": (0, 10_000), "download_timeout": (1, 300),
              "max_urls_per_record": (1, 20), "max_pdf_mb": (1, 50), "max_pdf_pages": (1, 2000),
              "network_retries": (0, 10), "cache_max_age_days": (1, 30), "verification_workers": (1, 4)}
    for name, (low, high) in ranges.items():
        if not low <= getattr(args, name) <= high:
            parser.error(f"--{name.replace('_', '-')} 必须在 {low} 到 {high} 之间")
    if not args.from_json:
        if any(source in args.sources for source in ("pubmed", "openalex")) and not EMAIL_RE.fullmatch(args.email):
            parser.error("PubMed/OpenAlex 检索需要有效 --email")
        if "openalex" in args.sources and not args.openalex_api_key:
            parser.error("OpenAlex 需要 --openalex-api-key；也可使用 --source europepmc")
    if args.download_pdfs and not args.skip_download and not EMAIL_RE.fullmatch(args.email):
        parser.error("PDF发现需要有效联系邮箱 --email")
    if args.from_json and not Path(args.from_json).is_file():
        parser.error("--from-json 文件不存在")
    if args.review_decisions and not Path(args.review_decisions).is_file():
        parser.error("--review-decisions 文件不存在")
    if args.supplement_manifest and not Path(args.supplement_manifest).is_file():
        parser.error("--supplement-manifest 文件不存在")
    if args.refresh_verification and args.from_json and not args.verify_online:
        parser.error("--refresh-verification 需要 --verify-online，或在线检索模式")


def _load_records(path: Path) -> list[Record]:
    if path.stat().st_size > MAX_INPUT_JSON_BYTES:
        raise ValueError("输入JSON超过200MB安全上限")
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(payload, dict):
        payload = payload.get("records")
    if not isinstance(payload, list):
        raise ValueError("输入JSON必须是记录数组，或含records数组的对象")
    records = []
    for index, item in enumerate(payload, 1):
        if not isinstance(item, dict):
            raise ValueError(f"第{index}条记录不是对象")
        try:
            record = Record.from_dict(item)
            record.clear_derived_fields()
            records.append(record)
        except ValueError as exc:
            raise ValueError(f"第{index}条记录无效：{exc}") from exc
    return records


def _load_supplement_manifest(path: Path, records: list[Record]) -> dict[str, list[dict[str, str]]]:
    """Hash explicitly listed local supplement files for their matched records."""
    if path.stat().st_size > 10 * 1024 * 1024:
        raise ValueError("补充材料清单超过10MB")
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if (not isinstance(payload, dict) or payload.get("schema_version") != 1
            or not isinstance(payload.get("materials"), list)):
        raise ValueError("补充材料清单必须使用schema_version=1和materials数组")

    key_counts: dict[str, int] = {}
    for record in records:
        key = key_for(record)
        key_counts[key] = key_counts.get(key, 0) + 1

    dependencies: dict[str, list[dict[str, str]]] = {}
    for index, item in enumerate(payload["materials"], 1):
        if not isinstance(item, dict) or set(item) != {"record_key", "role", "path"}:
            raise ValueError(
                f"补充材料 {index} 必须且只能包含record_key、role、path；SHA-256由CLI计算"
            )
        record_key, role, material_path = item["record_key"], item["role"], item["path"]
        if not isinstance(record_key, str) or not record_key:
            raise ValueError(f"补充材料 {index} 缺少record_key")
        if key_counts.get(record_key, 0) != 1:
            raise ValueError(f"补充材料 {index} 的record_key不存在或不唯一")
        if not isinstance(role, str) or not role:
            raise ValueError(f"补充材料 {index} 缺少role")
        if not isinstance(material_path, str) or not material_path:
            raise ValueError(f"补充材料 {index} 缺少本地path")

        local_path = Path(material_path)
        if not local_path.is_absolute():
            local_path = path.parent / local_path
        if not local_path.is_file():
            raise ValueError(f"补充材料 {index} 的本地文件不存在")
        digest = hashlib.sha256()
        with local_path.open("rb") as material_file:
            for chunk in iter(lambda: material_file.read(1024 * 1024), b""):
                digest.update(chunk)
        dependencies.setdefault(record_key, []).append({"role": role, "sha256": digest.hexdigest()})
    return dependencies


def _safe_args(args: argparse.Namespace) -> dict[str, Any]:
    hidden = {"email", "openalex_api_key", "ncbi_api_key"}
    return {k: v for k, v in vars(args).items() if k not in hidden} | {
        key + "_configured": bool(getattr(args, key)) for key in hidden
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    topic = load_topic(args.topic)
    started = datetime.now(timezone.utc)
    out_dir = Path(args.out_dir or f"outputs_v3/{topic.id}/{started.strftime('%Y%m%dT%H%M%S_%fZ')}")
    pdf_dir = Path(args.pdf_dir)
    if out_dir.exists() and any(out_dir.iterdir()):
        raise ValueError("输出目录不是空目录；请换一个目录，以免覆盖历史审查或残留旧产物")
    if out_dir.resolve() in {pdf_dir.resolve(), Path(args.cache_dir).resolve()}:
        raise ValueError("输出目录不能与PDF库/核验缓存相同")
    out_dir.mkdir(parents=True, exist_ok=True)
    client = HttpClient(email=args.email, retries=args.network_retries)
    store = MetadataStore(Path(args.cache_dir), client, offline=bool(args.from_json and not args.verify_online),
                          refresh=args.refresh_verification, max_age_days=args.cache_max_age_days)
    events: list[AuditEvent] = []
    raw_records: list[Record] = []
    source_counts: dict[str, int] = {}
    _atomic_write_text(out_dir / "topic_snapshot.json", json.dumps(topic.to_dict(), ensure_ascii=False, indent=2))
    manifest: dict[str, Any] = {
        "schema_version": 3, "pipeline_version": __version__, "status": "running",
        "started_at_utc": started.isoformat(), "arguments": _safe_args(args),
        "topic": topic.to_dict(), "topic_hash": topic.fingerprint, "python": sys.version,
        "platform": platform.platform(), "queries": {source: topic.query(source) for source in args.sources},
    }
    write_run_audit(out_dir, events, manifest)
    try:
        if args.from_json:
            input_path = Path(args.from_json)
            raw_records = _load_records(input_path)
            manifest["input"] = {"path": str(input_path.resolve()), "sha256": hashlib.sha256(input_path.read_bytes()).hexdigest()}
        else:
            def checkpoint(batch: list[Record]) -> None:
                raw_records.extend(batch)
                write_records_json(out_dir / "raw_records.json", raw_records)

            succeeded = 0
            for source in args.sources:
                before = len(raw_records)
                functions = {
                    "europepmc": lambda: fetch_europepmc(args.search_max, client=client, topic=topic,
                                                        events=events, on_batch=checkpoint, store=store),
                    "openalex": lambda: fetch_openalex(args.per_query, client=client, topic=topic,
                                                       api_key=args.openalex_api_key, email=args.email,
                                                       events=events, on_batch=checkpoint),
                    "pubmed": lambda: fetch_pubmed(args.pubmed_max, client=client, topic=topic, email=args.email,
                                                   api_key=args.ncbi_api_key, events=events, on_batch=checkpoint),
                }
                try:
                    functions[source]()
                    succeeded += 1
                    source_counts[source] = len(raw_records) - before
                    events.append(AuditEvent("search", "success", f"读取{source_counts[source]}条", source=source))
                except Exception as exc:
                    events.append(AuditEvent("search", "failed", f"{type(exc).__name__}: {exc}", source=source))
                    if not args.allow_partial_sources:
                        raise RuntimeError(f"{source}检索失败，已保留收到的原始记录；重试或明确 --allow-partial-sources") from exc
            if not succeeded:
                raise RuntimeError("全部检索源失败，不能把空结果报告为成功")
        write_records_json(out_dir / "raw_records.json", raw_records)
        merged = merge_records(raw_records, topic)
        supplemental_dependencies = (
            _load_supplement_manifest(Path(args.supplement_manifest), merged)
            if args.supplement_manifest else {}
        )
        requested = set()
        if args.review_decisions:
            review_path = Path(args.review_decisions)
            if review_path.stat().st_size > 10 * 1024 * 1024:
                raise ValueError("人工审阅文件超过10MB")
            review = json.loads(review_path.read_text(encoding="utf-8-sig"))
            if not isinstance(review, dict) or review.get("topic_hash") != topic.fingerprint:
                raise ValueError("人工审阅文件与课题不匹配")
            requested = {d.get("record_hash") for d in review.get("decisions", [])
                         if isinstance(d, dict) and d.get("decision") == "include" and isinstance(d.get("record_hash"), str)}
        candidates = [rec for rec in merged if record_fingerprint(rec) in requested or (
            rec.relevance_score >= args.score_threshold and passes_final_scope(rec, include_auxiliary=not args.core_only))]
        with ThreadPoolExecutor(max_workers=args.verification_workers) as executor:
            futures = {executor.submit(verify_record, rec, store): rec for rec in candidates}
            for index, future in enumerate(as_completed(futures), 1):
                events.extend(future.result())
                if index % 10 == 0:
                    print(f"身份核验 {index}/{len(candidates)}", file=sys.stderr)
        for rec in candidates:
            classify_and_score(rec, topic)
        # Re-screen authoritative abstracts. Keep ALL records and identity failures in
        # the audit instead of letting a conflict disappear behind relevance filtering.
        write_records_json(out_dir / "pre_download_records.json", candidates)
        if args.download_pdfs and not args.skip_download:
            verified = [rec for rec in candidates if rec.identity_status == "verified"]
            # download_pdfs validates serialized paths itself; preserve them until
            # that check so even undersized historical PDFs receive a rejection record.
            link_existing_pdfs([rec for rec in candidates if rec.identity_status != "verified"], pdf_dir)
            events.extend(download_pdfs(verified, pdf_dir, client=client, email=args.email,
                          max_pdfs=args.max_pdfs or None, timeout=args.download_timeout,
                          max_urls_per_record=args.max_urls_per_record, max_pdf_bytes=args.max_pdf_mb * 1024 * 1024,
                          include_landing=args.discover_landing_pdfs and not args.skip_landing_discovery,
                          log_path=out_dir / "pdf_download_log.csv"))
        else:
            link_existing_pdfs(candidates, pdf_dir)
        if not args.skip_pdf_extraction:
            events.extend(enrich_from_pdfs(candidates, max_pages=args.max_pdf_pages))
        for rec in candidates:
            build_evidence_cards(
                rec,
                topic,
                supplemental_dependencies=supplemental_dependencies.get(key_for(rec), []),
            )
        # Use all screened rows so human reviewers can rescue relevant rule false-negatives.
        # Rescued rows still need identity and evidence verification on a subsequent run.
        approved = apply_review_decisions(merged, topic, Path(args.review_decisions) if args.review_decisions else None)
        for rec in merged:
            extract_evidence(rec, topic)
        write_review_bundle(out_dir, merged, topic, approved)
        manifest["counts"] = {
            "by_source": source_counts, "raw": len(raw_records), "deduplicated": len(merged),
            "retained": len(candidates), "identity_verified": sum(r.identity_status == "verified" for r in candidates),
            "identity_conflicts": sum(r.identity_status == "metadata_conflict" for r in candidates),
            "with_pdf": sum(bool(r.pdf_file) for r in candidates),
            "with_results": sum(bool(r.results_excerpt) for r in candidates), "review_ready": len(approved),
        }
        final_events = write_outputs(candidates, out_dir, all_screened_records=merged, events=events)
        events = final_events
        failed_outputs = sum(e.stage == "output" and e.status == "failed" for e in events)
        warnings = any(e.status in {"failed", "truncated", "rejected"} for e in events)
        manifest["status"] = "failed" if failed_outputs else ("completed_with_warnings" if warnings else "completed")
        if failed_outputs:
            raise RuntimeError("有输出文件生成失败，见 events.jsonl")
    except Exception as exc:
        manifest["status"] = "failed"
        manifest.setdefault("counts", {"raw_before_failure": len(raw_records), "by_source": source_counts})
        events.append(AuditEvent("pipeline", "failed", f"{type(exc).__name__}: {exc}"))
        raise
    finally:
        completed = datetime.now(timezone.utc)
        manifest["completed_at_utc"] = completed.isoformat()
        manifest["duration_seconds"] = round((completed - started).total_seconds(), 3)
        write_run_audit(out_dir, events, manifest)
    return {**manifest["counts"], "status": manifest["status"], "out_dir": str(out_dir.resolve())}


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _validate_args(parser, args)
    try:
        summary = run(args)
    except (OSError, ValueError, RuntimeError) as exc:
        parser.exit(2, f"错误：{exc}\n")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
