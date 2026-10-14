from __future__ import annotations

import html
import json
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from typing import Any, Callable

from .core import norm_doi, norm_title
from .models import AuditEvent, Record
from .net import HttpClient
from .topics import Topic, load_topic
from .verification import EPMC_SEARCH, MetadataStore, epmc_record


# Legacy exports are derived from the default profile; the CLI always passes a
# Topic explicitly, so another study never inherits pantothenate search strings.
_DEFAULT_TOPIC = load_topic()
TITLE_QUERIES = tuple(_DEFAULT_TOPIC.products)
SEARCH_QUERIES = (_DEFAULT_TOPIC.query("openalex"),)
PUBMED_QUERIES = (_DEFAULT_TOPIC.query("pubmed"),)


def inverted_index_to_text(index: dict[str, list[int]] | None) -> str:
    if not index:
        return ""
    pairs: list[tuple[int, str]] = []
    for word, positions in index.items():
        for position in positions:
            if isinstance(position, int):
                pairs.append((position, word))
    return " ".join(word for _, word in sorted(pairs))


def add_pdf_url(record: Record, url: str | None) -> None:
    if url:
        cleaned = html.unescape(str(url)).strip()
        if cleaned and cleaned not in record.pdf_urls:
            record.pdf_urls.append(cleaned)


def record_from_openalex(item: dict[str, Any], source_label: str) -> Record:
    primary = item.get("primary_location") or {}
    source = primary.get("source") or {}
    best_oa = item.get("best_oa_location") or {}
    biblio = item.get("biblio") or {}
    authors: list[str] = []
    for authorship in item.get("authorships") or []:
        author = authorship.get("author") or {}
        name = author.get("display_name")
        if name:
            authors.append(str(name))
    rec = Record(
        title=norm_title(item.get("display_name") or item.get("title") or ""),
        doi=norm_doi(item.get("doi")),
        year=str(item.get("publication_year") or ""),
        journal=str(source.get("display_name") or ""),
        volume=str(biblio.get("volume") or ""),
        issue=str(biblio.get("issue") or ""),
        pages=str(biblio.get("first_page") or ""),
        authors=authors,
        abstract=inverted_index_to_text(item.get("abstract_inverted_index")),
        url=str(item.get("doi") or item.get("id") or ""),
        source_ids=[source_label, str(item.get("id") or "")],
    )
    add_pdf_url(rec, best_oa.get("pdf_url"))
    add_pdf_url(rec, primary.get("pdf_url"))
    for location in item.get("locations") or []:
        add_pdf_url(rec, location.get("pdf_url"))
    return rec


def _openalex_batch(data: Any, per_query: int) -> tuple[list[dict[str, Any]], int]:
    """Validate the small, single-page OpenAlex response contract."""
    if not isinstance(data, dict):
        raise ValueError("OpenAlex response must be an object")
    if data.get("error"):
        raise ValueError("OpenAlex response reported an API error")
    results = data.get("results")
    meta = data.get("meta")
    if not isinstance(results, list):
        raise ValueError("OpenAlex response results must be an array")
    if not isinstance(meta, dict) or type(meta.get("count")) is not int or meta["count"] < 0:
        raise ValueError("OpenAlex response meta.count must be a non-negative integer")
    count = meta["count"]
    if count < len(results):
        raise ValueError("OpenAlex response count is smaller than the returned results")
    expected = min(count, per_query)
    if len(results) != expected:
        raise ValueError("OpenAlex response is incomplete for the requested page")
    valid: list[dict[str, Any]] = []
    for item in results:
        if not isinstance(item, dict):
            raise ValueError("OpenAlex response contains a non-object result")
        title_value = item.get("display_name") or item.get("title")
        if not isinstance(title_value, str):
            raise ValueError("OpenAlex response contains a non-string title")
        for field in ("primary_location", "best_oa_location", "biblio"):
            value = item.get(field)
            if value is not None and not isinstance(value, dict):
                raise ValueError("OpenAlex response contains an invalid nested object")
        locations = item.get("locations")
        if locations is not None:
            if not isinstance(locations, list) or any(not isinstance(location, dict) for location in locations):
                raise ValueError("OpenAlex response contains invalid locations")
        authorships = item.get("authorships")
        if authorships is not None:
            if not isinstance(authorships, list) or any(not isinstance(authorship, dict) for authorship in authorships):
                raise ValueError("OpenAlex response contains invalid authorships")
        abstract_index = item.get("abstract_inverted_index")
        if abstract_index is not None and not isinstance(abstract_index, dict):
            raise ValueError("OpenAlex response contains an invalid abstract index")
        if abstract_index is not None and any(
            not isinstance(word, str) or not isinstance(positions, list)
            or any(type(position) is not int for position in positions)
            for word, positions in abstract_index.items()
        ):
            raise ValueError("OpenAlex response contains an invalid abstract index")
        title = norm_title(title_value)
        if not title:
            raise ValueError("OpenAlex response contains a result without a title")
        valid.append(item)
    return valid, count


