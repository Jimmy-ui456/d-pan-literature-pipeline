from __future__ import annotations

import urllib.parse
import unittest

from litsearch.net import Response
from litsearch.sources import fetch_openalex, fetch_pubmed
from litsearch.topics import load_topic


class FakeSourceClient:
    def __init__(self, *, pubmed_ids: list[str] | None = None, xml: bytes = b"") -> None:
        self.pubmed_ids = pubmed_ids
        self.xml = xml
        self.json_urls: list[str] = []
        self.posts: list[tuple[str, bytes]] = []

    def get_json(self, url: str, timeout: int = 40) -> dict:
        self.json_urls.append(url)
        if self.pubmed_ids is not None:
            return {"esearchresult": {"idlist": self.pubmed_ids}}
        return {"results": [], "meta": {"count": 0}}

    def post_bytes(self, url: str, data: bytes, **_: object) -> Response:
        self.posts.append((url, data))
        return Response(self.xml, "application/xml", url, 200)


PUBMED_XML = b"""<?xml version="1.0"?>
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation>
      <PMID>123</PMID>
      <Article>
        <ArticleTitle>D-pantothenic acid production</ArticleTitle>
        <Journal>
          <JournalIssue>
            <Volume>5</Volume><Issue>2</Issue>
            <PubDate><MedlineDate>2025 Winter</MedlineDate></PubDate>
          </JournalIssue>
          <Title>Test Journal</Title>
        </Journal>
        <Pagination><MedlinePgn>10-20</MedlinePgn></Pagination>
        <Abstract><AbstractText>Produced D-pantothenic acid at 5 g/L.</AbstractText></Abstract>
      </Article>
    </MedlineCitation>
    <PubmedData>
      <ArticleIdList>
        <ArticleId IdType="doi">10.1000/Test</ArticleId>
        <ArticleId IdType="pmc">PMC123</ArticleId>
      </ArticleIdList>
    </PubmedData>
  </PubmedArticle>
</PubmedArticleSet>
"""


