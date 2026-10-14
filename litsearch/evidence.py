"""Traceable evidence cards and a fail-closed, human-reviewed synthesis boundary."""
from __future__ import annotations

import hashlib
import json
import re
from enum import Enum
from pathlib import Path

from .core import VALUE_RE, find_terms, key_for, record_fingerprint, split_sentences
from .models import Record
from .outputs import _atomic_write_text, _md_text, _write_csv
from .topics import Topic


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# Bump manually whenever evidence-card extraction or interpretation changes.
# This stable version is part of every evidence ID; it is not a run timestamp.
EVIDENCE_EXTRACTOR_VERSION = "1.0.0"
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z", re.I)


class QuantitySignal(str, Enum):
    """Lexical quantity signal, not a conclusion about the science."""

    NONE = "none"
    QUANTITATIVE = "quantitative"
    NEEDS_REVIEW = "needs_review"


_VALUE_NUMBER = (
    r"(?:\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|"
    r"\d+(?:\.\d+)?\s*[×x]\s*10\s*\^\s*[+-]?\d+)"
)
_VALUE_WITH_ERROR = rf"{_VALUE_NUMBER}(?:\s*(?:±|\+/-)\s*{_VALUE_NUMBER})?"
_VALUE_RANGE = rf"{_VALUE_WITH_ERROR}(?:\s*(?:[-–—]|to|至)\s*{_VALUE_WITH_ERROR})?"
_KNOWN_QUANTITY_UNIT = (
    r"(?:(?:kg|mg|μg|µg|ug|ng|g)\s*/\s*(?:kg|g|mL|ml|L|l)|"
    r"(?:mmol|mol)\s*/\s*(?:L|l|mL|ml|g|kg)|"
    r"(?:kg|mg|μg|µg|ug|ng|g|mmol|mol)\s*[Ll]\s*[-−]\s*1|"
    r"(?:mM|μM|µM|uM|nM|pM)|%)"
)
_KNOWN_QUANTITY_RE = re.compile(
    rf"(?<![A-Za-z0-9_.]){_VALUE_RANGE}\s*{_KNOWN_QUANTITY_UNIT}(?![A-Za-z0-9_])", re.I
)
_FOLD_QUANTITY_RE = re.compile(
    rf"(?<![A-Za-z0-9_.]){_VALUE_NUMBER}\s*[-–—‐‑]?\s*(?:folds?|倍)(?![A-Za-z0-9_])", re.I
)
_NUMBER_TOKEN_RE = re.compile(rf"(?<![A-Za-z0-9_.]){_VALUE_NUMBER}(?![A-Za-z0-9_])", re.I)
_UNKNOWN_UNIT_RE = re.compile(
    rf"(?<![A-Za-z0-9_.]){_VALUE_RANGE}\s*(?:arbitrary\s+units?|units?|AU|U/L|"
    r"CFU(?:/[A-Za-z0-9]+)?|PFU(?:/[A-Za-z0-9]+)?|IU(?:/[A-Za-z0-9]+)?|"
    r"[A-Za-zμµ]{1,8}\s*/\s*[A-Za-zμµ0-9]{1,8})(?![A-Za-z0-9_])",
    re.I,
)
_BARE_UNIT_RE = re.compile(
    rf"(?<![A-Za-z0-9_.]){_VALUE_RANGE}\s*(?:kg|mg|μg|µg|ug|ng|g|mmol|mol)(?!\s*/)(?![A-Za-z0-9_])", re.I
)
_MEASUREMENT_CONTEXT_RE = re.compile(
    r"\b(?:titer|titre|yield|productivity|concentration|conversion|"
    r"specific\s+(?:activity|productivity)|content|value|rate)\b|"
    r"产量|效价|收率|得率|浓度|含量|比活性|产率",
    re.I,
)
_IDENTIFIER_NUMBER_RE = re.compile(
    r"\b(?:(?:in|during|published\s+in)\s+"
    r"(?:19|20)\d{2}(?:(?:\s*(?:,|and|or|to|[-–—‐‑])\s*)(?:19|20)\d{2})*|"
    r"K[-‐‑]12|MG1655|ilv2)\b",
    re.I,
)


def _span_is_covered(span: tuple[int, int], covering_spans: list[tuple[int, int]]) -> bool:
    start, end = span
    return any(cover_start <= start and end <= cover_end for cover_start, cover_end in covering_spans)


def _has_unconsumed_match(pattern: re.Pattern, text: str, consumed_spans: list[tuple[int, int]]) -> bool:
    return any(not _span_is_covered(match.span(), consumed_spans) for match in pattern.finditer(text))


