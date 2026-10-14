from __future__ import annotations

import json
import socket
import tempfile
import unittest
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from litsearch import sources
from litsearch.cli import _validate_args, build_parser, run
from litsearch.models import AuditEvent
from litsearch.net import Response


def oa_item(title: str = "A valid title", number: int = 1) -> dict:
    return {"display_name": title, "id": f"https://openalex.org/W{number}"}


class OpenAlexClient:
    def __init__(self, payloads):
        self.payloads = iter(payloads if isinstance(payloads, list) else [payloads])
        self.urls: list[str] = []

    def get_json(self, url: str, timeout: int = 40):
        self.urls.append(url)
        return next(self.payloads)


class PubMedClient:
    def __init__(self, ids, xmls):
        self.ids = [str(x) for x in ids]
        self.xmls = list(xmls if isinstance(xmls, list) else [xmls])
        self.posts: list[bytes] = []

    def get_json(self, url: str, timeout: int = 40):
        return {"esearchresult": {"count": str(len(self.ids)), "idlist": self.ids}}

    def post_bytes(self, url: str, data: bytes, **kwargs):
        self.posts.append(data)
        return Response(self.xmls[min(len(self.posts) - 1, len(self.xmls) - 1)], "application/xml", url, 200)


def article_xml(items: list[tuple[str | None, str | None]]) -> bytes:
    root = ET.Element("PubmedArticleSet")
    for pmid, title in items:
        article = ET.SubElement(root, "PubmedArticle")
        med = ET.SubElement(article, "MedlineCitation")
        if pmid is not None:
            ET.SubElement(med, "PMID").text = pmid
        art = ET.SubElement(med, "Article")
        if title is not None:
            ET.SubElement(art, "ArticleTitle").text = title
        journal = ET.SubElement(art, "Journal")
        ET.SubElement(journal, "Title").text = "Test Journal"
        ET.SubElement(art, "Abstract")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


