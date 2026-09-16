"""Pure normalization, conservative entity matching and evidence-location helpers."""
from __future__ import annotations

import copy
import hashlib
import html
import json
import re
import unicodedata
from collections import defaultdict
from functools import lru_cache
from typing import Iterable
from urllib.parse import unquote

from .models import Record
from .topics import Topic, load_topic


def norm_doi(value: str | None) -> str:
    """Normalize an identifier, not prose. Preserve legal suffix punctuation."""
    if not value or not isinstance(value, str):
        return ""
    cleaned = html.unescape(unquote(value)).strip().casefold()
    cleaned = re.sub(r"^doi\s*:\s*", "", cleaned)
    cleaned = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", cleaned)
    return cleaned if re.fullmatch(r"10\.\d{4,9}/[^\s\x00-\x1f]+", cleaned) else ""


def norm_title(value: str | None) -> str:
    text = unicodedata.normalize("NFKC", html.unescape(value or ""))
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip()


def title_key(value: str) -> str:
    text = norm_title(value).casefold().replace("β", "beta").replace("α", "alpha")
    return " ".join(re.findall(r"[^\W_]+", text, re.UNICODE))


def key_for(record: Record) -> str:
    if norm_doi(record.doi):
        return f"doi:{norm_doi(record.doi)}"
    if record.pmid:
        return f"pmid:{record.pmid}"
    material = [title_key(record.title), record.year, sorted(record.authors)]
    return "metadata:" + hashlib.sha256(json.dumps(material, ensure_ascii=False).encode()).hexdigest()[:24]


def record_fingerprint(record: Record) -> str:
    """Bind human decisions to the metadata actually screened, not just a DOI."""
    fields = [record.title, record.doi, record.pmid, record.year, record.authors, record.abstract]
    return hashlib.sha256(json.dumps(fields, ensure_ascii=False).encode()).hexdigest()


@lru_cache(maxsize=1024)
def _term_pattern(term: str) -> re.Pattern[str]:
    escaped = re.escape(unicodedata.normalize("NFKC", term)).replace(r"\ ", r"[\s‐‑–—-]+")
    if re.search(r"[\u3400-\u9fff]", term):
        return re.compile(escaped, re.I)
    return re.compile(rf"(?<![\w]){escaped}(?![\w])", re.I)


def contains_term(text: str, term: str) -> bool:
    return bool(_term_pattern(term).search(unicodedata.normalize("NFKC", text)))


def find_terms(text: str, terms: Iterable[str]) -> list[str]:
    return [term for term in terms if contains_term(text, term)]


def _linked_output(text: str, products: tuple[str, ...]) -> bool:
    for product in products:
        pattern = _term_pattern(product).pattern
        masked = re.sub(pattern + r"\s+(?:kinase|synthetase|transporter|supplementation|addition)\b",
                        " target-input ", text, flags=re.I)
        action = r"(?:production|overproduction|synthesis|biosynthesis|titer|titre|yield)"
        modifiers = r"(?:\s+(?:of|d|l|calcium|efficient|enhanced|microbial|de|novo|high|level|acid)){0,5}[\s‐‑–—-]+"
        if re.search(action + modifiers + pattern, masked, re.I) or re.search(
            pattern + r"\s+" + action, masked, re.I
        ):
            return True
        if re.search(pattern + r"\s+(?:was\s+)?(?:produced|accumulated)\b", masked, re.I):
            return True
    return False


