from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from litsearch.document_parse import (Options, ParseError, _continuations, _ocr_image,
                                      _stop_tree, main, parse_bounded,
                                      parse_document, parse_tsv, selected_pages)


TSV = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"


def fake_engine(command, **kwargs):
    # Actual adapter writes PNG and reads engine file; only binary unavailable.
    kwargs["stdout"].write((TSV +
        "5\t1\t1\t1\t1\t1\t10\t10\t30\t10\t95\tDose\n"
        "5\t1\t1\t1\t1\t2\t100\t10\t30\t10\t95\tValue\n"
        "5\t1\t1\t1\t2\t1\t10\t30\t30\t10\t92\tA\n"
        "5\t1\t1\t1\t2\t2\t100\t30\t30\t10\t52\t0.4\n").encode())
    kwargs["stdout"].flush()
    return SimpleNamespace(pid=12345, poll=lambda: 0, wait=lambda timeout: 0)


class DocumentParseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def html(self, text):
        path = self.root / "table.html"
        path.write_bytes(text.encode("utf-8"))
        return path

    def test_explicit_multirow_headers_spans_units_and_footer(self):
        source = ("<table><caption>Table S1 supplement</caption><thead>"
                  "<tr><th rowspan='2'>Strain</th><th colspan='2'>Yield (g/L)</th></tr>"
                  "<tr><th>A</th><th>B</th></tr></thead><tbody>"
                  "<tr><td>X</td><td>0.4 ± 0.1*</td><td></td></tr></tbody>"
                  "<tfoot><tr><td colspan='3'>* n not reported</td></tr></tfoot></table>"
                  "<p>Supplement note &amp; detail.</p>")
        path = self.html(source)
        report = parse_document(path)
        table = report["tables"][0]
        self.assertEqual(report["source"]["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(table["header_rows"], [0, 1])
        self.assertEqual(table["raw_matrix"][0], ["Strain", "Yield (g/L)", None])
        self.assertEqual(table["grid"][0][0], table["grid"][1][0])
        self.assertEqual(table["raw_matrix"][2], ["X", "0.4 ± 0.1*", ""])
        self.assertEqual(table["caption"]["raw_text"], "Table S1 supplement")
        self.assertEqual(table["notes"][0]["association"], "explicit_tfoot")
        self.assertIn("Supplement note & detail.", table["notes"][1]["raw_text"])
        self.assertIn("(g/L)", table["unit_candidates"])
        self.assertEqual(table["raw_html"], source[:source.index("</table>") + 8])
        self.assertEqual(report["science_approval"], "none")
        self.assertIsNone(report["human_review"])
        self.assertEqual(path.read_text(encoding="utf-8"), source)

    def test_exact_whitespace_and_entity_text_preserved(self):
        report = parse_document(self.html("<table><tr><td> A &amp; B\n  0.4 g </td></tr></table>"))
        cell = report["tables"][0]["cells"][0]
        self.assertEqual(cell["raw_text"], " A & B\n  0.4 g ")
        self.assertIn("&amp;", cell["raw_html"])
        self.assertEqual(report["tables"][0]["header_status"], "unresolved")

    def test_html_rendered_boundaries_do_not_create_numbers(self):
        for contents, expected in (("1<br>2", "1\n2"),
                                   ("<div>1</div><div>2</div>", "1\n2\n"),
                                   ("<p>0.4</p><p>g</p>", "0.4\ng\n")):
            with self.subTest(contents=contents):
                source = "<div>Outside1</div><div>Outside2</div><table><tr><td>" + contents + "</td></tr></table>"
                result = parse_document(self.html(source))
                table = result["tables"][0]
                self.assertEqual(table["raw_matrix"], [[expected]])
                self.assertEqual(table["cells"][0]["raw_html"], "<td>" + contents + "</td>")
                self.assertIn("newlines", table["cells"][0]["text_reconstruction"])
                self.assertIn("Outside1\nOutside2\n", result["pages"][0]["native"]["text"])
                self.assertIn("source_html retained", result["pages"][0]["native"]["text_reconstruction"])

    def test_html_mismatched_table_structure_rejected(self):
        for source in ("<table><tr><th>0.4</td></tr></table>",
                       "<table><tr><td>0.4</th></tr></table>",
                       "<table><tbody><tr><td>1</td></tr></thead></table>",
                       "<table><tr><td>1</td></tr></tr></table>",
                       "<table><caption>test</table>",
                       "<table><td>1</td></table>"):
            with self.subTest(source=source), self.assertRaises(ParseError):
                parse_document(self.html(source))

    def test_html_padded_case_closing_markup_preserved(self):
        source = "<TABLE><TR><TD>0.4 g</TD   ></TR ></TABLE >"
        table = parse_document(self.html(source))["tables"][0]
        self.assertEqual(table["raw_html"], source)
        self.assertEqual(table["cells"][0]["raw_html"], "<TD>0.4 g</TD   >")

    def test_html_unresolved_structure_is_not_guessed(self):
        for content in ("<table><tr><td rowspan='0'>x</td></tr></table>",
                        "<table><tr><td colspan='-1'>x</td></tr></table>",
                        "<table><tr><td colspan='a'>x</td></tr></table>",
                        "<table><tr><td rowspan='2'>x</td></tr></table>",
                        "<table><tr><td><table></table></td></tr></table>",
                        "<table><tr><td>x</td></tr>"):
            with self.subTest(content=content), self.assertRaises(ParseError):
                parse_document(self.html(content))

    def test_html_grid_bombs_rejected(self):
        with self.assertRaises(ParseError):
            parse_document(self.html("<table><tr><td colspan='999999'>x</td></tr></table>"))
        with self.assertRaises(ParseError):
            parse_document(self.html("<table><tr><td colspan='20'>x</td></tr></table>"),
                           Options(max_cells=10))

    def test_global_expanded_grid_bound_and_body_row_headers(self):
        with self.assertRaisesRegex(ParseError, "Document HTML grid"):
            parse_document(self.html("<table><tr><td colspan='3'>A</td></tr></table>" * 2),
                           Options(max_cells=5))
        result = parse_document(self.html("<table><tr><th scope='row'>A</th><td>1</td></tr></table >"))
        self.assertEqual(result["tables"][0]["header_rows"], [])
        self.assertTrue(result["tables"][0]["raw_html"].endswith("</table >"))

    def test_empty_corrupt_inputs_and_limits(self):
        path = self.html("")
        with self.assertRaises(ParseError):
            parse_document(path)
        path = self.html("<html><script>abc</script></html>")
        with self.assertRaises(ParseError):
            parse_document(path)
        path = self.root / "bad.pdf"
        path.write_bytes(b"not pdf")
        with self.assertRaises(ParseError):
            parse_document(path)
        path = self.html("text")
        with self.assertRaises(ParseError):
            parse_document(path, Options(max_input_bytes=1))
        with self.assertRaises(ParseError):
            parse_document(path, Options(max_output_bytes=1))
        with self.assertRaises(ParseError):
            parse_document(path, Options(max_seconds=1e-12))

    def test_page_selection_rejects_invalid_and_large_ranges(self):
        self.assertEqual(selected_pages("3,1-2,2", 5, 3), [1, 2, 3])
        for spec in ("0", "4-1", "6", "a", "1-10000000"):
            with self.assertRaises(ParseError):
                selected_pages(spec, 5, 3)
        with self.assertRaises(ParseError):
            selected_pages("", 1000, 10)

    def test_invalid_options(self):
        for opts in (Options(dpi=1000), Options(language="-c evil"), Options(max_seconds=float("nan")),
                     Options(max_cells=0), Options(min_ocr_confidence=200), Options(ocr="maybe")):
            with self.assertRaises(ParseError):
                opts.validate()

    def test_cli_output_collision_prevents_parsing(self):
        target = self.root / "out"
        target.mkdir()
        marker = target / "keep"
        marker.write_text("keep", encoding="utf-8")
        with patch("litsearch.document_parse.parse_bounded") as extract:
            self.assertEqual(main([str(self.html("text")), "--out-dir", str(target)]), 2)
            extract.assert_not_called()
        self.assertEqual(marker.read_text(), "keep")

    def test_cli_reparse_parent_rejected(self):
        from litsearch.document_parse import _fresh_output
        original = Path.lstat
        parent = self.root / "redirect"
        def lstat(path):
            if path == parent:
                return SimpleNamespace(st_mode=0, st_file_attributes=0x400)
            return original(path)
        with patch.object(Path, "lstat", lstat), self.assertRaisesRegex(ParseError, "reparse"):
            _fresh_output(parent / "out")

    def test_cli_isolated_html_writes_only_report(self):
        target = self.root / "fresh"
        self.assertEqual(main([str(self.html("<table><tr><td>0.4 g</td></tr></table>")),
                               "--out-dir", str(target)]), 0)
        self.assertEqual([p.name for p in target.iterdir()], ["document.json"])
        data = json.loads((target / "document.json").read_text(encoding="utf-8"))
        self.assertEqual(data["schema_version"], "document-parse/1")

    def test_bounded_worker_timeout(self):
        with self.assertRaises(ParseError):
            parse_bounded(self.html("text"), Options(max_seconds=.001))

    def test_continuation_only_candidate_no_header_fill(self):
        first = {"id": "a", "page": 1, "caption": {"raw_text": "Table 2 Results"},
                 "raw_matrix": [["Dose", "g/L"]]}
        second = {"id": "b", "page": 2, "caption": {"raw_text": "Table 2 (continued)"},
                  "raw_matrix": [["X", None]]}
        third = {"id": "c", "page": 3, "caption": {"raw_text": "Results"},
                 "raw_matrix": [["A", "B"]]}
        _continuations([first, second, third])
        self.assertEqual(second["continuation"]["candidate_ids"], ["a"])
        self.assertFalse(second["continuation"]["merged"])
        self.assertEqual(second["raw_matrix"], [["X", None]])
        self.assertNotIn("continuation", third)

    def test_tsv_invalid_boxes_and_header(self):
        with self.assertRaises(ParseError):
            parse_tsv("text\nabc\n", 100, 100)
        for box in ("-1\t1\t20\t10", "95\t1\t20\t10"):
            with self.assertRaises(ParseError):
                parse_tsv(TSV + "5\t1\t1\t1\t1\t1\t" + box + "\t99\tx\n", 100, 100)

    def test_tsv_exact_schema_cannot_overwrite_word(self):
        row = "5\t1\t1\t1\t1\t1\t10\t10\t20\t10\t99\t0.4g\n"
        for raw in (TSV.rstrip("\n") + "\ttext\n" + row.rstrip("\n") + "\t4.0g\n",
                    TSV + row.rstrip("\n") + "\t4.0g\n",
                    TSV + "\t".join(row.rstrip("\n").split("\t")[:-1]) + "\n",
                    TSV.replace("conf", "text") + row):
            with self.subTest(raw=raw), self.assertRaises(ParseError):
                parse_tsv(raw, 100, 100)
        self.assertEqual(parse_tsv(TSV + row, 100, 100)[0]["raw_text"], "0.4g")

    def test_tsv_word_dimensions_hierarchy_and_literal_quotes(self):
        valid = "5\t1\t1\t1\t1\t1\t10\t10\t20\t10\t99\t\"0.4g\"\n"
        self.assertEqual(parse_tsv(TSV + valid, 100, 100)[0]["raw_text"], '"0.4g"')
        fields = valid.rstrip("\n").split("\t")
        for index, value in ((0, "6"), (1, "0"), (2, "0"), (3, "-1"), (4, "0"),
                             (5, "0"), (8, "0"), (9, "0")):
            changed = fields.copy()
            changed[index] = value
            with self.subTest(index=index), self.assertRaises(ParseError):
                parse_tsv(TSV + "\t".join(changed) + "\n", 100, 100)
        with self.assertRaisesRegex(ParseError, "duplicate word"):
            parse_tsv(TSV + valid * 2, 100, 100)
        with self.assertRaisesRegex(ParseError, "hierarchy"):
            parse_tsv(TSV + "1\t1\t1\t0\t0\t0\t0\t0\t100\t100\t-1\t\n", 100, 100)
        self.assertEqual(parse_tsv(TSV + "1\t1\t0\t0\t0\t0\t0\t0\t100\t100\t-1\t\n" + valid,
                                   100, 100)[0]["raw_text"], '"0.4g"')

    def test_stop_tree_missing_posix_group_uses_exact_fallback(self):
        from unittest.mock import Mock
        fallback = Mock()
        with patch("litsearch.document_parse.os.name", "posix"), \
             patch("litsearch.document_parse.signal.SIGKILL", 9, create=True), \
             patch("litsearch.document_parse.os.killpg", create=True, side_effect=ProcessLookupError) as killpg:
            _stop_tree(12345, fallback)
        killpg.assert_called_once()
        fallback.assert_called_once_with()

    def test_stop_tree_nonleader_never_signals_unrelated_group(self):
        from unittest.mock import Mock
        fallback = Mock()
        with patch("litsearch.document_parse.os.name", "posix"), \
             patch("litsearch.document_parse.os.killpg", create=True) as killpg:
            _stop_tree(12345, fallback, process_group=False)
        killpg.assert_not_called()
        fallback.assert_called_once_with()

    def test_stop_tree_windows_failed_taskkill_uses_exact_fallback(self):
        from unittest.mock import Mock
        for outcome in (SimpleNamespace(returncode=1), OSError("missing taskkill"),
                        subprocess.TimeoutExpired("taskkill", 5)):
            fallback = Mock()
            kwargs = {"side_effect": outcome} if isinstance(outcome, Exception) else {"return_value": outcome}
            with patch("litsearch.document_parse.os.name", "nt"), \
                 patch("litsearch.document_parse.subprocess.run", **kwargs):
                _stop_tree(12345, fallback)
            fallback.assert_called_once_with()

    @unittest.skipUnless(importlib.util.find_spec("PIL"), "Pillow optional")
    def test_real_fake_engine_timeout_reaps_exact_process(self):
        from PIL import Image
        real_popen = subprocess.Popen
        engines = []
        def sleeping_engine(command, **kwargs):
            if command[0] != sys.executable:
                return real_popen(command, **kwargs)  # Real taskkill must remain real on Windows.
            process = real_popen([sys.executable, "-c", "import time; time.sleep(60)"], **kwargs)
            engines.append(process)
            return process
        try:
            with patch("litsearch.document_parse.shutil.which", return_value=sys.executable), \
                 patch("litsearch.document_parse.subprocess.Popen", side_effect=sleeping_engine), \
                 patch("litsearch.document_parse._IN_WORKER", True), \
                 self.assertRaisesRegex(ParseError, "timeout"):
                _ocr_image(Image.new("RGB", (50, 50), "white"), 1,
                           Options(ocr_timeout=.05), time.monotonic() + 10)
            self.assertEqual(len(engines), 1)
            self.assertIsNotNone(engines[0].poll())
        finally:
            for process in engines:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)

    @unittest.skipUnless(importlib.util.find_spec("PIL"), "Pillow optional")
    def test_image_adapter_fake_tesseract_preserves_confidence_and_boxes(self):
        from PIL import Image
        image = Image.new("RGB", (200, 100), "white")
        with patch("litsearch.document_parse.shutil.which", return_value="fake-tesseract"), \
             patch("litsearch.document_parse.subprocess.Popen", side_effect=fake_engine) as run:
            result = _ocr_image(image, 1, Options(ocr="always"), time.monotonic() + 20)
        self.assertFalse(run.call_args.kwargs["shell"])
        self.assertEqual(result["words"][3]["confidence"], 52)
        self.assertEqual(result["words"][3]["bbox_pixels"], [100, 30, 130, 40])
        self.assertEqual(result["quality"], "noisy_or_empty")
        self.assertEqual(result["table_candidates"][0]["status"], "unresolved")
        self.assertIsNone(result["table_candidates"][0]["cells"][0]["rowspan"])

    @unittest.skipUnless(importlib.util.find_spec("PIL"), "Pillow optional")
    def test_ocr_missing_engine_timeout_and_failure(self):
        from PIL import Image
        image = Image.new("RGB", (200, 100), "white")
        with patch("litsearch.document_parse.shutil.which", return_value=None), self.assertRaisesRegex(
                ParseError, "Tesseract executable missing"):
            _ocr_image(image, 1, Options(), time.monotonic() + 20)
        with patch("litsearch.document_parse.shutil.which", return_value="fake"), \
             patch("litsearch.document_parse.subprocess.Popen", return_value=SimpleNamespace(
                 pid=12345, poll=lambda: None, wait=lambda timeout: 0, kill=lambda: None)), \
             patch("litsearch.document_parse._stop_tree"), \
             self.assertRaisesRegex(ParseError, "timeout"):
            _ocr_image(image, 1, Options(ocr_timeout=.001), time.monotonic() + 20)
        with patch("litsearch.document_parse.shutil.which", return_value="fake"), \
             patch("litsearch.document_parse.subprocess.Popen", return_value=SimpleNamespace(
                 pid=12345, poll=lambda: 1, wait=lambda timeout: 1)), \
             self.assertRaisesRegex(ParseError, "failed"):
            _ocr_image(image, 1, Options(), time.monotonic() + 20)
        with self.assertRaisesRegex(ParseError, "max_pixels"):
            _ocr_image(image, 1, Options(max_pixels=1), time.monotonic() + 20)

    @unittest.skipUnless(importlib.util.find_spec("PIL"), "Pillow optional")
    def test_ocr_live_output_budget_kills_process(self):
        from PIL import Image
        image = Image.new("RGB", (200, 100), "white")
        def noisy(command, **kwargs):
            kwargs["stdout"].write(b"x" * 1000)
            kwargs["stdout"].flush()
            return SimpleNamespace(pid=12345, poll=lambda: None, wait=lambda timeout: 0, kill=lambda: None)
        with patch("litsearch.document_parse.shutil.which", return_value="fake"), \
             patch("litsearch.document_parse.subprocess.Popen", side_effect=noisy), \
             patch("litsearch.document_parse._stop_tree") as stop, \
             self.assertRaisesRegex(ParseError, "max_output_bytes"):
            _ocr_image(image, 1, Options(max_output_bytes=100), time.monotonic() + 20)
        stop.assert_called_once()

    @unittest.skipUnless(importlib.util.find_spec("PIL"), "Pillow optional")
    def test_ocr_cleanup_wait_fallback_and_worker_nonleader(self):
        from PIL import Image
        from unittest.mock import Mock
        wait = Mock(side_effect=[subprocess.TimeoutExpired("engine", 5), 0])
        kill = Mock()
        engine = SimpleNamespace(pid=12345, poll=lambda: None, wait=wait, kill=kill)
        with patch("litsearch.document_parse.shutil.which", return_value="fake"), \
             patch("litsearch.document_parse.subprocess.Popen", return_value=engine) as start, \
             patch("litsearch.document_parse._IN_WORKER", True), \
             patch("litsearch.document_parse._stop_tree") as stop, \
             self.assertRaisesRegex(ParseError, "timeout"):
            _ocr_image(Image.new("RGB", (50, 50)), 1, Options(ocr_timeout=.001), time.monotonic() + 10)
        stop.assert_called_once_with(12345, kill, process_group=False)
        self.assertFalse(start.call_args.kwargs["start_new_session"])
        kill.assert_called_once_with()
        self.assertEqual(wait.call_count, 2)

    @unittest.skipUnless(importlib.util.find_spec("PIL"), "Pillow optional")
    def test_empty_ocr_is_partial_not_success(self):
        from PIL import Image
        path = self.root / "blank.png"
        Image.new("RGB", (200, 100), "white").save(path)
        def empty(command, **kwargs):
            kwargs["stdout"].write(TSV.encode())
            return SimpleNamespace(pid=12345, poll=lambda: 0, wait=lambda timeout: 0)
        with patch("litsearch.document_parse.shutil.which", return_value="fake"), \
             patch("litsearch.document_parse.subprocess.Popen", side_effect=empty):
            result = parse_document(path, Options(ocr="always"))
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["pages"][0]["ocr"]["words"], [])

    @unittest.skipUnless(importlib.util.find_spec("PIL"), "Pillow optional")
    def test_image_partial_and_multipage_selection(self):
        from PIL import Image
        image = Image.new("RGB", (200, 100), "white")
        path = self.root / "scan.tiff"
        image.save(path, save_all=True, append_images=[image])
        with patch("litsearch.document_parse.shutil.which", return_value="fake"), \
             patch("litsearch.document_parse.subprocess.Popen", side_effect=fake_engine):
            result = parse_document(path, Options(ocr="auto", pages="2"))
        self.assertEqual([p["page"] for p in result["pages"]], [2])
        with patch("litsearch.document_parse.shutil.which", return_value=None):
            result = parse_document(path, Options(ocr="auto"))
        self.assertEqual(result["status"], "partial")
        self.assertEqual(len(result["errors"]), 2)
        with self.assertRaisesRegex(ParseError, "require --ocr"):
            parse_document(path)