def fetch_openalex(
    per_query: int,
    *,
    client: HttpClient,
    api_key: str,
    email: str,
    sleeper: Callable[[float], None] = time.sleep,
    topic: Topic | None = None,
    events: list[AuditEvent] | None = None,
    on_batch: Callable[[list[Record]], None] | None = None,
) -> list[Record]:
    if not api_key:
        raise ValueError("OpenAlex API key is required (set OPENALEX_API_KEY or --openalex-api-key)")
    if not 1 <= per_query <= 200:
        raise ValueError("per_query must be between 1 and 200")
    records: list[Record] = []
    common = {"per-page": str(per_query), "api_key": api_key}
    if email:
        common["mailto"] = email
    for query in (() if topic else TITLE_QUERIES):
        params = {**common, "filter": f"title.search:{query}"}
        url = "https://api.openalex.org/works?" + urllib.parse.urlencode(params)
        data = client.get_json(url)
        try:
            items, total = _openalex_batch(data, per_query)
        except ValueError as exc:
            if events is not None:
                events.append(AuditEvent("search_coverage", "failed", f"query={query}; {exc}", source="openalex"))
            raise
        try:
            for item in items:
                records.append(record_from_openalex(item, f"OpenAlex title:{query}"))
        except (TypeError, AttributeError, ValueError):
            if events is not None:
                events.append(AuditEvent("search_coverage", "failed", f"query={query}; invalid result structure", source="openalex"))
            raise ValueError("OpenAlex response contains an invalid result structure") from None
        if on_batch:
            on_batch(records[-len(items):] if items else [])
        if events is not None:
            events.append(AuditEvent("search_coverage", "truncated" if total > per_query else "success",
                                     f"query={query}; available={total}; retrieved={len(items)}; 单页上限明确记录",
                                     source="openalex"))
        sleeper(0.11)
    for query in ((topic.query("openalex"),) if topic else SEARCH_QUERIES):
        params = {**common, "search": query}
        url = "https://api.openalex.org/works?" + urllib.parse.urlencode(params)
        data = client.get_json(url)
        try:
            items, total = _openalex_batch(data, per_query)
        except ValueError as exc:
            if events is not None:
                events.append(AuditEvent("search_coverage", "failed", f"query={query}; {exc}", source="openalex"))
            raise
        before = len(records)
        try:
            for item in items:
                records.append(record_from_openalex(item, f"OpenAlex search:{query}"))
        except (TypeError, AttributeError, ValueError):
            if events is not None:
                events.append(AuditEvent("search_coverage", "failed", f"query={query}; invalid result structure", source="openalex"))
            raise ValueError("OpenAlex response contains an invalid result structure") from None
        if on_batch:
            on_batch(records[before:])
        if events is not None:
            events.append(AuditEvent("search_coverage", "truncated" if total > per_query else "success",
                                     f"query={query}; available={total}; retrieved={len(records) - before}; 单页上限明确记录",
                                     source="openalex"))
        sleeper(0.11)
    return records


def _pubmed_year(journal: ET.Element | None) -> str:
    if journal is None:
        return ""
    year = journal.findtext("./JournalIssue/PubDate/Year", default="")
    if year:
        return year
    medline_date = journal.findtext("./JournalIssue/PubDate/MedlineDate", default="")
    match = re.search(r"\b(?:18|19|20)\d{2}\b", medline_date)
    return match.group(0) if match else ""


