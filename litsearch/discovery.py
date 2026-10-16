"""Bounded candidate recovery, with an offline plan and auditable citation routes.

No result is approved evidence. Europe PMC covers indexed life science literature,
not all disciplines. API-reported exhaustion never proves exhaustive recall.
"""
from __future__ import annotations

import argparse
from collections import deque
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

from .net import HttpClient
from .topics import Topic, load_topic

EPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest"
CROSSREF = "https://api.crossref.org/works/"
MAX_JSON = 4 * 1024 * 1024
SOURCES = {"MED", "PMC", "AGR", "CBA", "CTX", "ETH", "HIR", "NBK", "PAT", "PPR"}


def _json(body: bytes) -> Any:
    def unique(pairs: list) -> dict:
        result: dict = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result
    if len(body) > MAX_JSON:
        raise ValueError("JSON input exceeds 4 MiB")
    try:
        return json.loads(body.decode("utf-8-sig"), object_pairs_hook=unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")))
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("invalid JSON input") from exc


def _read_json(path: Path) -> Any:
    with path.open("rb") as stream:
        return _json(stream.read(MAX_JSON + 1))


def _doi(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    if re.search(r"[\x00-\x1f\x7f]", value):
        return ""
    # Only explicit DOI fields, never titles/unstructured reference strings.
    value = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", value.strip(), flags=re.I)
    return value.casefold() if re.fullmatch(r"10\.\d{4,9}/[^\s\"<>\\]{1,180}", value) else ""


def _epmc_id(source: Any, identifier: Any) -> tuple[str, str] | None:
    if not isinstance(source, str) or not isinstance(identifier, str):
        return None
    source = source.upper()
    if source not in SOURCES or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", identifier):
        return None
    if source == "MED" and not identifier.isdecimal():
        return None
    if source == "PMC" and not re.fullmatch(r"PMC\d+", identifier.upper()):
        return None
    return source, identifier.upper() if source == "PMC" else identifier


def validate_seeds(values: Any) -> list[dict]:
    if not isinstance(values, list) or len(values) > 100:
        raise ValueError("seeds must be an array of at most 100 identifiers")
    result = []
    for value in values:
        if not isinstance(value, dict):
            raise ValueError("each seed must be an object")
        if set(value) == {"doi"} and _doi(value["doi"]):
            clean = {"doi": _doi(value["doi"])}
        elif set(value) == {"source", "id"} and _epmc_id(value["source"], value["id"]):
            source, identifier = _epmc_id(value["source"], value["id"])  # type: ignore[misc]
            clean = {"source": source, "id": identifier}
        else:
            raise ValueError("seed needs a valid DOI or Europe PMC source/id")
        if clean not in result:
            result.append(clean)
    return result


def validate_aliases(values: Any) -> list[str]:
    if not isinstance(values, list) or len(values) > 100:
        raise ValueError("aliases must be an array of at most 100 phrases")
    result = []
    for value in values:
        if (not isinstance(value, str) or not value.strip() or len(value) > 120
                or re.search(r'["\\\[\]{}:()*?~^\x00-\x1f\x7f]', value)
                or value.strip().upper() in {"AND", "OR", "NOT"}):
            raise ValueError("alias must be a literal phrase without query syntax")
        value = value.strip().casefold()
        if value not in result:
            result.append(value)
    return result


@dataclass(frozen=True)
class Budgets:
    requests: int = 50
    pages: int = 30
    records: int = 1000  # all observations, including duplicates and unresolved references
    depth: int = 1
    page_size: int = 100

    def validate(self) -> None:
        for name, high in (("requests", 500), ("pages", 500), ("records", 5000), ("page_size", 1000)):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= high:
                raise ValueError(f"{name} budget must be 1..{high}")
        if type(self.depth) is not int or not 0 <= self.depth <= 5:
            raise ValueError("depth budget must be 0..5")


def _engineering_topic(protocol: dict) -> Topic:
    expected = {"schema_version", "domain", "id", "name", "facets", "official_queries", "boundaries"}
    if (set(protocol) != expected or type(protocol.get("schema_version")) is not int
            or protocol["schema_version"] != 1 or protocol.get("domain") != "engineering"):
        raise ValueError("invalid engineering protocol schema")
    if not isinstance(protocol.get("id"), str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", protocol["id"]):
        raise ValueError("invalid engineering protocol id")
    for field in ("name", "boundaries"):
        if not isinstance(protocol[field], str) or not protocol[field].strip() or len(protocol[field]) > 10000:
            raise ValueError("invalid engineering text")
    facets = protocol["facets"]
    if not isinstance(facets, list) or not 1 <= len(facets) <= 50:
        raise ValueError("engineering facets must be a bounded nonempty array")
    terms, ids = [], []
    for facet in facets:
        if not isinstance(facet, dict) or set(facet) != {"id", "label", "terms", "query", "source_kinds"}:
            raise ValueError("invalid engineering facet")
        if (not isinstance(facet["id"], str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", facet["id"])
                or facet["id"] in ids):
            raise ValueError("invalid or duplicate facet id")
        ids.append(facet["id"])
        validate_aliases([facet["label"], facet["query"]])
        terms.extend(validate_aliases(facet["terms"]))
        if not isinstance(facet["source_kinds"], list) or not facet["source_kinds"] or any(
                kind not in {"research", "review", "facility", "vendor", "institution", "hazard", "bibliography"}
                for kind in facet["source_kinds"]):
            raise ValueError("invalid facet source kinds")
    official = protocol["official_queries"]
    if not isinstance(official, list) or len(official) > 100:
        raise ValueError("invalid official query array")
    for item in official:
        if not isinstance(item, dict) or set(item) != {"kind", "domain", "query"} or any(
                not isinstance(value, str) or not value.strip() or len(value) > 1000 for value in item.values()):
            raise ValueError("invalid official query metadata")
    return Topic(protocol["id"], protocol["name"], protocol["boundaries"], tuple(dict.fromkeys(terms)), (),
                 ("semiconductor",), tuple(ids), ())


def load_discovery_topic(name: str) -> Topic | dict:
    path = Path(name)
    if not path.is_file():
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", name):
            raise ValueError("invalid topic name")
        path = Path(__file__).with_name("profiles") / f"{name}.json"
    data = _read_json(path)
    if isinstance(data, dict) and data.get("domain") == "engineering":
        _engineering_topic(data)
        return data
    return load_topic(name)


def build_plan(topic: Topic | dict, aliases: list[str], seeds: list[dict], budgets: Budgets,
               *, sources: list[str] | None = None) -> dict:
    if isinstance(topic, dict):
        adapted = _engineering_topic(topic)
        plan = build_plan(adapted, aliases, seeds, budgets, sources=sources)
        plan["topic"] = topic
        plan["topic_fingerprint"] = hashlib.sha256(json.dumps(topic, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if "crossref" in plan["sources"]:
            plan["crossref_queries"] = list(dict.fromkeys(
                plan["crossref_queries"] + [facet["query"] for facet in topic["facets"]]))
        plan["limits"].append("Official engineering document queries remain a separate workflow; not executed by this tool")
        return plan
    budgets.validate()
    aliases = validate_aliases(aliases)
    seeds = validate_seeds(seeds)
    sources = ["europepmc"] if sources is None else sources
    if (not isinstance(sources, list) or not sources
            or any(source not in {"europepmc", "crossref"} for source in sources)):
        raise ValueError("sources must contain europepmc and/or crossref")
    sources = list(dict.fromkeys(sources))
    terms = validate_aliases(list(topic.products) + list(topic.pathway_terms) + aliases)
    baseline = topic.query("europepmc")
    broad = "(" + " OR ".join(f'TITLE_ABS:"{term}"' for term in terms) + ")"
    queries = list(dict.fromkeys([baseline, broad])) if "europepmc" in sources else []
    return {"schema_version": 1, "topic": json.loads(json.dumps(topic.to_dict())), "topic_fingerprint": topic.fingerprint,
            "aliases": aliases, "queries": queries, "sources": sources,
            "crossref_queries": terms if "crossref" in sources else [],
            "seeds": seeds, "budgets": asdict(budgets),
            "candidate_status": "unreviewed", "approval": None,
            "limits": ["Europe PMC indexed life science coverage only; select Crossref for broader disciplines",
                       "Crossref query terms are individual relevance-ranked text searches, not Boolean OR",
                       "Citation expansion uses Europe PMC identifiers independently of selected keyword providers",
                       "No exhaustive recall claim",
                       "Crossref supplies deposited backward references; no forward citation endpoint",
                       "Citation-only metadata without a stable identifier remains unresolved"]}


class BudgetStop(Exception):
    pass


def validate_plan(plan: Any) -> dict:
    if not isinstance(plan, dict) or not isinstance(plan.get("topic"), dict):
        raise ValueError("invalid plan")
    data = plan["topic"]
    if data.get("domain") == "engineering":
        _engineering_topic(data)
        rebuilt = build_plan(data, plan.get("aliases"), plan.get("seeds"), Budgets(**plan["budgets"]),
                             sources=plan.get("sources"))
        if plan != rebuilt:
            raise ValueError("engineering protocol fingerprint or queries were modified")
        return plan
    if (set(data) != set(Topic.__dataclass_fields__) or type(data.get("schema_version")) is not int
            or data.get("schema_version") != 1 or type(plan.get("schema_version")) is not int):
        raise ValueError("invalid topic schema")
    for field in ("id", "name", "question"):
        if not isinstance(data[field], str) or not data[field].strip() or len(data[field]) > 10000:
            raise ValueError("invalid topic text")
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", data["id"]):
        raise ValueError("invalid topic id")
    clean = dict(data)
    for field in ("products", "pathway_terms", "hosts", "interventions", "exclusions"):
        clean[field] = tuple(validate_aliases(data[field]))
        if field in {"products", "hosts", "interventions"} and not clean[field]:
            raise ValueError("missing topic terms")
    if not isinstance(plan.get("budgets"), dict) or set(plan["budgets"]) != set(Budgets.__dataclass_fields__):
        raise ValueError("invalid plan budgets")
    rebuilt = build_plan(Topic(**clean), plan.get("aliases"), plan.get("seeds"), Budgets(**plan["budgets"]),
                         sources=plan.get("sources"))
    if plan != rebuilt:
        raise ValueError("plan fingerprint, queries, or protocol fields were modified")
    return plan


def _safe_endpoint(url: str) -> bool:
    parts = urlsplit(url)
    return (parts.scheme == "https" and not parts.username and not parts.password
            and parts.port is None and
            ((parts.netloc == "www.ebi.ac.uk" and parts.path.startswith("/europepmc/webservices/rest/"))
             or (parts.netloc == "api.crossref.org" and
                 (parts.path == "/works" or parts.path.startswith("/works/")))))


class Discovery:
    def __init__(self, plan: dict, out_dir: Path, *, client: Any | None = None,
                 replay_dir: Path | None = None) -> None:
        self.plan, self.out_dir, self.client = validate_plan(plan), out_dir, client
        self.budgets = Budgets(**plan["budgets"])
        self.budgets.validate()
        self.requests: list[dict] = []
        self.coverage: list[dict] = []
        self.errors: list[dict] = []
        self.truncations: list[dict] = []
        self.candidates: list[dict] = []
        self.unresolved: list[dict] = []
        self.edges: list[dict] = []
        self.identity_redirects: dict[str, str] = {}
        self.observations = self.pages = 0
        self.replay: dict[str, bytes] | None = None
        if replay_dir is not None:
            if replay_dir.is_symlink():
                raise ValueError("replay directory cannot be a symlink")
            old_plan = _read_json(replay_dir / "plan.json")
            if old_plan != plan:
                raise ValueError("replay requires exactly the original plan and budgets")
            index = _read_json(replay_dir / "responses.json")
            if not isinstance(index, list) or len(index) > 500:
                raise ValueError("invalid replay index")
            self.replay = {}
            for row in index:
                if not isinstance(row, dict) or not _safe_endpoint(row.get("url", "")):
                    raise ValueError("invalid replay endpoint")
                if row.get("status") != "received":
                    continue
                filename = row.get("file", "")
                if not re.fullmatch(r"response_\d{4}\.json", filename):
                    raise ValueError("invalid replay snapshot path")
                path = replay_dir / filename
                if path.is_symlink():
                    raise ValueError("replay snapshot cannot be a symlink")
                with path.open("rb") as stream:
                    body = stream.read(MAX_JSON + 1)
                if len(body) > MAX_JSON or hashlib.sha256(body).hexdigest() != row.get("sha256"):
                    raise ValueError("replay snapshot digest/size mismatch")
                if row["url"] in self.replay:
                    raise ValueError("duplicate replay URL")
                self.replay[row["url"]] = body

    def _stop(self, reason: str, route: dict) -> None:
        self.truncations.append({"reason": reason, "route": route})
        raise BudgetStop(reason)

    def _get(self, url: str, route: dict) -> tuple[dict, int]:
        if len(self.requests) >= self.budgets.requests:
            self._stop("request_budget", route)
        if not _safe_endpoint(url):
            raise ValueError("endpoint is not a fixed trusted API")
        number = len(self.requests) + 1
        entry = {"request_id": number, "url": url, "route": route, "status": "failed"}
        self.requests.append(entry)
        try:
            if self.replay is not None:
                if url not in self.replay:
                    raise ValueError("requested response absent from replay")
                body = self.replay[url]
            elif self.client is not None:
                response = self.client.request(url, max_bytes=MAX_JSON, accept="application/json")
                if not 200 <= response.status < 300:
                    raise ValueError("non-success API status")
                body = response.body
            else:
                raise ValueError("network execution requires an explicit client")
            if len(body) > MAX_JSON:
                raise ValueError("response exceeds 4 MiB")
            name = f"response_{number:04d}.json"
            with (self.out_dir / name).open("xb") as stream:
                stream.write(body)
            entry.update(status="received", file=name, sha256=hashlib.sha256(body).hexdigest(), bytes=len(body))
            value = _json(body)
            if not isinstance(value, dict) or value.get("error"):
                raise ValueError("API response is not a success object")
            return value, number
        except Exception as exc:
            # No exception text: third-party errors can include tokens or private URLs.
            self.errors.append({"request_id": number, "route": route, "error_type": type(exc).__name__,
                                "reason": "request_or_response_failure"})
            raise

    def _page(self, data: dict, kind: str, offset: int, expected_total: int | None) -> tuple[list, int]:
        total = data.get("hitCount")
        if type(total) is not int or total < 0:
            raise ValueError("missing/noninteger hitCount")
        if expected_total is not None and total != expected_total:
            raise ValueError("hitCount changed during pagination")
        container, field = {"search": ("resultList", "result"),
                            "references": ("referenceList", "reference"),
                            "citations": ("citationList", "citation")}[kind]
        wrapper = data.get(container)
        # APIs can omit the list at zero; a declared positive count cannot.
        rows = [] if total == 0 and wrapper is None else wrapper.get(field) if isinstance(wrapper, dict) else None
        if rows is None and total == 0 and wrapper == {}:
            rows = []
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError("invalid result list")
        if len(rows) != min(self.budgets.page_size, max(0, total - offset)):
            raise ValueError("page count is inconsistent with hitCount")
        if kind == "search" and any(not _epmc_id(row.get("source"), row.get("id"))
                                     or not isinstance(row.get("title"), str) or not row["title"].strip()
                                     for row in rows):
            raise ValueError("invalid indexed article metadata")
        return rows, total

    def _identity(self, item: dict) -> list[str]:
        result = []
        epmc = _epmc_id(item.get("source"), item.get("id"))
        if epmc:
            result.append(f"epmc:{epmc[0]}:{epmc[1]}")
        doi = _doi(item.get("doi") or item.get("DOI"))
        if doi:
            result.append(f"doi:{doi}")
        return result

    def _canonical(self, candidate_id: str | None) -> str | None:
        while candidate_id in self.identity_redirects:
            candidate_id = self.identity_redirects[candidate_id]
        return candidate_id

    def _add(self, item: dict, route: dict, request_id: int | None) -> tuple[str | None, bool]:
        if self.observations >= self.budgets.records:
            self._stop("record_budget", route)
        self.observations += 1
        ids = self._identity(item)
        observation = {"payload": item, "route": route, "request_id": request_id}
        if not ids:
            self.unresolved.append(observation)
            return None, False
        matches = [row for row in self.candidates if set(row["identifiers"]) & set(ids)]
        fresh = not matches
        if fresh:
            candidate = {"candidate_id": ids[0], "identifiers": [], "observations": [],
                         "status": "unreviewed", "approval": None}
            self.candidates.append(candidate)
        else:
            candidate = matches[0]
            for duplicate in matches[1:]:
                self.identity_redirects[duplicate["candidate_id"]] = candidate["candidate_id"]
                candidate["identifiers"].extend(x for x in duplicate["identifiers"] if x not in candidate["identifiers"])
                candidate["observations"].extend(duplicate["observations"])
                for edge in self.edges:
                    for field in ("from", "to"):
                        if edge[field] == duplicate["candidate_id"]:
                            edge[field] = candidate["candidate_id"]
                self.candidates.remove(duplicate)
        candidate["identifiers"].extend(x for x in ids if x not in candidate["identifiers"])
        candidate["observations"].append(observation)
        # Nothing chooses a 'winning' title/year/DOI. Disagreement remains inspectable.
        candidate["conflicts"] = {}
        for field in ("title", "pubYear", "year", "doi", "DOI", "source", "id"):
            vals = []
            for obs in candidate["observations"]:
                value = obs["payload"].get(field)
                if value is not None and value not in vals:
                    vals.append(value)
            if len(vals) > 1:
                candidate["conflicts"][field] = vals
        return candidate["candidate_id"], fresh

    def _pages(self, kind: str, route: dict, *, query: str | None = None,
               epmc: tuple[str, str] | None = None):
        cursor, offset, total, page = "*", 0, None, 1
        seen_cursors: set[str] = set()
        seen_pages: set[str] = set()
        seen_identifiers: set[str] = set()
        while True:
            if self.pages >= self.budgets.pages:
                self._stop("page_budget", route)
            self.pages += 1
            if kind == "search":
                url = EPMC + "/search?" + urlencode({"query": query, "format": "json", "resultType": "core",
                       "pageSize": self.budgets.page_size, "cursorMark": cursor, "synonym": "true"})
            else:
                assert epmc is not None
                url = EPMC + f"/{epmc[0]}/{epmc[1]}/{kind}?" + urlencode(
                    {"format": "json", "pageSize": self.budgets.page_size, "page": page})
            data, request_id = self._get(url, {**route, "page": page})
            try:
                rows, count = self._page(data, kind, offset, total)
                total = count
                digest = hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()
                if rows and digest in seen_pages:
                    raise ValueError("repeated page payload")
                page_ids = []
                for row in rows:
                    ids = self._identity(row)
                    # A source/id identifies an API row; DOI-only bibliography
                    # entries are monitored when an index ID is unavailable.
                    if ids:
                        if ids[0] in seen_identifiers or ids[0] in page_ids:
                            raise ValueError("repeated indexed identity during pagination")
                        page_ids.append(ids[0])
                seen_identifiers.update(page_ids)
                seen_pages.add(digest)
                entry = {"route": route, "request_id": request_id, "page": page,
                         "reported_total": total, "returned": len(rows), "offset": offset,
                         "status": "page_validated"}
                self.coverage.append(entry)
                for item in rows:
                    yield item, request_id
                offset += len(rows)
                if offset >= total:
                    entry["status"] = "api_reported_end"
                    return
                if kind == "search":
                    nxt = data.get("nextCursorMark")
                    if not isinstance(nxt, str) or not nxt or len(nxt) > 4096 or nxt == cursor or nxt in seen_cursors:
                        raise ValueError("missing or cyclic pagination cursor")
                    seen_cursors.add(cursor)
                    cursor = nxt
                page += 1
            except BudgetStop:
                raise
            except (ValueError, TypeError) as exc:
                self.errors.append({"request_id": request_id, "route": route,
                                    "error_type": type(exc).__name__, "reason": "malformed_or_repeated_page"})
                raise

    def _crossref(self, doi: str, parent: str, depth: int) -> list[tuple[dict, str]]:
        route = {"kind": "references", "provider": "crossref", "parent": parent, "depth": depth}
        if self.pages >= self.budgets.pages:
            self._stop("page_budget", route)
        self.pages += 1
        data, rid = self._get(CROSSREF + quote(doi, safe=""), route)
        message = data.get("message")
        if data.get("status") != "ok" or not isinstance(message, dict) or _doi(message.get("DOI")) != doi:
            raise ValueError("Crossref response identity mismatch")
        rows = message.get("reference")
        if rows is None:
            self.coverage.append({"route": route, "request_id": rid, "status": "reference_metadata_absent"})
            return []
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError("invalid Crossref reference list")
        reported = message.get("reference-count")
        if reported is not None and (type(reported) is not int or reported < len(rows)):
            raise ValueError("invalid Crossref reference-count")
        if reported is not None and reported > len(rows):
            self.truncations.append({"reason": "crossref_deposit_subset", "route": route})
        frontier = []
        for row in rows:
            child, _ = self._add(row, route, rid)
            if child:
                self.edges.append({"from": parent, "to": child, "kind": "references",
                                   "provider": "crossref", "request_id": rid, "depth": depth})
                frontier.append(({"doi": _doi(row.get("DOI"))}, child))
        self.coverage.append({"route": route, "request_id": rid, "status": "deposited_references_read",
                              "returned": len(rows), "reported_total": reported})
        return frontier

    def _crossref_search_pages(self, query: str, route: dict):
        cursor, offset, total, page = "*", 0, None, 1
        seen_cursors: set[str] = set()
        seen_dois: set[str] = set()
        while True:
            if self.pages >= self.budgets.pages:
                self._stop("page_budget", route)
            self.pages += 1
            url = CROSSREF.rstrip("/") + "?" + urlencode(
                {"query": query, "rows": self.budgets.page_size, "cursor": cursor})
            data, rid = self._get(url, {**route, "page": page})
            try:
                message = data.get("message")
                if data.get("status") != "ok" or not isinstance(message, dict):
                    raise ValueError("Crossref search response is not a success message")
                count, rows = message.get("total-results"), message.get("items")
                if (type(count) is not int or count < 0 or (total is not None and total != count)
                        or not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows)
                        or len(rows) != min(self.budgets.page_size, max(0, count - offset))):
                    raise ValueError("Crossref total/items/page count is invalid")
                total = count
                # Crossref normally echoes query.search-terms. Validate it if present;
                # an omitted optional echo cannot establish a different query.
                echoed = message.get("query")
                if echoed is not None:
                    if not isinstance(echoed, dict):
                        raise ValueError("Crossref query echo is invalid")
                    if "search-terms" in echoed and echoed["search-terms"] != query:
                        raise ValueError("Crossref returned another query")
                page_dois = []
                for row in rows:
                    doi, titles = _doi(row.get("DOI")), row.get("title")
                    if (not doi or not isinstance(titles, list) or not titles
                            or any(not isinstance(title, str) or not title.strip() for title in titles)):
                        raise ValueError("Crossref indexed work metadata is invalid")
                    if doi in seen_dois or doi in page_dois:
                        raise ValueError("Crossref repeated DOI during a query")
                    page_dois.append(doi)
                seen_dois.update(page_dois)
                entry = {"route": route, "request_id": rid, "page": page, "reported_total": total,
                         "returned": len(rows), "offset": offset, "status": "page_validated"}
                self.coverage.append(entry)
                for row in rows:
                    yield row, rid
                offset += len(rows)
                if offset >= total:
                    entry["status"] = "api_reported_end"
                    return
                nxt = message.get("next-cursor")
                if not isinstance(nxt, str) or not nxt or len(nxt) > 4096 or nxt == cursor or nxt in seen_cursors:
                    raise ValueError("Crossref missing or cyclic cursor")
                seen_cursors.add(cursor)
                cursor = nxt
                page += 1
            except BudgetStop:
                raise
            except (ValueError, TypeError) as exc:
                self.errors.append({"request_id": rid, "route": route, "error_type": type(exc).__name__,
                                    "reason": "malformed_or_repeated_page"})
                raise

    def run(self) -> dict:
        queue = deque()
        visited = set()
        stopped = False
        for query in self.plan["queries"]:
            route = {"kind": "query", "provider": "europepmc", "query": query}
            try:
                for item, rid in self._pages("search", route, query=query):
                    self._add(item, route, rid)
            except BudgetStop:
                stopped = True
                break
            except Exception:
                continue
        if not stopped:
            for query in self.plan["crossref_queries"]:
                route = {"kind": "query", "provider": "crossref", "query": query}
                try:
                    for item, rid in self._crossref_search_pages(query, route):
                        self._add(item, route, rid)
                except BudgetStop:
                    stopped = True
                    break
                except Exception:
                    continue
        if not stopped:
            for seed in self.plan["seeds"]:
                try:
                    cid, _ = self._add(seed, {"kind": "seed"}, None)
                    queue.append((seed, cid, 0))
                except BudgetStop:
                    stopped = True
                    break
        while queue and not stopped:
            seed, parent, depth = queue.popleft()
            parent = self._canonical(parent)
            key = json.dumps(seed, sort_keys=True)
            if key in visited:
                continue
            visited.add(key)
            if depth >= self.budgets.depth:
                self.truncations.append({"reason": "depth_budget", "route": {"seed": seed, "depth": depth}})
                continue
            epmc = _epmc_id(seed.get("source"), seed.get("id"))
            try:
                if not epmc:
                    doi = seed["doi"]
                    route = {"kind": "resolve_doi", "provider": "europepmc", "doi": doi}
                    resolved = []
                    for row, rid in self._pages("search", route, query=f'DOI:"{doi}"'):
                        # Do not allow a search service to silently substitute another DOI.
                        if _doi(row.get("doi")) != doi:
                            raise ValueError("DOI resolution identity mismatch")
                        self._add(row, route, rid)
                        ident = _epmc_id(row.get("source"), row.get("id"))
                        if ident and ident not in resolved:
                            resolved.append(ident)
                    if resolved:
                        epmc = resolved[0]
                        for other in resolved[1:]:
                            queue.append(({"source": other[0], "id": other[1]}, parent, depth))
                    else:
                        for child, child_id in self._crossref(doi, parent, depth + 1):
                            queue.append((child, child_id, depth + 1))
                        self.coverage.append({"route": route, "status": "forward_citations_unavailable"})
                        continue
                epmc_key = f"epmc:{epmc[0]}:{epmc[1]}"
                if epmc_key in visited:
                    continue
                visited.add(epmc_key)
                for kind in ("references", "citations"):
                    route = {"kind": kind, "provider": "europepmc", "parent": parent, "depth": depth + 1}
                    try:
                        for row, rid in self._pages(kind, route, epmc=epmc):
                            child, _ = self._add(row, route, rid)
                            if child:
                                self.edges.append({"from": parent if kind == "references" else child,
                                    "to": child if kind == "references" else parent, "kind": "references",
                                    "discovery_direction": kind, "provider": "europepmc", "request_id": rid,
                                    "depth": depth + 1})
                                ident = _epmc_id(row.get("source"), row.get("id"))
                                child_seed = {"source": ident[0], "id": ident[1]} if ident else {"doi": _doi(row.get("doi"))}
                                queue.append((child_seed, child, depth + 1))
                    except BudgetStop:
                        raise
                    except Exception:
                        continue
            except BudgetStop:
                stopped = True
            except Exception as exc:
                self.errors.append({"route": {"kind": "citation_expansion", "seed": seed},
                                    "error_type": type(exc).__name__, "reason": "expansion_failure"})
        # Bridge observations can unify two stable IDs while their work is queued.
        # Resolve every graph/provenance parent only after all unions have settled.
        for edge in self.edges:
            edge["from"], edge["to"] = self._canonical(edge["from"]), self._canonical(edge["to"])
        for candidate in self.candidates:
            for observation in candidate["observations"]:
                route = observation["route"]
                if "parent" in route:
                    route["parent"] = self._canonical(route["parent"])
        for row in self.unresolved + self.coverage + self.errors + self.truncations + self.requests:
            if "parent" in row.get("route", {}):
                row["route"]["parent"] = self._canonical(row["route"]["parent"])
        return {"schema_version": 1, "topic_fingerprint": self.plan["topic_fingerprint"],
                "status": "partial" if self.errors or self.truncations else "bounded_retrieval_finished",
                "candidate_status": "unreviewed", "approval": None, "exhaustive_recall": False,
                "counts": {"requests": len(self.requests), "pages": self.pages, "observations": self.observations,
                           "candidates": len(self.candidates), "unresolved": len(self.unresolved), "edges": len(self.edges)},
                "candidates": self.candidates, "unresolved_references": self.unresolved, "edges": self.edges,
                "coverage": self.coverage, "errors": self.errors, "truncations": self.truncations,
                "identity_redirects": self.identity_redirects}


def _write_json(path: Path, value: Any) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        stream.write("\n")


def execute(plan: dict, out_dir: Path, *, online: bool = False, client: Any | None = None,
            replay_dir: Path | None = None) -> dict:
    """Output directory must be absent or empty; create no changes in input areas."""
    if online and replay_dir is not None:
        raise ValueError("online and replay are mutually exclusive")
    if out_dir.is_symlink() or any(parent.is_symlink() for parent in out_dir.parents):
        raise ValueError("output path cannot traverse symlinks")
    if out_dir.exists() and (not out_dir.is_dir() or any(out_dir.iterdir())):
        raise ValueError("output directory must be fresh and empty")
    if replay_dir is not None and out_dir.resolve() == replay_dir.resolve():
        raise ValueError("replay input and output must be separate")
    runner = Discovery(plan, out_dir, client=client if online else None, replay_dir=replay_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_json(out_dir / "plan.json", plan)
    if not online and replay_dir is None:
        report = {"schema_version": 1, "status": "offline_plan", "topic_fingerprint": plan["topic_fingerprint"],
                  "network_requests": 0, "candidate_status": "unreviewed", "approval": None,
                  "exhaustive_recall": False}
    else:
        report = runner.run()
    _write_json(out_dir / "discovery.json", report)
    _write_json(out_dir / "responses.json", runner.requests)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Plan or explicitly execute bounded literature recovery; outputs remain unreviewed")
    parser.add_argument("--topic", default="pantothenate")
    parser.add_argument("--source", choices=("europepmc", "crossref"), action="append",
                        help="keyword provider; repeat for both, default europepmc")
    parser.add_argument("--alias", action="append", default=[])
    parser.add_argument("--aliases-json", type=Path, help="strict JSON array of literal phrases")
    parser.add_argument("--seeds-json", type=Path, help="array of {source,id} or {doi}")
    parser.add_argument("--out-dir", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--online", action="store_true")
    mode.add_argument("--replay", type=Path, help="replay prior plan and response snapshots without network")
    parser.add_argument("--email", default="", help="API contact passed only in User-Agent")
    parser.add_argument("--max-requests", type=int, default=50)
    parser.add_argument("--max-pages", type=int, default=30)
    parser.add_argument("--max-records", type=int, default=1000)
    parser.add_argument("--max-depth", type=int, default=1)
    parser.add_argument("--page-size", type=int, default=100)
    args = parser.parse_args(argv)
    try:
        if args.replay:
            plan = _read_json(args.replay / "plan.json")
            validate_plan(plan)
            # Replay uses the saved protocol exactly, not silently changed defaults.
        else:
            aliases = validate_aliases(_read_json(args.aliases_json)) if args.aliases_json else []
            seeds = validate_seeds(_read_json(args.seeds_json)) if args.seeds_json else []
            plan = build_plan(load_discovery_topic(args.topic), aliases + args.alias, seeds,
                             Budgets(args.max_requests, args.max_pages, args.max_records, args.max_depth, args.page_size),
                             sources=args.source)
        client = HttpClient(email=args.email, retries=0) if args.online else None
        report = execute(plan, args.out_dir, online=args.online, client=client, replay_dir=args.replay)
        print(json.dumps({"status": report["status"], "candidate_status": "unreviewed"}))
        return 2 if report["status"] == "partial" else 0
    except (ValueError, OSError, KeyError, TypeError):
        print("discovery failed: invalid input, replay, or output directory; existing files were not overwritten", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