def classify_quantity_signal(text: str) -> QuantitySignal:
    """Classify bounded quantity cues across the whole claim text.

    Known mass/concentration units, percentages, fold changes, and Chinese 倍
    expressions are quantitative. Registered suspicious-unit patterns or a
    bare number in the limited window around a listed measurement term need
    review. Unlisted words beside numbers are not exhaustively recognized.
    Other text has no detected quantity signal. These lexical rules do not
    assess scientific truth, unit attribution, or comparability.
    """
    normalized = text
    known_matches = [*_KNOWN_QUANTITY_RE.finditer(normalized), *_FOLD_QUANTITY_RE.finditer(normalized)]
    consumed_spans = [match.span() for match in known_matches]
    if (_has_unconsumed_match(_UNKNOWN_UNIT_RE, normalized, consumed_spans)
            or _has_unconsumed_match(_BARE_UNIT_RE, normalized, consumed_spans)):
        return QuantitySignal.NEEDS_REVIEW
    identifier_spans = [match.span() for match in _IDENTIFIER_NUMBER_RE.finditer(normalized)]
    for cue in _MEASUREMENT_CONTEXT_RE.finditer(normalized):
        start = max(0, cue.start() - 36)
        end = min(len(normalized), cue.end() + 36)
        for number in _NUMBER_TOKEN_RE.finditer(normalized, start, end):
            if (_span_is_covered(number.span(), consumed_spans)
                    or _span_is_covered(number.span(), identifier_spans)):
                continue
            return QuantitySignal.NEEDS_REVIEW
    return QuantitySignal.QUANTITATIVE if known_matches else QuantitySignal.NONE


def _dependency_hash(value: object, label: str) -> str:
    if value is None or value == "":
        return ""
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ValueError(f"{label} must be a 64-character SHA-256 hex digest")
    return value.lower()


def _normalize_supplemental_dependencies(
    dependencies: list[dict[str, str]] | None,
) -> tuple[list[dict[str, str]], bool]:
    if dependencies is None:
        return [], True
    if not isinstance(dependencies, list):
        raise ValueError("supplemental_dependencies must be a list")
    normalized = []
    hashes_complete = True
    for index, dependency in enumerate(dependencies, 1):
        if not isinstance(dependency, dict):
            raise ValueError(f"supplemental dependency {index} must be an object")
        if not set(dependency).issubset({"role", "sha256"}):
            raise ValueError(f"supplemental dependency {index} accepts only role and sha256")
        role = dependency.get("role")
        if (not isinstance(role, str) or not re.fullmatch(r"[a-z][a-z0-9_.:-]{0,63}", role)
                or role in {"abstract", "body_pdf"}):
            raise ValueError(f"supplemental dependency {index} has an invalid material role")
        digest = _dependency_hash(dependency.get("sha256"), f"supplemental dependency {index} sha256")
        if not digest:
            hashes_complete = False
        normalized.append({"role": role, "sha256": digest})
    normalized.sort(key=lambda dependency: (dependency["role"], dependency["sha256"]))
    return normalized, hashes_complete