class SourceConfigurationTests(unittest.TestCase):
    def test_openalex_requests_include_key_and_contact(self) -> None:
        client = FakeSourceClient()
        records = fetch_openalex(
            1,
            client=client,  # type: ignore[arg-type]
            api_key="secret",
            email="person@example.org",
            sleeper=lambda _: None,
        )
        self.assertEqual(records, [])
        self.assertGreater(len(client.json_urls), 1)
        for url in client.json_urls:
            params = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            self.assertEqual(params["api_key"], ["secret"])
            self.assertEqual(params["mailto"], ["person@example.org"])

    def test_openalex_rejects_missing_key(self) -> None:
        with self.assertRaisesRegex(ValueError, "API key"):
            fetch_openalex(
                1,
                client=FakeSourceClient(),  # type: ignore[arg-type]
                api_key="",
                email="person@example.org",
            )

    def test_pubmed_requests_identify_tool_and_parse_metadata(self) -> None:
        client = FakeSourceClient(pubmed_ids=["123"], xml=PUBMED_XML)
        records = fetch_pubmed(
            10,
            client=client,  # type: ignore[arg-type]
            email="person@example.org",
            api_key="ncbi-secret",
            sleeper=lambda _: None,
        )
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record.doi, "10.1000/test")
        self.assertEqual(record.year, "2025")
        self.assertEqual(record.pages, "10-20")
        body = urllib.parse.parse_qs(client.posts[0][1].decode("utf-8"))
        self.assertEqual(body["tool"], ["d_pan_literature_pipeline"])
        self.assertEqual(body["email"], ["person@example.org"])
        self.assertEqual(body["api_key"], ["ncbi-secret"])

    def test_openalex_rejects_malformed_success_and_accepts_zero(self) -> None:
        class Client(FakeSourceClient):
            def __init__(self, payload: dict) -> None:
                super().__init__()
                self.payload = payload

            def get_json(self, url: str, timeout: int = 40) -> dict:
                self.json_urls.append(url)
                return self.payload

        for payload in ({"error": "bad"}, {"results": []}, {"results": [], "meta": {"count": True}},
                        {"results": [{}], "meta": {"count": 0}}):
            events = []
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                fetch_openalex(1, client=Client(payload), api_key="key", email="a@example.org",
                               events=events, sleeper=lambda _: None)
            self.assertTrue(events)
            self.assertEqual(events[-1].status, "failed")
        records = fetch_openalex(1, client=Client({"results": [], "meta": {"count": 0}}),
                                 api_key="key", email="a@example.org", sleeper=lambda _: None)
        self.assertEqual(records, [])

    def test_pubmed_partial_batch_is_checkpointed_with_missing_id(self) -> None:
        class Partial:
            def get_json(self, url: str) -> dict:
                return {"esearchresult": {"count": "2", "idlist": ["1", "2"]}}

            def post_bytes(self, url: str, data: bytes) -> Response:
                return Response(PUBMED_XML.replace(b">123<", b">1<"), "application/xml", url, 200)

        saved = []
        events = []
        with self.assertRaisesRegex(ValueError, "missing"):
            fetch_pubmed(2, client=Partial(), email="a@example.org", events=events,
                         on_batch=saved.extend, sleeper=lambda _: None)
        self.assertEqual(len(saved), 1)
        self.assertIn('"missing": ["2"]', events[-1].message)

    def test_openalex_title_and_search_reject_nested_shapes_without_leaking(self) -> None:
        class Client(FakeSourceClient):
            def __init__(self, payload: object) -> None:
                super().__init__()
                self.payload = payload

            def get_json(self, url: str, timeout: int = 40) -> object:
                self.json_urls.append(url)
                return self.payload

        payloads = (
            {"results": [{"title": {"secret": "SECRET_SENTINEL"}}], "meta": {"count": 1}},
            {"results": [{"title": "ok", "primary_location": "oops", "api_key": "KEY_SENTINEL"}],
             "meta": {"count": 1}},
        )
        for payload in payloads:
            events: list[object] = []
            with self.subTest(payload=payload), self.assertRaises(ValueError) as raised:
                fetch_openalex(1, client=Client(payload), api_key="KEY_SENTINEL",
                               email="mail@example.org", events=events, sleeper=lambda _: None,
                               topic=load_topic() if "primary_location" in payload["results"][0] else None)  # type: ignore[index]
            self.assertNotIn("SECRET_SENTINEL", str(raised.exception))
            self.assertNotIn("KEY_SENTINEL", str(raised.exception))
            self.assertTrue(events)
            self.assertEqual(getattr(events[-1], "status"), "failed")

    def test_pubmed_duplicate_ids_checkpoint_once_but_diagnose_duplicate(self) -> None:
        article_start = PUBMED_XML.index(b"<PubmedArticle>")
        article_end = PUBMED_XML.index(b"</PubmedArticle>") + len(b"</PubmedArticle>")
        duplicate_xml = PUBMED_XML[:article_end] + PUBMED_XML[article_start:article_end] + PUBMED_XML[article_end:]

        class Duplicate:
            def get_json(self, url: str) -> dict:
                return {"esearchresult": {"count": "2", "idlist": ["1", "2"]}}

            def post_bytes(self, url: str, data: bytes) -> Response:
                return Response(duplicate_xml.replace(b">123<", b">1<"), "application/xml", url, 200)

        saved: list[object] = []
        events: list[object] = []
        with self.assertRaisesRegex(ValueError, "duplicate"):
            fetch_pubmed(2, client=Duplicate(), email="a@example.org", events=events,
                         on_batch=saved.extend, sleeper=lambda _: None)
        self.assertEqual([getattr(record, "pmid") for record in saved], ["1"])
        self.assertIn('"duplicate": ["1"]', getattr(events[-1], "message"))
        self.assertIn('"missing": ["2"]', getattr(events[-1], "message"))


if __name__ == "__main__":
    unittest.main()
