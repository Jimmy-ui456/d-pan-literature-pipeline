"""Offline API adversaries only; never contact scholarly services."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from xml.sax.saxutils import escape

from litsearch.net import NetworkError, Response
from litsearch.service_probe import ContractError, SnapshotClient, build_parser, main, run_probe


DOI = "10.1234/synthetic-probe"
TITLE = "Synthetic demonstration of scholarly metadata adapter verification"


class Backend:
    def __init__(self, count: int = 3, fault: str = "") -> None:
        self.count, self.fault, self.calls = count, fault, []

    def epmc_item(self, number: int) -> dict:
        item = {"title": TITLE if number == 1 else f"Synthetic result {number}", "id": str(number),
                "source": "MED", "doi": DOI if number == 1 else f"10.1234/synthetic-{number}", "pubYear": "2020"}
        if self.fault in {"oa", "oa_fallback"}:
            item["fullTextUrlList"] = {"fullTextUrl": [{"documentStyle": "pdf", "availabilityCode": "OA", "url": "https://example.org/probe.pdf"}]}
            if self.fault == "oa_fallback":
                item["fullTextUrlList"]["fullTextUrl"].insert(0, {"documentStyle": "pdf", "availabilityCode": "OA", "url": "https://example.org/wrong.pdf"})
        return item

    def request(self, url: str, *, data: bytes | None = None, **kwargs) -> Response:
        self.calls.append((url, data, kwargs))
        if self.fault == "429_after_first" and len(self.calls) > 1:
            raise NetworkError("Never echo request data", status=429)
        parts = urlsplit(url)
        params = parse_qs(parts.query)
        if self.fault == "secret_error":
            raise NetworkError("Never log " + os.environ.get("OPENALEX_API_KEY", "") + " contact", status=429)
        value = None
        if parts.path.endswith("/search"):
            if "cursorMark" not in params:
                value = {"hitCount": 1, "resultList": {"result": [self.epmc_item(1)]}}
                if self.fault == "swapped_doi":
                    value["resultList"]["result"][0]["doi"] = "10.1234/wrong"
                if self.fault == "not_indexed":
                    value = {"hitCount": 0, "resultList": {"result": []}}
            else:
                offset = 0 if params["cursorMark"][0] == "*" else int(params["cursorMark"][0])
                size = int(params["pageSize"][0])
                numbers = range(offset + 1, min(offset + size, self.count) + 1)
                value = {"hitCount": self.count, "nextCursorMark": str(offset + size),
                         "resultList": {"result": [self.epmc_item(number) for number in numbers]}}
                if self.fault == "epmc_duplicate" and offset:
                    value["resultList"]["result"][0] = self.epmc_item(1)
                if self.fault == "epmc_missing_cursor":
                    value.pop("nextCursorMark")
                if self.fault == "epmc_repeated_cursor":
                    value["nextCursorMark"] = "*"
                if self.fault == "epmc_empty_page":
                    value["resultList"]["result"] = []
                if self.fault == "epmc_malformed_zero":
                    value = {"hitCount": 0}
                if self.fault == "epmc_negative":
                    value["hitCount"] = -1
                if self.fault == "epmc_changed" and offset:
                    value["hitCount"] = self.count + 1
        elif parts.path.endswith("/works") and parts.netloc == "api.crossref.org":
            if "filter" in params:
                value = {"status": "ok", "message": {"total-results": 0, "items": []}}
            else:
                offset = 0 if params["cursor"][0] == "*" else int(params["cursor"][0])
                size = int(params["rows"][0])
                items = [{"DOI": f"10.1234/search-{number}", "title": [f"Synthetic result {number}"]}
                         for number in range(offset + 1, min(offset + size, self.count) + 1)]
                value = {"status": "ok", "message": {"total-results": self.count, "items": items, "next-cursor": str(offset + size)}}
                if self.fault == "crossref_short_page" and items:
                    value["message"]["items"] = items[:-1]
                if self.fault == "crossref_repeated_cursor":
                    value["message"]["next-cursor"] = "*"
        elif "/works/" in parts.path and parts.netloc == "api.crossref.org":
            value = {"status": "ok", "message": {"title": [TITLE], "DOI": DOI, "published": {"date-parts": [[2020]]}}}
            if self.fault == "identity_mismatch":
                value["message"]["title"] = ["Unrelated remote article"]
            if self.fault == "identity_error_envelope":
                value["status"] = "error"
        elif parts.path.endswith("esearch.fcgi"):
            exact = "[DOI]" in params["term"][0]
            total = 1 if exact else self.count
            offset = int(params["retstart"][0])
            size = int(params["retmax"][0])
            value = {"esearchresult": {"count": str(total), "idlist": [str(i) for i in range(offset + 1, min(total, offset + size) + 1)]}}
            if self.fault == "pubmed_zero_malformed":
                value = {"esearchresult": {"idlist": []}}
        elif parts.path.endswith("efetch.fcgi"):
            numbers = parse_qs(data.decode())["id"][0].split(",")
            if self.fault == "pubmed_missing_fetch":
                numbers = numbers[:-1]
            articles = []
            for number in numbers:
                title = TITLE if number == "1" else "Synthetic result " + number
                doi = DOI if number == "1" else "10.1234/synthetic-" + number
                articles.append(f"<PubmedArticle><MedlineCitation><PMID>{number}</PMID><Article><ArticleTitle>{escape(title)}</ArticleTitle><Journal><JournalIssue><PubDate><Year>2020</Year></PubDate></JournalIssue></Journal></Article></MedlineCitation><PubmedData><ArticleIdList><ArticleId IdType='doi'>{doi}</ArticleId></ArticleIdList></PubmedData></PubmedArticle>")
            return Response(("<PubmedArticleSet>" + "".join(articles) + "</PubmedArticleSet>").encode(), "application/xml", url, 200)
        elif parts.netloc == "api.openalex.org":
            if "/works/" in parts.path:
                value = {"id": "https://openalex.org/W1", "doi": "https://doi.org/" + DOI, "display_name": TITLE}
                return Response(json.dumps(value).encode(), "application/json", url, 200)
            offset = 0 if params["cursor"][0] == "*" else int(params["cursor"][0])
            size = int(params["per-page"][0])
            items = [{"id": f"https://openalex.org/W{i}", "display_name": f"Synthetic result {i}"}
                     for i in range(offset + 1, min(self.count, offset + size) + 1)]
            value = {"meta": {"count": self.count, "next_cursor": str(offset + size)}, "results": items}
            if self.fault == "openalex_malformed":
                value["results"] = None
            if self.fault == "openalex_secret_echo":
                value["echo"] = params["api_key"][0]
        elif parts.path.endswith("wrong.pdf"):
            return Response(b"<html>Temporary unavailable</html>", "text/html", url, 200)
        elif parts.path.endswith("probe.pdf"):
            return Response(b"%PDF-1.7\n" + b"x" * 1500, "application/pdf", url, 200)
        else:
            raise AssertionError("Unexpected synthetic URL")
        body = json.dumps(value).encode()
        if self.fault == "oversized":
            body += b" " * (kwargs["max_bytes"] + 1)
        if self.fault == "invalid_json":
            body = b"{"
        return Response(body, "application/json", url, 200)


class ServiceProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def args(self, suffix="probe", *extra):
        return build_parser().parse_args(["--out-dir", str(self.root / suffix), "--online", "--query", "synthetic", "--doi", DOI, "--title", TITLE, "--email", "contact@example.org", *extra])

    def run_case(self, fault="", count=3, *extra):
        backend = Backend(count, fault)
        report = run_probe(self.args("probe", *extra), client=backend, sleeper=lambda _: None)
        return report, backend

    def test_offline_default_never_constructs_network_client(self):
        with patch("litsearch.service_probe.HttpClient", side_effect=AssertionError("network disabled")):
            code = main(["--out-dir", str(self.root / "plan")])
        self.assertEqual(code, 0)
        report = json.loads((self.root / "plan" / "probe_report.json").read_text())
        self.assertEqual(report["status"], "planned")
        self.assertFalse(report["all_services_validated"])
        self.assertEqual(report["plan"]["network_requests"], 0)

    def test_complete_multiple_pages_exercise_production_adapters(self):
        report, backend = self.run_case()
        self.assertEqual(report["status"], "pass")
        searches = [s for s in report["stages"] if s["stage"] in {"europepmc_search", "crossref_search", "pubmed_search"}]
        self.assertTrue(all(s["available"] == s["retrieved"] == 3 for s in searches))
        self.assertEqual(next(s for s in report["stages"] if s["stage"] == "metadata_identity_and_swapped_title")["swapped_title"], "metadata_conflict")
        self.assertFalse(report["all_services_validated"])
        self.assertGreater(len(backend.calls), 8)
        for snapshot in report["snapshots"]:
            body = (self.root / "probe" / snapshot["file"]).read_bytes()
            self.assertEqual(hashlib.sha256(body).hexdigest(), snapshot["saved_sha256"])
            self.assertNotIn("contact@example.org", snapshot["endpoint"])

    def test_zero_is_valid_but_malformed_zero_is_failure(self):
        report, _ = self.run_case(count=0)
        for name in ("europepmc_search", "crossref_search", "pubmed_search"):
            stage = next(s for s in report["stages"] if s["stage"] == name)
            self.assertEqual(stage["status"], "pass")
            self.assertEqual(stage["retrieved"], 0)
        for index, fault in enumerate(("epmc_malformed_zero", "pubmed_zero_malformed")):
            with self.subTest(fault=fault):
                report = run_probe(self.args(str(index)), client=Backend(0, fault), sleeper=lambda _: None)
                self.assertEqual(report["status"], "incomplete_or_failed")

    def test_pagination_adversaries_fail_closed(self):
        for index, fault in enumerate(("epmc_duplicate", "epmc_missing_cursor", "epmc_repeated_cursor", "epmc_empty_page", "epmc_negative", "epmc_changed", "crossref_short_page", "crossref_repeated_cursor", "pubmed_missing_fetch", "invalid_json", "oversized", "identity_mismatch", "identity_error_envelope", "swapped_doi")):
            with self.subTest(fault=fault):
                report = run_probe(self.args(str(index)), client=Backend(3, fault), sleeper=lambda _: None)
                self.assertEqual(report["status"], "incomplete_or_failed")
                self.assertTrue(any(s["status"] == "fail" for s in report["stages"]))

    def test_truncation_is_visible_and_nonzero(self):
        backend = Backend(10)
        args = self.args("truncated", "--max-pages", "1")
        with patch("litsearch.service_probe.HttpClient", return_value=backend), patch("litsearch.service_probe.time.sleep"):
            self.assertEqual(main(["--out-dir", args.out_dir, "--online", "--query", args.query, "--doi", args.doi, "--title", args.title, "--email", args.email, "--max-pages", "1"]), 1)
        report = json.loads((Path(args.out_dir) / "probe_report.json").read_text())
        self.assertEqual(sum(s["status"] == "truncated" for s in report["stages"]), 3)

    def test_credentials_and_exception_text_never_leak(self):
        secret = "unit" + "test" + "credential" + "123456789"
        with patch.dict(os.environ, {"OPENALEX_API_KEY": secret}):
            report, _ = self.run_case("secret_error", 3, "--openalex")
        serialized = json.dumps(report)
        self.assertNotIn(secret, serialized)
        self.assertNotIn("contact@example.org", serialized)
        self.assertTrue(any(s.get("http_status") == 429 for s in report["stages"]))

    def test_429_retains_successful_raw_snapshot_and_reports_failure(self):
        report, _ = self.run_case("429_after_first")
        self.assertEqual(report["status"], "incomplete_or_failed")
        self.assertEqual(len(report["snapshots"]), 1)
        self.assertTrue(any(s.get("http_status") == 429 for s in report["stages"]))
        self.assertTrue((self.root / "probe" / "probe_report.json").is_file())

    def test_interruption_retains_incomplete_checkpoint_not_success(self):
        backend = Backend(count=1)
        original = backend.request
        def interrupted(url, **kwargs):
            if "api.crossref.org/works?" in url:
                raise KeyboardInterrupt()
            return original(url, **kwargs)
        backend.request = interrupted
        with self.assertRaises(KeyboardInterrupt):
            run_probe(self.args(), client=backend, sleeper=lambda _: None)
        report = json.loads((self.root / "probe" / "probe_report.json").read_text())
        self.assertEqual(report["status"], "in_progress_incomplete")
        self.assertEqual(report["stages"][0]["status"], "pass")
        self.assertFalse(report["all_services_validated"])

    def test_missing_expected_identity_is_not_called_service_outage(self):
        report, _ = self.run_case("not_indexed")
        stage = next(s for s in report["stages"] if s["stage"] == "metadata_identity_and_swapped_title")
        self.assertEqual(stage["status"], "fail")
        self.assertEqual(stage["source_checks"]["europepmc"], "not_indexed")
        self.assertNotIn("error_type", stage)

    def test_echoed_key_is_redacted_and_digests_declared(self):
        secret = "unit" + "test" + "echo" + "123456789"
        with patch.dict(os.environ, {"OPENALEX_API_KEY": secret}):
            report, _ = self.run_case("openalex_secret_echo", 3, "--openalex")
        self.assertEqual(report["status"], "pass")
        self.assertTrue(any(s["redacted"] for s in report["snapshots"]))
        for path in (self.root / "probe").iterdir():
            self.assertNotIn(secret.encode(), path.read_bytes())

    def test_explicit_openalex_missing_key_fails(self):
        with patch.dict(os.environ, {"OPENALEX_API_KEY": ""}):
            report, _ = self.run_case("", 3, "--openalex")
        self.assertEqual(report["status"], "incomplete_or_failed")

    def test_openalex_adapter_malformed_fails(self):
        with patch.dict(os.environ, {"OPENALEX_API_KEY": "".join(("test", "key"))}):
            report, _ = self.run_case("openalex_malformed", 3, "--openalex")
        self.assertEqual(next(s for s in report["stages"] if s["stage"] == "openalex_search")["status"], "fail")

    def test_pdf_is_explicit_oa_and_requires_identity(self):
        report, backend = self.run_case()
        self.assertFalse(any(url.endswith(".pdf") for url, _, _ in backend.calls))
        with patch("litsearch.service_probe.extract_text_from_pdf", return_value=TITLE + "\n" + DOI):
            report = run_probe(self.args("oa", "--download-pdf"), client=Backend(3, "oa"), sleeper=lambda _: None)
        self.assertEqual(next(s for s in report["stages"] if s["stage"] == "oa_pdf")["status"], "pass")
        with patch("litsearch.service_probe.extract_text_from_pdf", return_value="Wrong paper"):
            report = run_probe(self.args("wrong", "--download-pdf"), client=Backend(3, "oa"), sleeper=lambda _: None)
        self.assertEqual(report["status"], "incomplete_or_failed")
        report = run_probe(self.args("no-oa", "--download-pdf"), client=Backend(), sleeper=lambda _: None)
        self.assertEqual(next(s for s in report["stages"] if s["stage"] == "oa_pdf")["reason"], "no_explicit_oa_pdf_candidate")

    def test_pdf_fallback_recovers_and_budget_reports_truncation(self):
        with patch("litsearch.service_probe.extract_text_from_pdf", return_value=TITLE + "\n" + DOI):
            report, _ = self.run_case("oa_fallback", 3, "--download-pdf")
        stage = next(s for s in report["stages"] if s["stage"] == "oa_pdf")
        self.assertEqual(stage["status"], "pass")
        self.assertEqual([a["status"] for a in stage["attempts"]], ["rejected", "pass"])
        report = run_probe(self.args("bounded", "--download-pdf", "--max-pdf-candidates", "1"), client=Backend(3, "oa_fallback"), sleeper=lambda _: None)
        self.assertEqual(next(s for s in report["stages"] if s["stage"] == "oa_pdf")["status"], "truncated")

    def test_no_clobber(self):
        # Keep the overwrite contract independently exercised below.
        path = self.root / "used"
        path.mkdir()
        (path / "keep.txt").write_text("preserved")
        self.assertEqual(main(["--out-dir", str(path)]), 2)
        self.assertEqual((path / "keep.txt").read_text(), "preserved")

    def test_secret_echo_pdf_is_never_saved_unredacted(self):
        contact = self.args().email

        class EchoPdf(Backend):
            def request(self, url, **kwargs):
                response = super().request(url, **kwargs)
                if url.endswith("probe.pdf"):
                    return Response(response.body + contact.encode(), response.content_type,
                                    response.final_url, response.status)
                return response

        with patch("litsearch.service_probe.extract_text_from_pdf", return_value=TITLE + " " + DOI):
            report = run_probe(self.args("echo", "--download-pdf"), client=EchoPdf(3, "oa"), sleeper=lambda _: None)
        pdf = next(stage for stage in report["stages"] if stage["stage"] == "oa_pdf")
        self.assertEqual(pdf["status"], "fail")
        self.assertFalse(list((self.root / "echo").glob("*.pdf")))
        for path in (self.root / "echo").iterdir():
            self.assertNotIn(contact.encode(), path.read_bytes())

    def test_configured_secret_in_final_url_path_is_redacted(self):
        marker = "SYNTHETIC_PATH_MARKER"

        class EchoPath:
            def request(self, url, **kwargs):
                return Response(b"{}", "application/json", "https://example.org/" + marker, 200)

        out = self.root / "path"
        out.mkdir()
        client = SnapshotClient(EchoPath(), out, page_size=1, max_bytes=4096,
                                timeout=1, secrets=(marker,))
        client.request("https://example.org/data")
        self.assertNotIn(marker, json.dumps(client.snapshots))

    def test_json_escaped_configured_secret_is_redacted(self):
        marker = 'encoded"credential_' + "123456789"

        class EchoJSON:
            def request(self, url, **kwargs):
                return Response(json.dumps({"echo": marker}).encode(), "application/json", url, 200)

        out = self.root / "escaped"
        out.mkdir()
        client = SnapshotClient(EchoJSON(), out, page_size=1, max_bytes=4096,
                                timeout=1, secrets=(marker,))
        client.request("https://example.org/data")
        saved = json.loads((out / "response_0001.bin").read_text())
        self.assertNotEqual(saved["echo"], marker)
        self.assertTrue(client.snapshots[0]["redacted"])

    def test_malformed_epmc_sources_and_identifiers_fail_contract(self):
        for source, identifier in (("INVALID_SOURCE", "1"), ("MED", "not-an-id"), ("PMC", "123")):
            class BadIdentity(Backend):
                def epmc_item(self, number):
                    item = super().epmc_item(number)
                    item.update(source=source, id=identifier)
                    return item

            with self.subTest(source=source, identifier=identifier):
                report = run_probe(self.args("bad-" + source + "-" + identifier),
                                   client=BadIdentity(1), sleeper=lambda _: None)
                self.assertEqual(next(s for s in report["stages"] if s["stage"] == "europepmc_search")["status"], "fail")
                self.assertNotEqual(report["status"], "pass")

    def test_online_needs_independent_identity_inputs(self):
        with self.assertRaises(SystemExit) as caught:
            main(["--out-dir", str(self.root / "unused"), "--online"])
        self.assertEqual(caught.exception.code, 2)
        self.assertFalse((self.root / "unused").exists())

    def test_public_transport_guard_even_with_injected_backend(self):
        backend = Backend()
        client = SnapshotClient(backend, self.root, page_size=2, max_bytes=2048, timeout=30)
        with self.assertRaises(NetworkError):
            client.request("http://127.0.0.1/private")
        self.assertEqual(backend.calls, [])


if __name__ == "__main__":
    unittest.main()