def classify_and_score(rec: Record, topic: Topic | None = None) -> None:
    topic = topic or load_topic()
    rec.title = norm_title(rec.title)
    abstract = rec.verified_abstract if rec.identity_status == "verified" else rec.abstract
    text = f"{rec.title} {abstract}"
    products = find_terms(text, topic.products)
    pathways = find_terms(text, topic.pathway_terms)
    hosts = find_terms(text, topic.hosts)
    actions = find_terms(text, topic.interventions)
    exclusions = find_terms(text, topic.exclusions)
    non_article = find_terms(rec.title, ("additional file", "supplementary material", "supporting information",
                                       "correction", "corrigendum", "erratum", "retraction"))
    review = find_terms(rec.title, ("review", "progress", "perspectives")) or any(
        "review" in kind.casefold() for kind in rec.publication_types
    )
    rec.topic_id, rec.topic_hash = topic.id, topic.fingerprint
    rec.product = topic.name
    rec.category = "微生物代谢工程候选（规则，不是结论）"
    rec.relevance_score = 5 * bool(products) + 3 * bool(pathways) + 4 * bool(hosts) + 4 * bool(actions)
    rec.screening_reasons = [f"课题：{topic.question}"]
    for label, hits in (("目标物", products), ("通路", pathways), ("宿主/过程", hosts), ("工程信号", actions),
                        ("非生产场景（需复核，不作一票否决）", exclusions)):
        if hits:
            rec.screening_reasons.append(label + "：" + "、".join(hits))
    if non_article:
        rec.relevance_status, rec.library_tier = "excluded_document", "非独立研究文献"
    elif (products or pathways) and review:
        rec.relevance_status, rec.library_tier = "background_review", "综述/背景文献"
    elif products and hosts and actions and _linked_output(text, topic.products):
        rec.relevance_status, rec.library_tier = "direct_candidate", "核心生产/合成文献"
    elif (products or pathways) and (hosts or actions):
        rec.relevance_status, rec.library_tier = "pathway_candidate", "辅助通路/酶学/机制文献"
    elif products or pathways:
        rec.relevance_status, rec.library_tier = "manual_review", "待人工判断"
    else:
        rec.relevance_status, rec.library_tier = "out_of_scope", "背景文献"
    rec.reliability_cn = "规则仅作候选排序；文献身份、相关性及证据需分别核查。"


def passes_core_relevance(rec: Record) -> bool:
    if rec.relevance_status == "unscreened":
        classify_and_score(rec)
    return rec.relevance_status not in {"out_of_scope", "excluded_document"}


def passes_final_scope(rec: Record, include_auxiliary: bool = True) -> bool:
    return passes_core_relevance(rec) and (include_auxiliary or rec.relevance_status == "direct_candidate")


def _compatible(a: Record, b: Record) -> bool:
    if title_key(a.title) != title_key(b.title):
        return False
    for field in ("doi", "pmid", "pmcid"):
        if getattr(a, field) and getattr(b, field) and getattr(a, field) != getattr(b, field):
            return False
    if a.year and b.year and a.year != b.year:
        return False
    return not (a.authors and b.authors and {title_key(x) for x in a.authors}.isdisjoint(
        title_key(x) for x in b.authors
    ))


def merge_records(records: Iterable[Record], topic: Topic | None = None) -> list[Record]:
    """No mutation of inputs; merge corroborated identities, link uncertain matches."""
    materialized = copy.deepcopy(list(records))
    for rec in materialized:
        rec.title, rec.doi = norm_title(rec.title), norm_doi(rec.doi) or rec.doi
    materialized.sort(key=lambda r: (not bool(r.doi or r.pmid), key_for(r), record_fingerprint(r)))
    merged: list[Record] = []
    identifiers: dict[str, list[Record]] = defaultdict(list)
    titles: dict[str, list[Record]] = defaultdict(list)
    for rec in materialized:
        ids = [f"doi:{rec.doi}"] if rec.doi else []
        if rec.pmid:
            ids.append(f"pmid:{rec.pmid}")
        candidates = list({id(r): r for identity in ids for r in identifiers[identity]}.values())
        match = next((r for r in candidates if _compatible(r, rec)), None)
        if match is None and not ids:
            exact_rows = [r for r in titles[title_key(rec.title)] if record_fingerprint(r) == record_fingerprint(rec)]
            match = exact_rows[0] if len(exact_rows) == 1 else None
        if match is None and not ids:
            possible = [r for r in titles[title_key(rec.title)] if rec.year and rec.authors
                        and r.year and r.authors and _compatible(r, rec)]
            match = possible[0] if len(possible) == 1 else None
        if match is not None:
            for field in ("doi", "pmid", "pmcid", "year", "journal", "volume", "issue", "pages", "url"):
                if not getattr(match, field):
                    setattr(match, field, getattr(rec, field))
            if len(rec.abstract) > len(match.abstract):
                match.abstract = rec.abstract
            for field in ("source_ids", "pdf_urls", "publication_types"):
                setattr(match, field, sorted(set(getattr(match, field) + getattr(rec, field))))
            for identity in ids:
                if match not in identifiers[identity]:
                    identifiers[identity].append(match)
            continue
        for previous in candidates:
            for item in (previous, rec):
                warning = "同标识符元数据冲突，保留隔离待核实"
                if warning not in item.processing_warnings:
                    item.processing_warnings.append(warning)
        for previous in titles[title_key(rec.title)]:
            previous.duplicate_keys.append(key_for(rec))
            rec.duplicate_keys.append(key_for(previous))
        merged.append(rec)
        titles[title_key(rec.title)].append(rec)
        for identity in ids:
            identifiers[identity].append(rec)
    for rec in merged:
        classify_and_score(rec, topic)
        rec.duplicate_keys = sorted(set(rec.duplicate_keys))
    return sorted(merged, key=lambda r: (-r.relevance_score, key_for(r), record_fingerprint(r)))