class OpenAlexPublicationMatrixTests(unittest.TestCase):
    def invoke(self, path: str, payloads, per_query: int = 1):
        client = OpenAlexClient(payloads)
        events: list[AuditEvent] = []
        batches = []
        with patch.object(sources, "TITLE_QUERIES", ("title-case",) if path == "title" else ()), \
             patch.object(sources, "SEARCH_QUERIES", ("search-case",) if path == "search" else ()):
            try:
                sources.fetch_openalex(per_query, client=client, api_key="KEY_SENTINEL",
                                       email="mail-sentinel@example.org", events=events,
                                       on_batch=batches.append, sleeper=lambda _: None)
            except ValueError as exc:
                error = exc
            else:
                self.fail("malformed OpenAlex response unexpectedly succeeded")
        serialized_events = json.dumps([asdict(event) for event in events], ensure_ascii=False, sort_keys=True)
        return client, events, batches, error, serialized_events

    def test_each_path_rejects_complete_malformed_matrix_without_checkpoint(self):
        malformed = [
            None,
            {"error": "KEY_SENTINEL mail-sentinel@example.org"}, {}, {"meta": {"count": 0}},
            {"results": []}, {"results": {}, "meta": {"count": 0}},
            {"results": [], "meta": {}}, {"results": [], "meta": {"count": -1}},
            {"results": [], "meta": {"count": True}}, {"results": [], "meta": {"count": "1"}},
            {"results": [], "meta": {"count": 1.5}},
            {"results": [oa_item(), oa_item("second", 2)], "meta": {"count": 1}},
            {"results": [oa_item()], "meta": {"count": 2}},
            {"results": ["row"], "meta": {"count": 1}},
            {"results": [{"display_name": "   "}], "meta": {"count": 1}},
            {"results": [{"display_name": 7}], "meta": {"count": 1}},
            {"results": [{"display_name": "ok", "primary_location": "bad"}], "meta": {"count": 1}},
            {"results": [{"display_name": "ok", "primary_location": {"source": "bad"}}], "meta": {"count": 1}},
            {"results": [{"display_name": "ok", "authorships": [{"author": "bad"}]}], "meta": {"count": 1}},
            {"results": [{"display_name": "ok", "abstract_inverted_index": {"word": [None]}}], "meta": {"count": 1}},
            {"results": [{"display_name": "ok", "abstract_inverted_index": {"word": None}}], "meta": {"count": 1}},
            {"results": [{"id": "https://openalex.org/W1"}], "meta": {"count": 1}},
            {"results": [oa_item(), {"display_name": "bad", "primary_location": "bad"}], "meta": {"count": 2}},
        ]
        for path in ("title", "search"):
            for payload in malformed:
                with self.subTest(path=path, payload=payload):
                    # A budget of two lets every two-row case reach row validation;
                    # count=2 with one returned row still exercises the incomplete-page check.
                    client, events, batches, error, serialized_events = self.invoke(path, payload, 2)
                    self.assertEqual(len(batches), 0)
                    self.assertEqual(events[-1].source, "openalex")
                    self.assertEqual(events[-1].status, "failed")
                    self.assertFalse(any(e.status in {"success", "truncated"} for e in events))
                    self.assertNotIn("KEY_SENTINEL", str(error))
                    self.assertNotIn("mail-sentinel@example.org", str(error))
                    self.assertNotIn("KEY_SENTINEL", serialized_events)
                    self.assertNotIn("mail-sentinel@example.org", serialized_events)
                    self.assertEqual(len(client.urls), 1)
                    params = urllib.parse.parse_qs(urllib.parse.urlsplit(client.urls[0]).query)
                    self.assertIn("filter" if path == "title" else "search", params)
                    self.assertEqual(params["api_key"], ["KEY_SENTINEL"])
                    self.assertEqual(params["mailto"], ["mail-sentinel@example.org"])
                    other_path_param = "search" if path == "title" else "filter"
                    self.assertNotIn(other_path_param, params)
                    if isinstance(payload, dict) and payload.get("meta", {}).get("count") == 2 and len(payload.get("results", [])) == 2:
                        self.assertEqual(str(error), "OpenAlex response contains an invalid nested object")

    def test_each_path_has_zero_one_truncated_callback_and_exact_requests(self):
        for path in ("title", "search"):
            for payload, expected_count, expected_status, expected_titles in (
                ({"results": [], "meta": {"count": 0}}, 0, "success", []),
                ({"results": [oa_item("one")], "meta": {"count": 1}}, 1, "success", ["one"]),
                ({"results": [oa_item("one"), oa_item("two", 2)], "meta": {"count": 3}}, 2, "truncated", ["one", "two"]),
            ):
                client = OpenAlexClient(payload); events = []; batches = []
                with self.subTest(path=path, count=expected_count), \
                     patch.object(sources, "TITLE_QUERIES", ("title-case",) if path == "title" else ()), \
                     patch.object(sources, "SEARCH_QUERIES", ("search-case",) if path == "search" else ()):
                    records = sources.fetch_openalex(2, client=client, api_key="key", email="a@b.org",
                                                      events=events, on_batch=batches.append, sleeper=lambda _: None)
                self.assertEqual([r.title for r in records], expected_titles)
                self.assertEqual([[r.title for r in b] for b in batches], [expected_titles] if expected_titles else [[]])
                self.assertEqual(events[-1].status, expected_status)
                self.assertEqual(len(client.urls), 1)

    def test_successful_first_path_is_checkpointed_before_second_path_failure(self):
        client = OpenAlexClient([{"results": [oa_item("first")], "meta": {"count": 1}}, {"results": {}, "meta": {"count": 0}}])
        events = []; batches = []
        with patch.object(sources, "TITLE_QUERIES", ("title-case",)), patch.object(sources, "SEARCH_QUERIES", ("search-case",)):
            with self.assertRaises(ValueError):
                sources.fetch_openalex(1, client=client, api_key="key", email="a@b.org", events=events,
                                       on_batch=batches.append, sleeper=lambda _: None)
        self.assertEqual([[r.title for r in b] for b in batches], [["first"]])
        self.assertEqual([e.status for e in events], ["success", "failed"])

    def test_callback_typeerror_propagates_unchanged(self):
        payload = {"results": [oa_item()], "meta": {"count": 1}}
        with patch.object(sources, "TITLE_QUERIES", ("title-case",)), patch.object(sources, "SEARCH_QUERIES", ()):
            with self.assertRaisesRegex(TypeError, "callback sentinel"):
                sources.fetch_openalex(1, client=OpenAlexClient(payload), api_key="key", email="a@b.org",
                                       on_batch=lambda _: (_ for _ in ()).throw(TypeError("callback sentinel")), sleeper=lambda _: None)