def build_evidence_cards(
    rec: Record,
    topic: Topic,
    *,
    supplemental_dependencies: list[dict[str, str]] | None = None,
) -> None:
    """Build cards bound to the source material and extraction version.

    Callers may declare necessary supplemental materials with explicit role and
    SHA-256 values. A missing declared hash keeps PDF-derived cards ineligible.
    The normal CLI computes these hashes from files in its explicit local
    supplement manifest; it does not discover or download supplements.
    """
    rec.evidence = []
    supplements, supplements_complete = _normalize_supplemental_dependencies(supplemental_dependencies)
    sources = []
    if rec.verified_abstract:
        sources.append(("verified_abstract", rec.verified_abstract, rec.identity_status == "verified"))
    elif rec.abstract:
        sources.append(("unverified_abstract", rec.abstract, False))
    if rec.results_excerpt:
        sources.append(("pdf_results", rec.results_excerpt,
                        rec.identity_status == "verified" and rec.pdf_identity_status == "matched"))
    for kind, text, eligible in sources:
        if kind in {"verified_abstract", "unverified_abstract"}:
            material_dependencies = [{"role": "abstract", "sha256": text_hash(text)}]
            dependencies_complete = True
            pdf_hash = ""
        else:
            pdf_hash = _dependency_hash(rec.pdf_sha256, "pdf_sha256")
            material_dependencies = [{"role": "body_pdf", "sha256": pdf_hash}, *supplements]
            dependencies_complete = bool(pdf_hash) and supplements_complete
        offset = 0
        for sentence in split_sentences(text):
            start = text.find(sentence, offset)
            if start < 0 or len(sentence) < 25:
                continue
            offset = start + len(sentence)
            # Context is a whole sentence, never a number detached from an analyte.
            source_text_hash = text_hash(text)
            material = {
                "topic_hash": topic.fingerprint,
                "record_hash": record_fingerprint(rec),
                "source_kind": kind,
                "source_text_sha256": source_text_hash,
                "start": start,
                "end": offset,
                "material_dependencies": [dict(dependency) for dependency in material_dependencies],
                "extractor_version": EVIDENCE_EXTRACTOR_VERSION,
            }
            serialized_material = json.dumps(
                material, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            identity = text_hash(serialized_material)[:24]
            markers = list(re.finditer(r"\[\[PAGE (\d+)]]", text))
            first_page = rec.results_pages[0] if kind == "pdf_results" and rec.results_pages else None
            for marker in markers:
                if marker.start() <= start:
                    first_page = int(marker.group(1))
            pages = ([first_page] if first_page else []) + [int(m.group(1)) for m in markers if start < m.start() < offset]
            pages = list(dict.fromkeys(pages))
            warning = "同句出现不等于产量归属；需核对目标物、对照、单位和培养条件。"
            if not dependencies_complete:
                warning += " 材料依赖缺少有效SHA-256；此证据不得批准。"
            rec.evidence.append({
                "id": identity, "source_kind": kind, "quote": sentence,
                "source_text_sha256": source_text_hash, "start": start, "end": offset,
                "pdf_sha256": rec.pdf_sha256 if kind == "pdf_results" else "",
                "material_dependencies": [dict(dependency) for dependency in material_dependencies],
                "extractor_version": EVIDENCE_EXTRACTOR_VERSION,
                "page_hint": first_page, "pages": pages, "section": "Results (heuristic)" if kind == "pdf_results" else "Abstract",
                "target_mentions": find_terms(sentence, topic.products),
                "quantity_candidates": [match.group(0) for match in VALUE_RE.finditer(sentence)],
                "eligible": eligible and dependencies_complete, "warning": warning,
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
            quantity_signal = classify_quantity_signal(claim["text"])
            if quantity_signal is QuantitySignal.NEEDS_REVIEW:
                raise ValueError("疑似数量表达需核对后才能批准")
            if claim["kind"] == "qualitative" and quantity_signal is QuantitySignal.QUANTITATIVE:
                raise ValueError("含定量信号的结论必须标为quantitative并填写完整context")
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


_REVIEW_CONTEXT_FIELDS = (
    "product", "host", "strain", "intervention", "comparator", "cultivation",
    "medium", "metric", "value", "unit", "time", "scale", "limitations",
)
_REVIEW_CLAIM_CSV_FIELDS = [
    "record_key", "title", "doi", "claim_index", "kind", "text", "role",
    *_REVIEW_CONTEXT_FIELDS, "context_json", "evidence_ids_json", "source_kinds_json", "evidence_json",
    "claim_json",
]


def _review_output_projection(approved: list[dict]) -> dict:
    """Build shared claim rows while retaining the approved JSON objects unchanged."""
    projected_items = []
    projected_claims = []
    for item in approved:
        item_claims = []
        for claim_index, claim in enumerate(item.get("claims", []), 1):
            row = {
                "item": item,
                "claim": claim,
                "claim_index": claim_index,
                "has_context": "context" in claim,
                "context": claim.get("context"),
                "evidence": claim.get("evidence", []),
            }
            item_claims.append(row)
            projected_claims.append(row)
        projected_items.append({"item": item, "claims": item_claims})
    return {"approved": approved, "items": projected_items, "claims": projected_claims}


def _review_display(value: object) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return "null"
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _review_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def write_review_bundle(out_dir: Path, records: list[Record], topic: Topic, approved: list[dict]) -> None:
    projection = _review_output_projection(approved)
    _atomic_write_text(out_dir / "review_decisions.template.json", json.dumps(review_template(records, topic), ensure_ascii=False, indent=2))
    _atomic_write_text(out_dir / "review_ready.json", json.dumps(projection["approved"], ensure_ascii=False, indent=2))
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
             "本投影保留输入字段，不验证科学事实或推断材料等级；摘要证据不会因导出而升级为正文证据。CSV按claim逐行导出，不代表独立研究计数。", "",
             f"研究问题：{_md_text(topic.question)}", "", f"通过门禁：{len(approved)} 篇。", ""]
    if not approved:
        lines.append("尚无可用于写作的已审阅证据。请填写人工审阅模板；不要将候选表直接用作参考文献。")
    for projected_item in projection["items"]:
        item = projected_item["item"]
        lines.extend([
            f"## {_md_text(_review_display(item.get('title', '')))}", "",
            f"DOI：{_md_text(_review_display(item.get('doi', '')))}；PMID：{_md_text(_review_display(item.get('pmid', '')))}；用途：{_md_text(_review_display(item.get('role', '')))}", "",
        ])
        for row in projected_item["claims"]:
            claim = row["claim"]
            lines.extend([
                f"### Claim {row['claim_index']}（{_md_text(_review_display(claim.get('kind', '')))}）", "",
                "人工结论：" + _md_text(_review_display(claim.get("text", ""))), "",
            ])
            claim_metadata_fields = ("material_grade", "comparability")
            present_claim_metadata = [field for field in claim_metadata_fields if field in claim]
            if present_claim_metadata:
                lines.extend(["Claim-level metadata：", ""])
                for field in present_claim_metadata:
                    lines.append(
                        f"- claim.{field}: {_md_text(_review_display(claim[field]))}"
                    )
                lines.append("")
            context = row["context"]
            if not row["has_context"]:
                lines.extend(["Context：未提供（输入对象中不存在 context 字段）。", ""])
            elif context is None:
                lines.extend(["Context：显式 null（输入值为 null）。", ""])
            elif isinstance(context, dict):
                if context:
                    lines.extend(["Context：", ""])
                else:
                    lines.extend(["Context：空对象（输入为 {}）。", ""])
                for field in _REVIEW_CONTEXT_FIELDS:
                    value = context.get(field, "字段未提供（输入中不存在该字段）")
                    lines.append(f"- {_md_text(field)}: {_md_text(_review_display(value))}")
                for field, value in context.items():
                    if field not in _REVIEW_CONTEXT_FIELDS:
                        lines.append(f"- {_md_text(_review_display(field))}: {_md_text(_review_display(value))}")
                lines.append("")
            else:
                lines.extend(["Context：", "", "- context_value: " + _md_text(_review_display(context)), ""])

            for evidence_index, card in enumerate(row["evidence"], 1):
                if not isinstance(card, dict):
                    lines.extend([f"来源证据 {evidence_index}：", "", _md_text(_review_display(card)), ""])
                    continue
                position_fields = ("page_hint", "pages", "section", "start", "end")
                position = "; ".join(
                    f"{field}={_review_display(card[field])}"
                    for field in position_fields if field in card
                ) or "输入未提供"
                lines.extend([
                    f"来源证据 {evidence_index}：", "",
                    f"- id: {_md_text(_review_display(card.get('id', '输入未提供')))}",
                    f"- source_kind: {_md_text(_review_display(card.get('source_kind', '输入未提供')))}",
                    f"- source_position: {_md_text(position)}",
                ])
                for field in ("material_grade", "comparability"):
                    if field in card:
                        lines.append(
                            f"- evidence.{field}: {_md_text(_review_display(card[field]))}"
                        )
                for field in ("extractor_version", "material_dependencies"):
                    if field in card:
                        lines.append(
                            f"- {field}: {_md_text(_review_display(card[field]))}"
                        )
                if "warning" in card:
                    lines.append(f"- warning: {_md_text(_review_display(card['warning']))}")
                lines.extend(["- 原句：", "", "> " + _md_text(_review_display(card.get("quote", "原句未提供"))), ""])
    _atomic_write_text(out_dir / "review_materials.md", "\n".join(lines) + "\n")
    claim_rows = []
    for row in projection["claims"]:
        item, claim = row["item"], row["claim"]
        context = row["context"]
        evidence = row["evidence"]
        context_fields = context if isinstance(context, dict) else {}
        claim_rows.append({
            "record_key": item.get("record_key", ""),
            "title": item.get("title", ""),
            "doi": item.get("doi", ""),
            "claim_index": row["claim_index"],
            "kind": claim.get("kind", ""),
            "text": claim.get("text", ""),
            "role": item.get("role", ""),
            **{field: context_fields.get(field, "") for field in _REVIEW_CONTEXT_FIELDS},
            "context_json": _review_json(context) if row["has_context"] else "",
            "evidence_ids_json": _review_json(claim.get("evidence_ids", [])),
            "source_kinds_json": _review_json([
                card.get("source_kind") if isinstance(card, dict) else card for card in evidence
            ]),
            "evidence_json": _review_json(evidence),
            "claim_json": _review_json(claim),
        })
    _write_csv(out_dir / "review_claims.csv", claim_rows, _REVIEW_CLAIM_CSV_FIELDS)
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