def _pubmed_common_params(email: str, api_key: str) -> dict[str, str]:
    params = {"tool": "d_pan_literature_pipeline", "email": email}
    if api_key:
        params["api_key"] = api_key
    return params


def fetch_pubmed(
    max_results: int,
    *,
    client: HttpClient,
    email: str,
    api_key: str = "",
    sleeper: Callable[[float], None] = time.sleep,
    topic: Topic | None = None,
    events: list[AuditEvent] | None = None,
    on_batch: Callable[[list[Record]], None] | None = None,
) -> list[Record]:
    if not email:
        raise ValueError("PubMed requests require a contact email")
    if not 1 <= max_results <= 10_000:
        raise ValueError("max_results must be between 1 and 10000")
    records: list[Record] = []
    seen_pmids: set[str] = set()
    common = _pubmed_common_params(email, api_key)
    delay = 0.11 if api_key else 0.34
    for query in ((topic.query("pubmed"),) if topic else PUBMED_QUERIES):
        params = {
            **common,
            "db": "pubmed",
            "term": query,
            "retmax": str(max_results),
            "retmode": "json",
            "sort": "relevance",
        }
        esearch_url = (
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?"
            + urllib.parse.urlencode(params)
        )
        data = client.get_json(esearch_url)
        search = data.get("esearchresult") or {}
        if "idlist" not in search or data.get("error") or search.get("errorlist"):
            raise ValueError("PubMed search returned an invalid/error response")
        ids = search["idlist"]
        if events is not None:
            total = int(search.get("count", len(ids)))
            events.append(AuditEvent("search_coverage", "truncated" if total > len(ids) else "success",
                                     f"query={query}; available={total}; retrieved_ids={len(ids)}", source="pubmed"))
        sleeper(delay)
        for offset in range(0, len(ids), 100):
            chunk = [str(item) for item in ids[offset : offset + 100]]
            body = urllib.parse.urlencode(
                {**common, "db": "pubmed", "id": ",".join(chunk), "retmode": "xml"}
            ).encode("utf-8")
            response = client.post_bytes(
                "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
                body,
            )
            root = ET.fromstring(response.body)
            if root.find(".//ERROR") is not None:
                raise ValueError("PubMed efetch returned an error")
            before = len(records)
            batch: list[Record] = []
            received_ids: list[str] = []
            for article in root.findall(".//PubmedArticle"):
                medline = article.find("./MedlineCitation")
                art = medline.find("./Article") if medline is not None else None
                if art is None:
                    continue
                title_node = art.find("./ArticleTitle")
                title = norm_title(
                    ET.tostring(title_node, encoding="unicode", method="text")
                    if title_node is not None
                    else ""
                )
                journal = art.find("./Journal")
                authors: list[str] = []
                for author in art.findall("./AuthorList/Author"):
                    last = author.findtext("LastName", default="")
                    fore = author.findtext("ForeName", default="")
                    collective = author.findtext("CollectiveName", default="")
                    name = " ".join(part for part in (fore, last) if part) or collective
                    if name:
                        authors.append(name)
                abstract = " ".join(
                    norm_title(ET.tostring(node, encoding="unicode", method="text"))
                    for node in art.findall("./Abstract/AbstractText")
                )
                doi = ""
                pmcid = ""
                pmid = medline.findtext("./PMID", default="") if medline is not None else ""
                if not pmid or not title:
                    continue
                for identifier in article.findall("./PubmedData/ArticleIdList/ArticleId"):
                    kind = identifier.attrib.get("IdType", "")
                    if kind == "doi":
                        doi = norm_doi(identifier.text)
                    elif kind == "pmc":
                        pmcid = identifier.text or ""
                if not doi:
                    for location in art.findall("./ELocationID"):
                        if location.attrib.get("EIdType") == "doi":
                            doi = norm_doi(location.text)
                            break
                rec = Record(
                    title=title,
                    doi=doi,
                    year=_pubmed_year(journal),
                    journal=journal.findtext("./Title", default="") if journal is not None else "",
                    volume=journal.findtext("./JournalIssue/Volume", default="") if journal is not None else "",
                    issue=journal.findtext("./JournalIssue/Issue", default="") if journal is not None else "",
                    pages=art.findtext("./Pagination/MedlinePgn", default=""),
                    authors=authors,
                    abstract=abstract,
                    pmid=pmid,
                    pmcid=pmcid,
                    source_ids=["PubMed", pmid],
                    url=f"https://doi.org/{doi}" if doi else f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                    publication_types=[node.text or "" for node in art.findall("./PublicationTypeList/PublicationType")],
                )
                if pmcid:
                    add_pdf_url(rec, f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/pdf/")
                batch.append(rec)
                received_ids.append(str(pmid))
            requested_ids = set(chunk)
            received_set = set(received_ids)
            missing = sorted(requested_ids - received_set)
            unexpected = sorted(received_set - requested_ids)
            duplicates = sorted({pmid for pmid in received_ids if received_ids.count(pmid) > 1})
            diagnostics = {"requested": chunk, "received": received_ids, "missing": missing,
                           "unexpected": unexpected, "duplicate": duplicates}
            unique_batch: list[Record] = []
            batch_seen: set[str] = set()
            for record in batch:
                if record.pmid and record.pmid not in seen_pmids and record.pmid not in batch_seen:
                    unique_batch.append(record)
                    batch_seen.add(record.pmid)
            seen_pmids.update(record.pmid for record in unique_batch if record.pmid)
            records.extend(unique_batch)
            if on_batch and unique_batch:
                on_batch(unique_batch)
            if missing or unexpected or duplicates or len(received_ids) != len(chunk):
                if events is not None:
                    events.append(AuditEvent("efetch_coverage", "failed", json.dumps(diagnostics, ensure_ascii=False, sort_keys=True), source="pubmed"))
                raise ValueError("PubMed efetch batch is incomplete: " + json.dumps(diagnostics, ensure_ascii=False, sort_keys=True))
            if on_batch:
                # Complete batches are checkpointed exactly once as well.
                if not batch:
                    on_batch([])
            if events is not None:
                events.append(AuditEvent("efetch_coverage", "success", json.dumps(diagnostics, ensure_ascii=False, sort_keys=True), source="pubmed"))
            sleeper(delay)
    return records


def fetch_europepmc(max_results: int, *, client: HttpClient, topic: Topic,
                    events: list[AuditEvent], store: MetadataStore | None = None,
                    on_batch: Callable[[list[Record]], None] | None = None,
                    sleeper: Callable[[float], None] = time.sleep) -> list[Record]:
    """Cursor pagination with a declared budget; missing pages are never 'zero hits'."""
    if not 1 <= max_results <= 100_000:
        raise ValueError("Europe PMC max_results must be 1..100000")
    query, cursor = topic.query("europepmc"), "*"
    seen_cursors: set[str] = set()
    seen_ids: set[tuple[str, str]] = set()
    records: list[Record] = []
    total = 0
    while len(records) < max_results:
        if cursor in seen_cursors:
            raise ValueError("Europe PMC cursor repeated before all requested results were read")
        seen_cursors.add(cursor)
        url = EPMC_SEARCH + urllib.parse.urlencode({"query": query, "format": "json", "resultType": "core",
                                                   "pageSize": min(100, max_results - len(records)), "cursorMark": cursor})
        data = store.get(url)[0] if store else client.get_json(url)
        items = (data.get("resultList") or {}).get("result")
        if not isinstance(items, list) or "hitCount" not in data:
            raise ValueError("Europe PMC search response is invalid")
        total = int(data["hitCount"])
        batch = []
        for item in items:
            identity = (str(item.get("source") or ""), str(item.get("id") or ""))
            if not all(identity) or not item.get("title"):
                raise ValueError("Europe PMC returned an item without identity/title")
            if identity not in seen_ids:
                seen_ids.add(identity)
                batch.append(epmc_record(item))
        records.extend(batch)
        if on_batch:
            on_batch(batch)
        if len(records) >= min(total, max_results):
            break
        next_cursor = data.get("nextCursorMark")
        if not batch or not next_cursor or next_cursor == cursor:
            raise ValueError("Europe PMC pagination ended before hitCount was reached")
        cursor = str(next_cursor)
        sleeper(0.15)
    events.append(AuditEvent("search_coverage", "truncated" if len(records) < total else "success",
                             f"query={query}; available={total}; retrieved={len(records)}; limit={max_results}",
                             source="europepmc"))
    return records
