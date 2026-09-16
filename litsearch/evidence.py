"""Traceable evidence cards and a fail-closed, human-reviewed synthesis boundary."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from .core import VALUE_RE, find_terms, key_for, record_fingerprint, split_sentences
from .models import Record
from .outputs import _atomic_write_text, _md_text, _write_csv
from .topics import Topic


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def build_evidence_cards(rec: Record, topic: Topic) -> None:
    rec.evidence = []
    sources = []
    if rec.verified_abstract:
        sources.append(("verified_abstract", rec.verified_abstract, rec.identity_status == "verified"))
    elif rec.abstract:
        sources.append(("unverified_abstract", rec.abstract, False))
    if rec.results_excerpt:
        sources.append(("pdf_results", rec.results_excerpt,
                        rec.identity_status == "verified" and rec.pdf_identity_status == "matched"))
    for kind, text, eligible in sources:
        offset = 0
        for sentence in split_sentences(text):
            start = text.find(sentence, offset)
            if start < 0 or len(sentence) < 25:
                continue
            offset = start + len(sentence)
            # Context is a whole sentence, never a number detached from an analyte.
            material = [topic.fingerprint, record_fingerprint(rec), kind, text_hash(text), start, offset]
            identity = text_hash(json.dumps(material))[:24]
            markers = list(re.finditer(r"\[\[PAGE (\d+)]]", text))
            first_page = rec.results_pages[0] if kind == "pdf_results" and rec.results_pages else None
            for marker in markers:
                if marker.start() <= start:
                    first_page = int(marker.group(1))
            pages = ([first_page] if first_page else []) + [int(m.group(1)) for m in markers if start < m.start() < offset]
            pages = list(dict.fromkeys(pages))
            rec.evidence.append({
                "id": identity, "source_kind": kind, "quote": sentence,
                "source_text_sha256": text_hash(text), "start": start, "end": offset,
                "pdf_sha256": rec.pdf_sha256 if kind == "pdf_results" else "",
                "page_hint": first_page, "pages": pages, "section": "Results (heuristic)" if kind == "pdf_results" else "Abstract",
                "target_mentions": find_terms(sentence, topic.products),
                "quantity_candidates": [match.group(0) for match in VALUE_RE.finditer(sentence)],
                "eligible": eligible, "warning": "同句出现不等于产量归属；需核对目标物、对照、单位和培养条件。",
            })


def review_template(records: list[Record], topic: Topic) -> dict:
    return {"schema_version": 1, "topic_hash": topic.fingerprint, "decisions": [
        {"record_key": key_for(rec), "record_hash": record_fingerprint(rec), "decision": "pending",
         "role": "primary", "reviewer": "", "reason": "", "claims": []}
        for rec in records
    ]}


def apply_review_decisions(records: list[Record], topic: Topic, path: Path | None) -> list[dict]:
    for rec in records:
        rec.review_status, rec.review_notes = "pending", ""
    if path is None:
        return []
    if path.stat().st_size > 10 * 1024 * 1024:
        raise ValueError("人工审阅文件超过10MB")
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if (not isinstance(payload, dict) or payload.get("schema_version") != 1
            or payload.get("topic_hash") != topic.fingerprint or not isinstance(payload.get("decisions"), list)):
        raise ValueError("人工审阅文件版本/课题哈希不匹配，不能跨课题沿用纳入决定")
    by_id = {(key_for(rec), record_fingerprint(rec)): rec for rec in records}
    seen: set[tuple[str, str]] = set()
    approved: list[dict] = []
    for decision in payload["decisions"]:
        if not isinstance(decision, dict):
            raise ValueError("人工审阅 decisions 必须为对象数组")
        identity = (decision.get("record_key", ""), decision.get("record_hash", ""))
        if any(not isinstance(x, str) for x in identity) or identity in seen or identity not in by_id:
            raise ValueError("人工审阅记录重复、不存在或元数据已变更；请重新生成模板")
        seen.add(identity)
        rec = by_id[identity]
        label = decision.get("decision")
        if label not in {"pending", "include", "exclude"}:
            raise ValueError("decision 只能是 pending/include/exclude")
        if label == "pending":
            continue
        for field in ("reviewer", "reason"):
            if not isinstance(decision.get(field), str) or not decision[field].strip():
                raise ValueError(f"人工决定缺少 {field}")
        rec.review_notes = f"{decision['reviewer']}: {decision['reason']}"
        if label == "exclude":
            rec.review_status = "excluded_by_reviewer"
            continue
        if rec.identity_status != "verified":
            rec.review_status = "blocked_identity"
            continue
        role = decision.get("role")
        if role not in {"primary", "background", "method"}:
            raise ValueError("role 必须为 primary/background/method")
        if rec.relevance_status == "excluded_document" or (role == "primary" and rec.relevance_status == "background_review"):
            rec.review_status = "blocked_document_type"
            continue
        cards = {card["id"]: card for card in rec.evidence if card["eligible"]}
        claims = decision.get("claims")
        if not isinstance(claims, list) or not claims:
            rec.review_status = "blocked_evidence"
            continue
        checked_claims = []
        for claim in claims:
            if not isinstance(claim, dict) or not isinstance(claim.get("text"), str) or not claim["text"].strip():
                raise ValueError("每项 claim 必须有人工填写的 text")
            ids = claim.get("evidence_ids")
            if not isinstance(ids, list) or not ids or any(not isinstance(i, str) or i not in cards for i in ids):
                rec.review_status = "blocked_evidence"
                break
            if claim.get("kind") not in {"qualitative", "quantitative"}:
                raise ValueError("claim.kind 必须为 qualitative/quantitative")
            if claim["kind"] == "qualitative" and VALUE_RE.search(claim["text"]):
                raise ValueError("含数值单位的结论不能标为qualitative来跳过定量背景字段")
            if claim["kind"] == "quantitative":
                context = claim.get("context")
                required = ("product", "host", "strain", "intervention", "comparator", "cultivation",
                            "medium", "metric", "value", "unit", "time", "scale", "limitations")
                if not isinstance(context, dict) or any(not isinstance(context.get(k), str) or not context[k].strip() for k in required):
                    raise ValueError("定量 claim 必须填写产物、宿主、菌株、改造、对照、培养、培养基、指标、数值、单位、时间、规模和局限；缺失写 not_reported")
                if context["metric"] not in {"titer", "yield", "productivity", "conversion", "other"}:
                    raise ValueError("metric 必须区分 titer/yield/productivity/conversion/other")
            checked_claims.append({**claim, "evidence": [cards[i] for i in ids]})
        else:
            rec.review_status = "approved"
            matched = [check for check in rec.identity_checks if check.get("status") == "matched"]
            canonical = next((check for check in matched if check.get("source") == "crossref"), matched[0] if matched else {})
            approved.append({"record_key": key_for(rec), "title": rec.title, "doi": rec.doi, "pmid": rec.pmid,
                             "year": canonical.get("remote_year") or rec.year,
                             "authors": canonical.get("remote_authors") or rec.authors,
                             "journal": canonical.get("remote_journal") or rec.journal,
                             "volume": canonical.get("remote_volume") or rec.volume,
                             "issue": canonical.get("remote_issue") or rec.issue,
                             "pages": canonical.get("remote_pages") or rec.pages, "role": role,
                             "reviewer": decision["reviewer"], "reason": decision["reason"],
                             "claims": checked_claims, "identity_checks": rec.identity_checks})
    return approved


def write_review_bundle(out_dir: Path, records: list[Record], topic: Topic, approved: list[dict]) -> None:
    _atomic_write_text(out_dir / "review_decisions.template.json", json.dumps(review_template(records, topic), ensure_ascii=False, indent=2))
    _atomic_write_text(out_dir / "review_ready.json", json.dumps(approved, ensure_ascii=False, indent=2))
    _atomic_write_text(out_dir / "evidence_cards.json", json.dumps([
        {"record_key": key_for(rec), "record_hash": record_fingerprint(rec), "cards": rec.evidence} for rec in records
    ], ensure_ascii=False, indent=2))
    _write_csv(out_dir / "identity_audit.csv", [
        {"title": rec.title, "doi": rec.doi, "pmid": rec.pmid, "identity_status": rec.identity_status,
         "relevance_status": rec.relevance_status, "review_status": rec.review_status,
         "checks": json.dumps(rec.identity_checks, ensure_ascii=False)} for rec in records
    ], ["title", "doi", "pmid", "identity_status", "relevance_status", "review_status", "checks"])
    lines = [f"# {_md_text(topic.name)}：人工审阅通过的写作材料", "",
             "本文件不是自动生成的综述。身份核验只证明数据库记录相符，不证明实验可重复；人工结论仍须负责任地审核。", "",
             f"研究问题：{_md_text(topic.question)}", "", f"通过门禁：{len(approved)} 篇。", ""]
    if not approved:
        lines.append("尚无可用于写作的已审阅证据。请填写人工审阅模板；不要将候选表直接用作参考文献。")
    for item in approved:
        lines.extend([f"## {_md_text(item['title'])}", "", f"DOI：{_md_text(item['doi'])}；PMID：{_md_text(item['pmid'])}；用途：{item['role']}", ""])
        for claim in item["claims"]:
            lines.extend(["人工结论：" + _md_text(claim["text"]), ""])
            for card in claim["evidence"]:
                lines.extend([f"原文（{card['source_kind']}；页码提示 {card['page_hint']}；证据 {card['id']}）：", "",
                              "> " + _md_text(card["quote"]), ""])
    _atomic_write_text(out_dir / "review_materials.md", "\n".join(lines) + "\n")
    # Only the reviewed subset may enter a reference-manager interchange file.
    ris = []
    for item in approved:
        ris.append("TY  - GEN")
        for tag, field in (("TI", "title"), ("DO", "doi"), ("PY", "year"), ("JO", "journal"),
                           ("VL", "volume"), ("IS", "issue"), ("SP", "pages")):
            if item.get(field):
                ris.append(f"{tag}  - " + re.sub(r"[\r\n]+", " ", item[field]))
        for author in item["authors"]:
            ris.append("AU  - " + re.sub(r"[\r\n]+", " ", author))
        ris.extend(["ER  - ", ""])
    _atomic_write_text(out_dir / "review_references.ris", "\n".join(ris))
