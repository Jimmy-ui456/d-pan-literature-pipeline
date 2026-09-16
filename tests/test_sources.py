from __future__ import annotations

import urllib.parse
import unittest

from litsearch.net import Response
from litsearch.sources import fetch_openalex, fetch_pubmed


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
        return {"results": []}

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


if __name__ == "__main__":
    unittest.main()