class PubMedPublicationMatrixTests(unittest.TestCase):
    def run_case(self, requested, returned, *, on_batch=True):
        client = PubMedClient(requested, article_xml(returned)); events = []; saved = []
        kwargs = {"client": client, "email": "a@b.org", "events": events, "sleeper": lambda _: None}
        if on_batch: kwargs["on_batch"] = saved.extend
        try:
            result = sources.fetch_pubmed(len(requested), **kwargs)
            error = None
        except ValueError as exc:
            result = None; error = exc
        coverage = [(event, json.loads(event.message)) for event in events if event.stage == "efetch_coverage"]
        return result, error, saved, events, coverage

    def test_exact_single_batch_diagnostics_and_saved_ids(self):
        cases = [
            (["1"], [("1", "one")], ["1"], ["1"], "success", ["1"], [], [], []),
            (["1", "2"], [("1", "one")], ["1"], [], "failed", ["1"], ["2"], [], []),
            (["1", "2"], [("1", "one"), ("1", "one")], ["1"], [], "failed", ["1", "1"], ["2"], [], ["1"]),
            (["1", "2"], [("1", "one"), ("3", "three")], ["1", "3"], [], "failed", ["1", "3"], ["2"], ["3"], []),
        ]
        for requested, returned, saved_ids, result_ids, status, received, missing, unexpected, duplicate in cases:
            with self.subTest(requested=requested, returned=returned):
                result, error, saved, events, coverage = self.run_case(requested, returned)
                self.assertEqual([r.pmid for r in saved], saved_ids)
                self.assertEqual([r.pmid for r in result or []], result_ids)
                self.assertEqual(len(coverage), 1)
                event, diagnostic = coverage[0]
                self.assertEqual((event.source, event.status), ("pubmed", status))
                self.assertEqual(diagnostic, {"requested": requested, "received": received,
                                              "missing": missing, "unexpected": unexpected, "duplicate": duplicate})
                if status == "success":
                    self.assertIsNone(error)
                else:
                    self.assertIsInstance(error, ValueError)

    def test_invalid_articles_are_excluded_from_received_diagnostic(self):
        invalid_cases = (
            ("empty PMID", [("1", "one"), ("", "bad")]),
            ("missing PMID element", [("1", "one"), (None, "bad")]),
            ("empty title", [("1", "one"), ("2", "")]),
            ("missing title", [("1", "one"), ("2", None)]),
        )
        expected = {"requested": ["1", "2"], "received": ["1"], "missing": ["2"],
                    "unexpected": [], "duplicate": []}
        for label, returned in invalid_cases:
            with self.subTest(invalid_article=label):
                result, error, saved, _events, coverage = self.run_case(["1", "2"], returned)
                self.assertIsNone(result)
                self.assertIsInstance(error, ValueError)
                self.assertEqual([r.pmid for r in saved], ["1"])
                self.assertEqual(len(coverage), 1)
                event, diagnostic = coverage[0]
                self.assertEqual((event.source, event.status), ("pubmed", "failed"))
                self.assertEqual(diagnostic, expected)

    def test_cross_batch_and_cross_query_deduplicate_once(self):
        requested = [str(i) for i in range(1, 103)]
        xmls = [article_xml([(str(i), f"title {i}") for i in range(1, 101)]), article_xml([("101", "title 101")])]
        client = PubMedClient(requested, xmls); events = []; saved = []
        with self.assertRaises(ValueError):
            sources.fetch_pubmed(102, client=client, email="a@b.org", events=events, on_batch=saved.extend, sleeper=lambda _: None)
        self.assertEqual([r.pmid for r in saved], [str(i) for i in range(1, 102)])
        coverage = [(event, json.loads(event.message)) for event in events if event.stage == "efetch_coverage"]
        self.assertEqual([(event.source, event.status) for event, _ in coverage],
                         [("pubmed", "success"), ("pubmed", "failed")])
        self.assertEqual([diagnostic for _, diagnostic in coverage], [
            {"requested": [str(i) for i in range(1, 101)], "received": [str(i) for i in range(1, 101)],
             "missing": [], "unexpected": [], "duplicate": []},
            {"requested": ["101", "102"], "received": ["101"], "missing": ["102"],
             "unexpected": [], "duplicate": []},
        ])

        client = PubMedClient(["1"], article_xml([("1", "one")]))
        with patch.object(sources, "PUBMED_QUERIES", ("q1", "q2")):
            events = []; saved = []; result = sources.fetch_pubmed(1, client=client, email="a@b.org", events=events, on_batch=saved.extend, sleeper=lambda _: None)
        self.assertEqual([r.pmid for r in result], ["1"]); self.assertEqual([r.pmid for r in saved], ["1"])
        coverage = [(event, json.loads(event.message)) for event in events if event.stage == "efetch_coverage"]
        self.assertEqual([(event.source, event.status) for event, _ in coverage],
                         [("pubmed", "success"), ("pubmed", "success")])
        self.assertEqual([diagnostic for _, diagnostic in coverage], [
            {"requested": ["1"], "received": ["1"], "missing": [], "unexpected": [], "duplicate": []},
            {"requested": ["1"], "received": ["1"], "missing": [], "unexpected": [], "duplicate": []},
        ])


