from __future__ import annotations

import unittest

from litsearch.core import (
    classify_and_score,
    contains_term,
    extract_results_like,
    extract_target_yields,
    merge_records,
    norm_doi,
    subsection_summary_cn,
)
from litsearch.models import Record
from litsearch.pdfs import _pages_for_excerpt


class NormalizationTests(unittest.TestCase):
    def test_normalizes_doi_urls_without_guessing_suffix(self) -> None:
        self.assertEqual(
            norm_doi(" DOI: https://doi.org/10.1000/ABC.123 "),
            "10.1000/abc.123",
        )

    def test_rejects_non_doi(self) -> None:
        self.assertEqual(norm_doi("not an identifier"), "")

    def test_short_gene_names_use_boundaries(self) -> None:
        self.assertTrue(contains_term("PanE improves flux", "pane"))
        self.assertFalse(contains_term("a panel study", "pane"))


class ScreeningTests(unittest.TestCase):
    def test_production_paper_is_core(self) -> None:
        record = Record(
            title="Metabolic engineering of Escherichia coli for D-pantothenic acid production"
        )
        classify_and_score(record)
        self.assertEqual(record.library_tier, "核心生产/合成文献")
        self.assertTrue(record.screening_reasons)

    def test_chirality_not_dropped_and_review_is_not_primary(self) -> None:
        titles = (
            "Selective Aqueous Biocatalysis of d -Panthenol via Engineered Enzyme",
            "Biotechnological Production of D‐Pantothenic Acid",
            "Development of a whole-cell catalyst for synthesis of d-(−)-pantolactone",
            "Progress on Production of D-calcium Pantothenate and D-panthenol",
        )
        for title in titles:
            with self.subTest(title=title):
                record = Record(title=title)
                classify_and_score(record)
                self.assertNotEqual(record.relevance_status, "out_of_scope")
                if title.startswith("Progress"):
                    self.assertEqual(record.relevance_status, "background_review")

    def test_infection_biosynthesis_paper_is_not_core(self) -> None:
        record = Record(
            title="Pantothenate biosynthesis is critical for chronic infection by Toxoplasma gondii"
        )
        classify_and_score(record)
        self.assertNotEqual(record.library_tier, "核心生产/合成文献")
        self.assertIn("非生产场景", "；".join(record.screening_reasons))

    def test_supplement_is_not_core(self) -> None:
        record = Record(
            title="Additional file 1 of metabolic engineering for D-pantothenic acid production"
        )
        classify_and_score(record)
        self.assertNotEqual(record.library_tier, "核心生产/合成文献")

    def test_target_enzyme_used_for_another_product_is_not_core(self) -> None:
        record = Record(
            title=(
                "Enhanced supply of acetyl-CoA by exogenous pantothenate kinase "
                "promotes synthesis of poly(3-hydroxybutyrate)"
            )
        )
        classify_and_score(record)
        self.assertNotEqual(record.library_tier, "核心生产/合成文献")

    def test_target_used_as_input_for_other_product_is_not_core(self) -> None:
        titles = (
            "Impact of pantothenic acid addition on H2S production by Saccharomyces",
            "PI3K drives the de novo synthesis of coenzyme A from vitamin B5",
            "Metabolomics reveals an involvement of pantothenate for male production",
        )
        for title in titles:
            with self.subTest(title=title):
                record = Record(title=title)
                classify_and_score(record)
                self.assertNotEqual(record.library_tier, "核心生产/合成文献")


class DeduplicationTests(unittest.TestCase):
    def test_title_author_year_merge_is_order_independent(self) -> None:
        with_doi = Record(
            title="A useful paper",
            doi="10.1000/useful",
            year="2025", authors=["Test Author"],
            source_ids=["OpenAlex"],
        )
        without_doi = Record(
            title="A useful paper",
            abstract="longer abstract",
            year="2025", authors=["Test Author"],
            source_ids=["PubMed"],
        )
        forward = merge_records([without_doi, with_doi])
        reverse = merge_records(
            [
                Record(title="A useful paper", doi="10.1000/useful", year="2025", authors=["Test Author"], source_ids=["OpenAlex"]),
                Record(title="A useful paper", abstract="longer abstract", year="2025", authors=["Test Author"], source_ids=["PubMed"]),
            ]
        )
        self.assertEqual(len(forward), 1)
        self.assertEqual(len(reverse), 1)
        self.assertEqual(forward[0].doi, reverse[0].doi)
        self.assertEqual(forward[0].abstract, "longer abstract")
        self.assertEqual(set(forward[0].source_ids), {"OpenAlex", "PubMed"})

    def test_repository_version_is_linked_not_deleted_without_relation_metadata(self) -> None:
        records = [
            Record(title="Same work", doi="10.6084/m9.figshare.123.v1", source_ids=["repo"]),
            Record(title="Same work", doi="10.1186/journal-article", source_ids=["publisher"]),
        ]
        merged = merge_records(records)
        self.assertEqual(len(merged), 2)
        self.assertTrue(all(rec.duplicate_keys for rec in merged))

    def test_distinct_publisher_dois_with_same_title_are_preserved(self) -> None:
        merged = merge_records(
            [
                Record(title="Annual report", doi="10.1000/one"),
                Record(title="Annual report", doi="10.1000/two"),
            ]
        )
        self.assertEqual(len(merged), 2)


class EvidenceTests(unittest.TestCase):
    def test_yield_requires_target_in_same_sentence(self) -> None:
        text = (
            "Glucose reached 80 g/L in the feed. "
            "The D-pantothenic acid titer reached 22.5 g/L after 48 h."
        )
        matches = extract_target_yields(text)
        self.assertEqual(len(matches), 1)
        self.assertIn("22.5 g/L", matches[0])
        self.assertNotIn("80 g/L", matches[0])

    def test_supplement_dose_is_not_reported_as_yield(self) -> None:
        text = "Mice were supplemented with pantothenic acid at 5 g/L in drinking water."
        self.assertEqual(extract_target_yields(text), [])

    def test_subsection_does_not_reuse_global_yield(self) -> None:
        record = Record(
            title="D-pantothenic acid production",
            abstract="D-pantothenic acid reached 88 g/L.",
            final_yield="D-pantothenic acid reached 88 g/L.",
            results_excerpt="some results",
        )
        summary = subsection_summary_cn(
            record,
            "Enzyme activity",
            "The enzyme activity increased after mutation, without a product titer.",
        )
        self.assertNotIn("88 g/L", summary)

    def test_results_parser_prefers_last_heading_over_toc(self) -> None:
        text = (
            "Contents\nResults\nMethods\n"
            + "table of contents " * 30
            + "\n[[PAGE 4]]\n3 Results\n"
            + "D-pantothenic acid production improved under fermentation conditions. " * 8
            + "\n4 Conclusion\nDone."
        )
        excerpt = extract_results_like(text)
        self.assertTrue(excerpt.startswith("3 Results"))
        self.assertNotIn("Contents", excerpt)

    def test_result_excerpt_retains_pdf_page_provenance(self) -> None:
        text = (
            "\n[[PAGE 1]]\nIntroduction text. "
            "\n[[PAGE 2]]\n3 Results\n"
            + "D-pantothenic acid reached 10 g/L in fermentation. " * 8
            + "\n[[PAGE 3]]\n"
            + "D-pantothenic acid reached 12 g/L after optimization. " * 8
            + "\n4 Conclusion\nDone."
        )
        excerpt = extract_results_like(text)
        self.assertEqual(_pages_for_excerpt(text, excerpt), [2, 3])


if __name__ == "__main__":
    unittest.main()