@unittest.skipUnless(all(importlib.util.find_spec(module) for module in
                         ("pdfplumber", "reportlab", "pypdfium2", "PIL")), "Optional PDF adapters unavailable")
class SyntheticPDFTests(unittest.TestCase):
    def setUp(self):
        __import__("pypdfium2")  # Import native dependency before mocking OCR's subprocess.
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def pdf(self, image=False, rotated=False):
        from reportlab.pdfgen import canvas
        from reportlab.lib.utils import ImageReader
        from PIL import Image, ImageDraw
        path = self.root / "synthetic.pdf"
        doc = canvas.Canvas(str(path), pagesize=(300, 300))
        if rotated:
            doc.setPageRotation(90)
        if image:
            raster = Image.new("RGB", (200, 100), "white")
            ImageDraw.Draw(raster).text((10, 10), "Dose 0.4 g", fill="black")
            doc.drawImage(ImageReader(raster), 20, 100, width=200, height=100)
        else:
            doc.drawString(20, 270, "Table S1 Yield (g/L)")
            for y in (240, 210, 180):
                doc.line(20, y, 220, y)
            for x in (20, 100, 220):
                doc.line(x, 180, x, 240)
            for x, y, text in ((25, 220, "Dose"), (105, 220, "Yield (g/L)"),
                               (25, 190, "A"), (105, 190, "0.4*")):
                doc.drawString(x, y, text)
            doc.drawString(20, 155, "* n not reported")
        doc.showPage()
        doc.drawString(20, 270, "Second page native text remains separate.")
        doc.save()
        return path

    def test_actual_pdf_table_text_footnotes_boxes(self):
        path = self.pdf()
        result = parse_document(path, Options(pages="1"))
        self.assertEqual(result["status"], "extracted_pending_review")
        table = result["tables"][0]
        self.assertEqual(table["raw_matrix"], [["Dose", "Yield (g/L)"], ["A", "0.4*"]])
        self.assertTrue(table["cells"][0]["raw_characters"])
        self.assertEqual(table["cells"][0]["coordinates"], "pdf_points_top_left")
        self.assertIn("n not reported", table["notes"][0]["raw_text"])
        self.assertEqual(table["header_rows"], [])
        self.assertEqual(table["header_status"], "unresolved")

    def test_pdf_raster_adapter_mapping_and_separate_layer(self):
        path = self.pdf(image=True)
        with patch("litsearch.document_parse.shutil.which", return_value="fake"), \
             patch("litsearch.document_parse.subprocess.Popen", side_effect=fake_engine):
            result = parse_document(path, Options(ocr="auto"))
        self.assertEqual(result["pages"][0]["native"]["text"], "")
        word = result["pages"][0]["ocr"]["words"][0]
        self.assertIn("bbox_pdf_points_bottom_left", word)
        self.assertIsNone(result["pages"][1]["ocr"])
        self.assertEqual(result["pages"][0]["ocr"]["quality"], "noisy_or_empty")

    def test_rotated_pdf_ocr_posconv(self):
        path = self.pdf(image=True, rotated=True)
        with patch("litsearch.document_parse.shutil.which", return_value="fake"), \
             patch("litsearch.document_parse.subprocess.Popen", side_effect=fake_engine):
            result = parse_document(path, Options(ocr="always", pages="1"))
        self.assertEqual(result["pages"][0]["rotation"], 90)
        bbox = result["pages"][0]["ocr"]["words"][0]["bbox_pdf_points_bottom_left"]
        self.assertEqual(len(bbox), 4)
        self.assertTrue(all(0 <= x <= 300 for x in bbox))

    def test_ocr_failure_preserves_other_pages(self):
        path = self.pdf(image=True)
        with patch("litsearch.document_parse.shutil.which", return_value=None):
            result = parse_document(path, Options(ocr="auto"))
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["pages"][0]["status"], "partial")
        self.assertIn("Second page", result["pages"][1]["native"]["text"])

    def test_native_page_failure_can_still_ocr_and_continue(self):
        path = self.pdf(image=True)
        with patch("litsearch.document_parse._pdf_table", side_effect=RuntimeError("bad table")), \
             patch("litsearch.document_parse.shutil.which", return_value="fake"), \
             patch("litsearch.document_parse.subprocess.Popen", side_effect=fake_engine):
            # Text PDF first page causes table failure, second native page survives.
            result = parse_document(self.pdf(), Options(ocr="always"))
        self.assertEqual(result["status"], "partial")
        self.assertIsNotNone(result["pages"][0]["ocr"])
        self.assertIn("Second page", result["pages"][1]["native"]["text"])

    def test_actual_merged_geometry(self):
        from reportlab.pdfgen import canvas
        path = self.root / "merged.pdf"
        doc = canvas.Canvas(str(path), pagesize=(300, 300))
        for y in (240, 210, 180):
            doc.line(20, y, 220, y)
        for x in (20, 220):
            doc.line(x, 180, x, 240)
        doc.line(100, 180, 100, 210)
        doc.drawString(30, 220, "Merged (g/L)")
        doc.drawString(30, 190, "A")
        doc.drawString(110, 190, "0.4")
        doc.save()
        result = parse_document(path)
        table = result["tables"][0]
        merged = next(c for c in table["cells"] if c["colspan"] == 2)
        self.assertEqual(merged["raw_text"], "Merged (g/L)")
        self.assertEqual(table["grid"][0][0], table["grid"][0][1])
        self.assertIsNone(table["raw_matrix"][0][1])

    @unittest.skipUnless(shutil.which("tesseract"), "Tesseract binary is not installed")
    def test_real_tesseract_on_synthetic_image(self):
        from PIL import Image, ImageDraw, ImageFont
        path = self.root / "real-ocr.png"
        image = Image.new("RGB", (800, 200), "white")
        ImageDraw.Draw(image).text((30, 50), "SYNTHETIC DOSE 0.4 g", font=ImageFont.load_default(size=40), fill="black")
        image.save(path)
        result = parse_document(path, Options(ocr="always"))
        self.assertEqual(result["status"], "extracted_pending_review")
        self.assertTrue(result["pages"][0]["ocr"]["words"])


if __name__ == "__main__":
    unittest.main()