class RealFetchCliIntegrationTests(unittest.TestCase):
    def args(self, root: Path):
        parser = build_parser(); args = parser.parse_args(["--out-dir", str(root / "output"), "--cache-dir", str(root / "cache"),
            "--pdf-dir", str(root / "pdfs"), "--skip-pdf-extraction", "--source", "pubmed", "--email", "a@b.org", "--pubmed-max", "2"])
        _validate_args(parser, args); return args

    def test_cli_uses_real_fetch_and_persists_exact_failure_audit(self):
        def get_json(_self, url, timeout=40):
            return {"esearchresult": {"count": "2", "idlist": ["1", "2"]}}
        def post_bytes(_self, url, data, **kwargs):
            return Response(article_xml([("1", "one")]), "application/xml", url, 200)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("litsearch.net.HttpClient.get_json", get_json), patch("litsearch.net.HttpClient.post_bytes", post_bytes), \
                 patch("socket.socket", side_effect=AssertionError("socket must remain offline")), self.assertRaises(RuntimeError):
                run(self.args(root))
            raw = json.loads((root / "output/raw_records.json").read_text(encoding="utf-8"))
            self.assertEqual([row["pmid"] for row in raw], ["1"])
            events = [json.loads(line) for line in (root / "output/events.jsonl").read_text(encoding="utf-8").splitlines()]
            coverage = next(event for event in events if event["stage"] == "efetch_coverage" and event["source"] == "pubmed")
            self.assertEqual(coverage["status"], "failed")
            self.assertEqual(json.loads(coverage["message"]), {"requested": ["1", "2"], "received": ["1"], "missing": ["2"], "unexpected": [], "duplicate": []})
            manifest = json.loads((root / "output/run_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "failed"); self.assertEqual(manifest["counts"]["raw_before_failure"], 1)


if __name__ == "__main__":
    unittest.main()