def safe_pdf_name(rec: Record) -> str:
    readable = re.sub(r"[^A-Za-z0-9._-]", "_", rec.doi or "no-doi")[:120]
    digest = hashlib.sha256(key_for(rec).encode()).hexdigest()[:16]
    return f"{readable}_{digest}.pdf"


def split_sentences(text: str) -> list[str]:
    text = re.sub(r"\b([A-Z])\.\s+(?=[a-z])", r"\1<dot> ", text)
    return [x.strip().replace("<dot>", ".") for x in re.split(r"(?<=[.!?])\s+|(?<=[。！？])", text) if x.strip()]


VALUE_RE = re.compile(r"(?<![\w.])\d+(?:\.\d+)?\s*(?:mg|g|mmol|mol)\s*(?:/\s*[lL]|[lL][−-]1)(?!\w)|\d+(?:\.\d+)?\s*%", re.I)
OUTPUT_SIGNAL_RE = re.compile(r"\b(?:titer|titre|yield|productivity|produced|production|reached|achieved)\b|产量|效价|收率", re.I)


def extract_target_yields(text: str, topic: Topic | None = None) -> list[str]:
    """Return candidate QUOTATIONS, not normalized yields or established conclusions."""
    topic = topic or load_topic()
    return list(dict.fromkeys(sentence for sentence in split_sentences(text)
                             if find_terms(sentence, topic.products) and VALUE_RE.search(sentence)
                             and OUTPUT_SIGNAL_RE.search(sentence)))[:20]


RESULT_HEADING_RE = re.compile(r"(?im)^[ \t]*(?:\d+(?:\.\d+){0,3}[.)]?[ \t]+)?(?:results(?:[ \t]+and[ \t]+discussion)?|experimental[ \t]+results)[ \t]*$")
END_HEADING_RE = re.compile(r"(?im)^[ \t]*(?:\d+(?:\.\d+){0,3}[.)]?[ \t]+)?(?:discussion|conclusions?|materials[ \t]+and[ \t]+methods|experimental[ \t]+procedures|methods|references|acknowledg(?:e)?ments?)[ \t]*$")


def extract_results_like(text: str) -> str:
    clean = normalize_pdf_text(text)
    # Skip TOC headings with no body; keep repeated Results page headers.
    for heading in RESULT_HEADING_RE.finditer(clean):
        end = END_HEADING_RE.search(clean, heading.end())
        next_start = RESULT_HEADING_RE.search(clean, heading.end())
        probe_end = min(x.start() for x in (end, next_start) if x) if end or next_start else len(clean)
        body = re.sub(r"\[\[PAGE \d+]]", "", clean[heading.end():probe_end]).strip()
        if len(body) < 40:
            continue
        return clean[heading.start():end.start() if end else len(clean)].strip()
    return ""


def normalize_pdf_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).replace("\r\n", "\n").replace("\r", "\n")
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[^\S\n]+", " ", text))


def infer_strategy_cn(rec: Record, evidence: str, yields: list[str] | None = None) -> str:
    return "仅提供原文定位，不由关键词自动推断敲除/强化、机制、增产或因果；请在证据卡中填写人工核对的结论。"


def extract_evidence(rec: Record, topic: Topic | None = None) -> None:
    rec.final_yield = "未人工核对；数值线索不是最终产量"
    rec.method_result_cn = infer_strategy_cn(rec, "")
    rec.reliability_cn = f"身份：{rec.identity_status}；相关性：{rec.relevance_status}；PDF身份：{rec.pdf_identity_status}；综述门禁：{rec.review_status}。"


def subsection_summary_cn(rec: Record, heading: str, body: str) -> str:
    return infer_strategy_cn(rec, body)
