"""Offline, provenance-preserving PDF/HTML/image extraction.

Nothing in this module interprets scientific quantities or approves evidence.
HTML spans are explicit; PDF spans and OCR rows are geometric observations.
CLI parsing runs in an isolated process with a wall-clock deadline. Optional
dependencies are imported only by the adapter which needs them.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import importlib
import io
import json
import math
import multiprocessing
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

_IN_WORKER = False
_HTML_BREAK_TAGS = {"address", "article", "aside", "blockquote", "dd", "div", "dl", "dt",
                    "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3",
                    "h4", "h5", "h6", "header", "hr", "li", "main", "nav", "ol", "p", "pre",
                    "section", "table", "tr", "ul"}


class ParseError(ValueError):
    """Invalid input, missing dependency, or exceeded resource limit."""


@dataclass
class Options:
    ocr: str = "off"
    pages: str = ""
    dpi: int = 200
    language: str = "eng"
    tesseract: str = "tesseract"
    ocr_timeout: float = 30.0
    max_seconds: float = 120.0
    max_input_bytes: int = 50_000_000
    max_output_bytes: int = 20_000_000
    max_pages: int = 200
    max_cells: int = 100_000
    max_span: int = 256
    max_pixels: int = 40_000_000
    min_native_chars: int = 20
    min_ocr_confidence: float = 70.0
    table_strategy: str = "lines"

    def validate(self) -> None:
        if self.ocr not in {"off", "auto", "always"}:
            raise ParseError("ocr must be off, auto or always")
        if self.table_strategy not in {"lines", "text"}:
            raise ParseError("table strategy must be lines or text")
        if not 72 <= self.dpi <= 600:
            raise ParseError("dpi must be between 72 and 600")
        if not re.fullmatch(r"[A-Za-z0-9_]+(?:\+[A-Za-z0-9_]+)*", self.language):
            raise ParseError("language must be a Tesseract language code, e.g. eng or eng+chi_sim")
        for name in ("ocr_timeout", "max_seconds", "max_input_bytes", "max_output_bytes",
                     "max_pages", "max_cells", "max_span", "max_pixels"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ParseError(f"{name} must be finite and positive")
        if self.min_native_chars < 0 or not 0 <= self.min_ocr_confidence <= 100:
            raise ParseError("invalid native-character threshold or OCR confidence threshold")


def _dependency(name: str, extra: str):
    try:
        return importlib.import_module(name)
    except ImportError as exc:
        raise ParseError(f"Missing {name}; install pip install 'd-pan-literature-pipeline[{extra}]'") from exc


def _check(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise ParseError("Document parsing exceeded max_seconds")


def _stop_tree(pid: int, fallback, *, process_group: bool = True) -> None:
    """Stop a known group leader/tree, or kill an exact nonleader process.

    OCR engines inside a worker inherit its session and are not group leaders.
    Never assume their PID names a group; the parent cleans the worker group.
    """
    stopped = False
    if os.name == "nt":
        try:
            result = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    shell=False, timeout=5, check=False)
            stopped = result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            pass
    elif process_group:
        try:
            os.killpg(pid, signal.SIGKILL)
            stopped = True
        except OSError:
            pass
    if not stopped:
        try:
            fallback()
        except ProcessLookupError:
            pass  # The exact process exited between observation and cleanup.


def _fresh_output(path: Path) -> None:
    """Reject symlink/junction parents before creating a new output tree."""
    for component in (path, *path.parents):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ParseError("Output path contains a symlink/junction/reparse point")
    if path.exists():
        raise ParseError("Output directory already exists; choose a fresh path (no overwrites)")


def selected_pages(spec: str, total: int, maximum: int) -> list[int]:
    """Return 1-based pages, never silently truncate or accept invalid pages."""
    if not spec:
        if total > maximum:
            raise ParseError(f"Document has {total} pages; select pages or raise --max-pages")
        return list(range(1, total + 1))
    pages: set[int] = set()
    for token in spec.split(","):
        match = re.fullmatch(r"\s*(\d+)(?:-(\d+))?\s*", token)
        if not match:
            raise ParseError("pages must be 1-based comma-separated pages/ranges, e.g. 1,3-5")
        first, last = int(match[1]), int(match[2] or match[1])
        if first < 1 or last < first or last > total or last - first + 1 > maximum:
            raise ParseError(f"Invalid or excessive page range: {token}")
        pages.update(range(first, last + 1))
        if len(pages) > maximum:
            raise ParseError("Selected page count exceeds max_pages")
    return sorted(pages)


def _units(text: str) -> list[str]:
    # Only label candidates, retain spelling, never transform numbers/units.
    return list(dict.fromkeys(re.findall(r"\([^()\n]{1,80}\)|\[[^\[\]\n]{1,80}\]", text)))


def _table(table_id: str, page: int, method: str) -> dict[str, Any]:
    return {"id": table_id, "page": page, "method": method, "status": "unresolved",
            "cells": [], "raw_matrix": [], "grid": [], "header_rows": [],
            "header_status": "unresolved", "caption": None, "notes": [],
            "unit_candidates": [], "continuation": {"status": "unresolved", "candidate_ids": []}}


class _TablesHTML(HTMLParser):
    """Bounded explicit HTML table expansion; no network/XML/entity resolver."""
    def __init__(self, source: str, opts: Options, deadline: float):
        super().__init__(convert_charrefs=True)
        self.source, self.opts, self.deadline = source, opts, deadline
        self.tables: list[dict[str, Any]] = []
        self.current: dict[str, Any] | None = None
        self.cell: dict[str, Any] | None = None
        self.rows: list[list[dict[str, Any]]] = []
        self.row: list[dict[str, Any]] | None = None
        self.section = "body"
        self.caption = False
        self.table_depth = 0
        self.ignore_depth = 0
        self.page_text: list[str] = []
        self.after: list[str] = []
        self.total_cells = 0
        self.total_grid_cells = 0
        self.structure: list[str] = []
        self.line_offsets = [0]
        for match in re.finditer("\n", source):
            self.line_offsets.append(match.end())

    def _offset(self) -> int:
        line, column = self.getpos()
        return self.line_offsets[line - 1] + column

    def _span(self, attrs: dict, name: str) -> int:
        raw = attrs.get(name, "1")
        if not re.fullmatch(r"[1-9]\d{0,5}", raw or ""):
            raise ParseError(f"Invalid HTML {name}: {raw!r}; zero/negative/noninteger spans need manual review")
        value = int(raw)
        if value > self.opts.max_span:
            raise ParseError(f"HTML {name} exceeds max_span")
        return value

    def handle_starttag(self, tag: str, attrs: list) -> None:
        _check(self.deadline)
        parents = {"table": {None}, "thead": {"table"}, "tbody": {"table"},
                   "tfoot": {"table"}, "caption": {"table"},
                   "tr": {"table", "thead", "tbody", "tfoot"}, "td": {"tr"}, "th": {"tr"}}
        if tag in parents:
            parent = self.structure[-1] if self.structure else None
            if parent not in parents[tag]:
                raise ParseError(f"Invalid/unclosed HTML table structure before <{tag}>")
            self.structure.append(tag)
        if tag == "br" or tag in _HTML_BREAK_TAGS or tag in {"td", "th"}:
            self._boundary(force=tag == "br")
        if tag in {"script", "style"}:
            self.ignore_depth += 1
        if tag == "table":
            if self.current is not None:
                raise ParseError("Nested HTML tables require manual extraction; not flattened")
            if self.tables and self.after:
                self.tables[-1]["notes"].append({"raw_text": "".join(self.after),
                                               "association": "following_context_candidate"})
            self.after = []
            self.current = _table(f"table-{len(self.tables) + 1}", 1, "html_explicit_spans")
            self.current["source_start"] = self._offset()
            self.current["source_line"] = self.getpos()[0]
            self.rows = []
            self.section = "body"
        elif self.current is not None:
            if tag in {"thead", "tbody", "tfoot"}:
                self.section = tag
            elif tag == "caption":
                self.caption = True
                self.current["caption"] = {"raw_text": "", "association": "explicit_caption"}
            elif tag == "tr":
                if self.row is not None:
                    raise ParseError("Unclosed/nested HTML row")
                self.row = []
                self.rows.append(self.row)
                if len(self.rows) > self.opts.max_cells:
                    raise ParseError("HTML row count exceeds max_cells")
            elif tag in {"td", "th"}:
                if self.row is None or self.cell is not None:
                    raise ParseError("HTML cell outside row or unclosed cell")
                attributes = dict(attrs)
                self.cell = {"raw_text": "", "rowspan": self._span(attributes, "rowspan"),
                             "colspan": self._span(attributes, "colspan"), "kind": tag,
                             "section": self.section, "source_line": self.getpos()[0],
                             "source_start": self._offset(), "attributes": attributes}
                self.row.append(self.cell)
                self.total_cells += 1
                if self.total_cells > self.opts.max_cells:
                    raise ParseError("HTML cell count exceeds max_cells")

    def _boundary(self, *, force: bool = False) -> None:
        """Retain rendered breaks explicitly, without silently joining numbers."""
        if self.ignore_depth:
            return
        if force or (self.page_text and not self.page_text[-1].endswith("\n")):
            self.page_text.append("\n")
        target = self.cell
        if target is None and self.caption and self.current is not None:
            target = self.current["caption"]
        if target is not None:
            value = target["raw_text"]
            if force or (value and not value.endswith("\n")):
                target["raw_text"] += "\n"
        elif self.current is None and self.tables and (force or (self.after and not self.after[-1].endswith("\n"))):
            self.after.append("\n")

    def handle_endtag(self, tag: str) -> None:
        structural = {"table", "thead", "tbody", "tfoot", "caption", "tr", "td", "th"}
        if tag in structural:
            if not self.structure or self.structure[-1] != tag:
                raise ParseError(f"Mismatched HTML table closing tag </{tag}>")
            self.structure.pop()
        if tag in _HTML_BREAK_TAGS:
            self._boundary()
        if tag in {"script", "style"}:
            self.ignore_depth = max(0, self.ignore_depth - 1)
        if self.current is None:
            return
        if tag in {"td", "th"} and self.cell is not None:
            self.cell["raw_html"] = self.source[self.cell["source_start"]:self.source.find(">", self._offset()) + 1]
            self.cell = None
        elif tag == "tr":
            if self.cell is not None:
                raise ParseError("Unclosed HTML cell")
            self.row = None
        elif tag == "caption":
            self.caption = False
        elif tag in {"thead", "tbody", "tfoot"}:
            self.section = "body"
        elif tag == "table":
            if self.row is not None or self.cell is not None:
                raise ParseError("Unclosed HTML row/cell")
            self._finish()

    def handle_data(self, data: str) -> None:
        if self.ignore_depth:
            return
        self.page_text.append(data)
        if self.cell is not None:
            self.cell["raw_text"] += data
        elif self.caption and self.current is not None:
            self.current["caption"]["raw_text"] += data
        elif self.current is None and self.tables:
            self.after.append(data)

    def _finish(self) -> None:
        table = self.current
        assert table is not None
        table["raw_html"] = self.source[table["source_start"]:self.source.find(">", self._offset()) + 1]
        occupied: dict[tuple[int, int], str] = {}
        anchors: dict[tuple[int, int], str] = {}
        for r, row in enumerate(self.rows):
            col = 0
            if any(cell["section"] == "thead" or cell["attributes"].get("scope") in {"col", "colgroup"}
                   for cell in row) or (row and all(cell["kind"] == "th" and
                   cell["attributes"].get("scope") not in {"row", "rowgroup"} for cell in row)):
                table["header_rows"].append(r)
            for cell in row:
                while (r, col) in occupied:
                    col += 1
                if r + cell["rowspan"] > len(self.rows):
                    raise ParseError("HTML rowspan extends beyond explicit rows")
                count = cell["rowspan"] * cell["colspan"]
                if len(occupied) + count > self.opts.max_cells:
                    raise ParseError("Expanded HTML grid exceeds max_cells")
                cell_id = f"{table['id']}-cell-{len(table['cells']) + 1}"
                for rr in range(r, r + cell["rowspan"]):
                    for cc in range(col, col + cell["colspan"]):
                        if (rr, cc) in occupied:
                            raise ParseError("Overlapping HTML spans; unresolved layout")
                        occupied[rr, cc] = cell_id
                cell.update({"id": cell_id, "row": r, "column": col, "bbox": None,
                             "coordinates": "html_grid", "span_evidence": "explicit_attributes",
                             "text_reconstruction": "Decoded HTML text with explicit br/block boundaries as newlines; raw_html retained"})
                anchors[r, col] = cell["raw_text"]
                table["cells"].append(cell)
                if cell["section"] == "tfoot":
                    table["notes"].append({"raw_text": cell["raw_text"], "cell_id": cell_id,
                                           "association": "explicit_tfoot"})
                col += cell["colspan"]
        width = max((c + 1 for _, c in occupied), default=0)
        if len(self.rows) * width > self.opts.max_cells:
            raise ParseError("Rectangular HTML matrix exceeds max_cells")
        self.total_grid_cells += len(self.rows) * width
        if self.total_grid_cells > self.opts.max_cells:
            raise ParseError("Document HTML grid count exceeds max_cells")
        table["grid"] = [[occupied.get((r, c)) for c in range(width)] for r in range(len(self.rows))]
        table["raw_matrix"] = [[anchors.get((r, c)) for c in range(width)] for r in range(len(self.rows))]
        table["header_status"] = "explicit_markup" if table["header_rows"] else "unresolved"
        table["unit_candidates"] = _units(" ".join(c["raw_text"] for c in table["cells"]))
        table["status"] = "extracted_pending_review"
        self.tables.append(table)
        self.current = None

    def finish(self) -> None:
        self.close()
        if self.current is not None:
            raise ParseError("Unclosed HTML table")
        if self.tables and self.after:
            self.tables[-1]["notes"].append({"raw_text": "".join(self.after),
                                           "association": "following_context_candidate"})


def _pdf_table(page: Any, found: Any, table_id: str, number: int, opts: Options) -> dict:
    table = _table(table_id, number, "pdfplumber_geometry")
    boxes = sorted(set(tuple(float(v) for v in box) for box in found.cells))
    xs = sorted({v for box in boxes for v in (box[0], box[2])})
    ys = sorted({v for box in boxes for v in (box[1], box[3])})
    if len(boxes) > opts.max_cells or max(0, len(xs) - 1) * max(0, len(ys) - 1) > opts.max_cells:
        raise ParseError("PDF table grid exceeds max_cells")
    rows, columns = len(ys) - 1, len(xs) - 1
    matrix = [[None] * columns for _ in range(rows)]
    grid = [[None] * columns for _ in range(rows)]
    for box in boxes:
        r, c = ys.index(box[1]), xs.index(box[0])
        rs, cs = ys.index(box[3]) - r, xs.index(box[2]) - c
        chars = [char for char in page.chars if box[0] <= (char["x0"] + char["x1"]) / 2 < box[2]
                 and box[1] <= (char["top"] + char["bottom"]) / 2 < box[3]]
        extracted = page.crop(box, strict=False).extract_text() or ""
        cid = f"{table_id}-cell-{len(table['cells']) + 1}"
        cell = {"id": cid, "row": r, "column": c, "rowspan": rs, "colspan": cs,
                "bbox": list(box), "coordinates": "pdf_points_top_left",
                "raw_text": extracted, "raw_characters": chars,
                "text_reconstruction": "pdfplumber crop reading order; original character objects retained",
                "span_evidence": "geometric_edges", "kind": "unknown", "status": "pending_review"}
        table["cells"].append(cell)
        matrix[r][c] = extracted
        for rr in range(r, r + rs):
            for cc in range(c, c + cs):
                if grid[rr][cc] is not None:
                    raise ParseError("PDF geometric cells overlap; unresolved layout")
                grid[rr][cc] = cid
    table.update({"bbox": list(found.bbox), "raw_matrix": matrix, "grid": grid,
                  "extractor_matrix": found.extract(), "status": "extracted_pending_review"})
    x0, top, x1, bottom = found.bbox
    above = (page.crop((0, max(0, top - 70), page.width, top), strict=False).extract_text() or "") if top > 0 else ""
    below = (page.crop((0, bottom, page.width, min(page.height, bottom + 70)), strict=False).extract_text() or "") if bottom < page.height else ""
    if above:
        table["caption"] = {"raw_text": above, "association": "nearby_above_candidate"}
    if below:
        table["notes"] = [{"raw_text": below, "association": "nearby_below_candidate"}]
    table["unit_candidates"] = _units(" ".join(c["raw_text"] for c in table["cells"]))
    return table


def _continuations(tables: list[dict]) -> None:
    """Flag explicit continued captions + previous same table number, never merge."""
    for table in tables:
        caption = (table.get("caption") or {}).get("raw_text", "")
        if not re.search(r"\bcontinu(?:ed|ation)\b", caption, re.I):
            continue
        label = re.search(r"\btable\s+([A-Za-z]?\d+)\b", caption, re.I)
        if not label:
            continue
        candidates = []
        for earlier in tables:
            old = (earlier.get("caption") or {}).get("raw_text", "")
            old_label = re.search(r"\btable\s+([A-Za-z]?\d+)\b", old, re.I)
            if earlier["page"] == table["page"] - 1 and old_label and old_label[1].lower() == label[1].lower():
                if earlier["raw_matrix"] and table["raw_matrix"] and len(earlier["raw_matrix"][0]) == len(table["raw_matrix"][0]):
                    candidates.append(earlier["id"])
        table["continuation"] = {"status": "explicit_caption_candidate" if candidates else "unresolved",
                                 "candidate_ids": candidates, "merged": False,
                                 "headers_copied": False}


def parse_tsv(raw: str, width: int, height: int) -> list[dict]:
    columns = ["level", "page_num", "block_num", "par_num", "line_num", "word_num",
               "left", "top", "width", "height", "conf", "text"]
    # Tesseract emits literal text (including quotes), not CSV quoting.
    reader = csv.reader(io.StringIO(raw), delimiter="\t", quoting=csv.QUOTE_NONE)
    if next(reader, None) != columns:
        raise ParseError("Tesseract returned invalid TSV header")
    words = []
    seen_words: set[tuple[int, ...]] = set()
    for fields in reader:
        try:
            if len(fields) != len(columns):
                raise ValueError("TSV row must have exactly 12 columns")
            row = dict(zip(columns, fields))
            level = int(row["level"])
            hierarchy = tuple(int(row[k]) for k in columns[1:6])
            if not 1 <= level <= 5 or hierarchy[0] < 1 or any(v < 0 for v in hierarchy):
                raise ValueError("invalid hierarchy identifiers")
            # At each level its IDs are positive and all lower-level IDs zero.
            if any(v < 1 for v in hierarchy[:level]) or any(hierarchy[level:]):
                raise ValueError("invalid hierarchy identifiers for level")
            if level != 5:
                if row["text"]:
                    raise ValueError("text on a nonword TSV row")
                continue
            x, y, w, h = (int(row[k]) for k in ("left", "top", "width", "height"))
            conf = float(row["conf"])
            if min(x, y) < 0 or min(w, h) <= 0 or x + w > width or y + h > height or not math.isfinite(conf):
                raise ValueError("invalid bounds or confidence")
            if not -1 <= conf <= 100:
                raise ValueError("confidence outside range")
            if hierarchy in seen_words:
                raise ValueError("duplicate word hierarchy identifiers")
            seen_words.add(hierarchy)
            if not row["text"]:
                continue
            words.append({"raw_text": row["text"], "bbox_pixels": [x, y, x + w, y + h],
                          "confidence": conf, "line": [int(row[k]) for k in
                                                         ("page_num", "block_num", "par_num", "line_num")]})
        except (TypeError, ValueError, KeyError) as exc:
            raise ParseError(f"Invalid Tesseract word: {exc}") from exc
    return words


def _ocr_rows(words: list[dict], number: int) -> list[dict]:
    """Expose whitespace-separated OCR row groups as unresolved table candidates."""
    lines: dict[tuple, list] = {}
    for word in words:
        lines.setdefault(tuple(word["line"]), []).append(word)
    rows = []
    for line in lines.values():
        line.sort(key=lambda w: w["bbox_pixels"][0])
        groups: list[list[dict]] = []
        for word in line:
            if not groups or word["bbox_pixels"][0] - groups[-1][-1]["bbox_pixels"][2] > max(
                    12, 1.5 * (word["bbox_pixels"][3] - word["bbox_pixels"][1])):
                groups.append([])
            groups[-1].append(word)
        if len(groups) >= 2:
            rows.append(groups)
    if len(rows) < 2:
        return []
    table = _table(f"ocr-table-page-{number}", number, "ocr_whitespace_rows_heuristic")
    table["limitations"] = ["Row/column correspondence, merged cells, headers and footnotes unresolved",
                             "May be multi-column prose; never use as verified scientific table"]
    for r, row in enumerate(rows):
        texts, ids = [], []
        for c, group in enumerate(row):
            cid = f"{table['id']}-cell-{len(table['cells']) + 1}"
            boxes = [w["bbox_pixels"] for w in group]
            raw_text = " ".join(w["raw_text"] for w in group)
            table["cells"].append({"id": cid, "row": r, "column": c, "rowspan": None,
                                   "colspan": None, "raw_text": raw_text,
                                   "bbox": [min(b[0] for b in boxes), min(b[1] for b in boxes),
                                            max(b[2] for b in boxes), max(b[3] for b in boxes)],
                                   "coordinates": "image_pixels_top_left", "word_records": group,
                                   "span_evidence": "unresolved", "status": "unresolved"})
            texts.append(raw_text)
            ids.append(cid)
        table["raw_matrix"].append(texts)
        table["grid"].append(ids)
    return [table]


def _ocr_image(image: Any, number: int, opts: Options, deadline: float, mapper=None) -> dict:
    _check(deadline)
    if image.width * image.height > opts.max_pixels:
        raise ParseError("Rendered image exceeds max_pixels; reduce DPI or raise limit")
    executable = shutil.which(opts.tesseract)
    if not executable:
        raise ParseError("Tesseract executable missing; install Tesseract and language data, then use --tesseract PATH")
    with tempfile.TemporaryDirectory(prefix="literature-ocr-") as td:
        png = Path(td) / "page.png"
        image.save(png, format="PNG")
        # File-backed stdout avoids unbounded capture of a hostile/noisy engine.
        out_path, err_path = Path(td) / "engine.tsv", Path(td) / "engine.err"
        with out_path.open("wb") as out, err_path.open("wb") as err:
            try:
                process = subprocess.Popen([executable, str(png), "stdout", "-l", opts.language,
                                            "--dpi", str(opts.dpi), "tsv"], shell=False,
                                           stdout=out, stderr=err,
                                           start_new_session=os.name != "nt" and not _IN_WORKER)
                engine_deadline = min(deadline, time.monotonic() + opts.ocr_timeout)
                try:
                    while process.poll() is None:
                        if out_path.stat().st_size > opts.max_output_bytes or err_path.stat().st_size > opts.max_output_bytes:
                            raise ParseError("Tesseract output exceeds max_output_bytes")
                        if time.monotonic() >= engine_deadline:
                            raise ParseError("Tesseract exceeded OCR timeout")
                        time.sleep(min(.05, max(.001, engine_deadline - time.monotonic())))
                    completed_code = process.wait(timeout=5)
                finally:
                    if process.poll() is None:
                        _stop_tree(process.pid, process.kill, process_group=not _IN_WORKER)
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            # A tree tool can return success before termination,
                            # or fail to reap the exact process. Still enforce it.
                            process.kill()
                            process.wait(timeout=5)
            except OSError as exc:
                raise ParseError(f"Cannot start Tesseract: {exc}") from exc
        if out_path.stat().st_size > opts.max_output_bytes or err_path.stat().st_size > opts.max_output_bytes:
            raise ParseError("Tesseract output exceeds max_output_bytes")
        if completed_code:
            detail = err_path.read_bytes()[:2000].decode("utf-8", errors="replace")
            raise ParseError(f"Tesseract failed ({completed_code}): {detail}")
        raw = out_path.read_text(encoding="utf-8")
    words = parse_tsv(raw, image.width, image.height)
    if len(words) > opts.max_cells:
        raise ParseError("OCR word count exceeds max_cells")
    for word in words:
        if mapper:
            x0, y0, x1, y1 = word["bbox_pixels"]
            points = [mapper(x0, y0), mapper(x1, y1)]
            word["bbox_pdf_points_bottom_left"] = [min(p[0] for p in points), min(p[1] for p in points),
                                                   max(p[0] for p in points), max(p[1] for p in points)]
    mean = sum(w["confidence"] for w in words) / len(words) if words else None
    lines: dict[tuple, list[str]] = {}
    for word in words:
        lines.setdefault(tuple(word["line"]), []).append(word["raw_text"])
    return {"engine": "tesseract", "language": opts.language, "dpi": opts.dpi,
            "raw_tsv": raw, "text": "\n".join(" ".join(line) for line in lines.values()),
            "text_reconstruction": "TSV word order with inserted spaces/newlines; raw_tsv retained",
            "words": words, "mean_word_confidence": mean, "image_size_pixels": [image.width, image.height],
            "status": "pending_review", "quality": "noisy_or_empty" if mean is None or
            any(w["confidence"] < opts.min_ocr_confidence for w in words) else "confidence_only_not_validated",
            "table_candidates": _ocr_rows(words, number)}


def _ocr_pdf(path: Path, number: int, opts: Options, deadline: float) -> dict:
    pdfium = _dependency("pypdfium2", "ocr")
    _dependency("PIL.Image", "ocr")
    with pdfium.PdfDocument(str(path)) as pdf:
        page = pdf[number - 1]
        try:
            width, height = page.get_size()
            scale = opts.dpi / 72
            if math.ceil(width * scale) * math.ceil(height * scale) > opts.max_pixels:
                raise ParseError("PDF raster would exceed max_pixels; reduce DPI")
            bitmap = page.render(scale=scale)
            try:
                mapper = bitmap.get_posconv(page).to_page
                return _ocr_image(bitmap.to_pil(), number, opts, deadline, mapper)
            finally:
                bitmap.close()
        finally:
            page.close()


def parse_document(path: Path | str, options: Options | None = None) -> dict:
    """Extract locally. For untrusted documents use parse_bounded (also used by CLI)."""
    opts = options or Options()
    opts.validate()
    deadline = time.monotonic() + opts.max_seconds
    path = Path(os.path.abspath(path))
    if not path.is_file() or path.stat().st_size == 0:
        raise ParseError("Input must be a nonempty file")
    if path.stat().st_size > opts.max_input_bytes:
        raise ParseError("Input exceeds max_input_bytes")
    with path.open("rb") as source:
        raw = source.read(opts.max_input_bytes + 1)
    if len(raw) > opts.max_input_bytes:
        raise ParseError("Input exceeds max_input_bytes")
    source_hash = hashlib.sha256(raw).hexdigest()
    report = {"schema_version": "document-parse/1", "source": {"path": str(path),
              "sha256": source_hash, "bytes": len(raw)}, "options": asdict(opts),
              "status": "extracted_pending_review", "ai_status": "pending",
              "science_approval": "none", "human_review": None,
              "pages": [], "tables": [], "errors": [], "limitations": [
                  "Extraction is not scientific validation; units and values are never normalized",
                  "Missing cells stay null; candidate captions/notes and continuation are not asserted",
                  "PDF reading order and OCR/table recognition require human review"]}
    def add_tables(tables):
        existing = sum(max(len(t["cells"]), sum(len(row) for row in t["grid"])) for t in report["tables"])
        extra = sum(max(len(t["cells"]), sum(len(row) for row in t["grid"])) for t in tables)
        if existing + extra > opts.max_cells:
            raise ParseError("Document total cell/grid count exceeds max_cells")
        report["tables"].extend(tables)
    suffix = path.suffix.lower()
    if suffix in {".html", ".htm"}:
        selected_pages(opts.pages, 1, opts.max_pages)
        try:
            html = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ParseError("HTML must be UTF-8; convert a copy explicitly before parsing") from exc
        parser = _TablesHTML(html, opts, deadline)
        parser.feed(html)
        parser.finish()
        text = "".join(parser.page_text)
        if not text.strip() and not parser.tables:
            raise ParseError("HTML contains no extractable text or tables")
        report["pages"] = [{"page": 1, "status": "extracted_pending_review", "native": {
            "engine": "stdlib_html", "text": text, "source_html": html,
            "text_reconstruction": "Decoded HTML text with explicit br/block/table boundaries as newlines; source_html retained"}, "ocr": None}]
        report["tables"] = parser.tables
        if opts.ocr != "off":
            report["limitations"].append("OCR applies only to PDF/images; HTML linked images are not fetched")
    elif suffix == ".pdf":
        pdfplumber = _dependency("pdfplumber", "tables")
        try:
            with pdfplumber.open(io.BytesIO(raw)) as pdf:
                numbers = selected_pages(opts.pages, len(pdf.pages), opts.max_pages)
                report["total_pages"] = len(pdf.pages)
                for number in numbers:
                    _check(deadline)
                    page_report = {"page": number, "status": "extracted_pending_review", "native": None,
                                   "ocr": None, "errors": []}
                    report["pages"].append(page_report)
                    page = None
                    try:
                        page = pdf.pages[number - 1]
                        text = page.extract_text() or ""
                        page_report["size_pdf_points"] = [page.width, page.height]
                        page_report["rotation"] = page.rotation
                        page_report["native"] = {"engine": "pdfplumber", "text": text,
                                                  "characters": page.chars,
                                                  "coordinates": "pdf_points_top_left",
                                                  "text_reconstruction": "extractor reading order; original characters retained"}
                        settings = {"vertical_strategy": opts.table_strategy,
                                    "horizontal_strategy": opts.table_strategy}
                        for found in page.find_tables(settings):
                            add_tables([_pdf_table(page, found,
                                        f"table-{len(report['tables']) + 1}", number, opts)])
                    except Exception as exc:
                        page_report["errors"].append({"layer": "native", "error": str(exc)})
                    native_chars = len((page_report["native"] or {}).get("text", "").strip())
                    needs_ocr = opts.ocr == "always" or (opts.ocr == "auto" and native_chars < opts.min_native_chars)
                    if needs_ocr:
                        try:
                            page_report["ocr"] = _ocr_pdf(path, number, opts, deadline)
                            add_tables(page_report["ocr"]["table_candidates"])
                            if page_report["ocr"]["quality"] == "noisy_or_empty":
                                page_report["errors"].append({"layer": "ocr_quality", "error": "OCR empty or contains low-confidence words; unusable without review"})
                        except Exception as exc:
                            page_report["errors"].append({"layer": "ocr", "error": str(exc)})
                    if native_chars == 0 and not page_report["ocr"]:
                        page_report["errors"].append({"layer": "content", "error": "No text extracted; enable OCR for scanned pages"})
                    if page_report["errors"]:
                        page_report["status"] = "partial"
                        report["errors"].append({"page": number, "errors": page_report["errors"]})
                    if page is not None:
                        page.close()
        except ParseError:
            raise
        except Exception as exc:
            raise ParseError(f"Cannot parse PDF: {exc}") from exc
    elif suffix in {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}:
        if opts.ocr == "off":
            raise ParseError("Image inputs require --ocr auto or --ocr always")
        image_module = _dependency("PIL.Image", "ocr")
        with image_module.open(path) as image:
            total = getattr(image, "n_frames", 1)
            report["total_pages"] = total
            for number in selected_pages(opts.pages, total, opts.max_pages):
                _check(deadline)
                page_report = {"page": number, "native": None, "ocr": None, "status": "pending_review"}
                report["pages"].append(page_report)
                try:
                    image.seek(number - 1)
                    page_report["ocr"] = _ocr_image(image, number, opts, deadline)
                    add_tables(page_report["ocr"]["table_candidates"])
                    if page_report["ocr"]["quality"] == "noisy_or_empty":
                        page_report["status"] = "partial"
                        report["errors"].append({"page": number, "layer": "ocr_quality", "error": "OCR empty or low-confidence; requires review"})
                except Exception as exc:
                    page_report["status"] = "partial"
                    report["errors"].append({"page": number, "layer": "ocr", "error": str(exc)})
    else:
        raise ParseError("Supported inputs: PDF, UTF-8 HTML, PNG/JPEG/TIFF/BMP/WebP")
    _check(deadline)
    if sum(len(table["cells"]) for table in report["tables"]) > opts.max_cells:
        raise ParseError("Document total cell count exceeds max_cells")
    if path.stat().st_size != len(raw):
        raise ParseError("Source changed during extraction; output rejected")
    with path.open("rb") as source:
        current_bytes = source.read(opts.max_input_bytes + 1)
    if hashlib.sha256(current_bytes).hexdigest() != source_hash:
        raise ParseError("Source changed during extraction; output rejected")
    _continuations(report["tables"])
    for table in report["tables"]:
        table["source_sha256"] = source_hash
    if report["errors"]:
        report["status"] = "partial"
    if len((json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8")) > opts.max_output_bytes:
        raise ParseError("Extraction exceeds max_output_bytes; select fewer pages")
    return report


def _worker(connection, path: str, opts: Options) -> None:
    global _IN_WORKER
    _IN_WORKER = True
    if os.name != "nt":
        os.setsid()
    try:
        result = parse_document(path, opts)
    except Exception as exc:
        result = str(exc)
    try:
        connection.send((not isinstance(result, str), result))
    except (BrokenPipeError, EOFError, OSError):
        pass  # Parent has already enforced the hard timeout.
    finally:
        connection.close()


def parse_bounded(path: Path | str, options: Options | None = None) -> dict:
    """Hard wall-clock bound for native PDF/image adapters, in a spawned worker."""
    opts = options or Options()
    opts.validate()
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=_worker, args=(sender, str(path), opts))
    process.start()
    sender.close()
    try:
        if not receiver.poll(opts.max_seconds):
            raise ParseError("Document worker exceeded max_seconds (including startup)")
        try:
            okay, result = receiver.recv()
        except EOFError as exc:
            raise ParseError("Document worker exited without a result") from exc
        if not okay:
            raise ParseError(result)
        return result
    finally:
        receiver.close()
        if os.name != "nt" or process.is_alive():
            # Native engines may have descendants. Terminate this worker's exact
            # process tree, not a broad image-name match or unrelated engines.
            _stop_tree(process.pid, process.terminate)
        process.join(timeout=5)
        if process.is_alive():
            process.kill()
            process.join(timeout=5)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline PDF/HTML/image parsing; all evidence remains pending review")
    parser.add_argument("input", type=Path)
    parser.add_argument("--out-dir", required=True, type=Path, help="New output directory; existing paths are rejected")
    parser.add_argument("--ocr", choices=["off", "auto", "always"], default="off")
    parser.add_argument("--pages", default="", help="1-based pages/ranges, e.g. 1,3-5")
    parser.add_argument("--dpi", type=int, default=200)
    parser.add_argument("--language", default="eng")
    parser.add_argument("--tesseract", default="tesseract")
    parser.add_argument("--table-strategy", choices=["lines", "text"], default="lines")
    for name, kind in (("ocr_timeout", float), ("max_seconds", float), ("max_input_bytes", int),
                       ("max_output_bytes", int), ("max_pages", int), ("max_cells", int),
                       ("max_span", int), ("max_pixels", int), ("min_native_chars", int),
                       ("min_ocr_confidence", float)):
        parser.add_argument("--" + name.replace("_", "-"), type=kind, default=getattr(Options(), name))
    args = parser.parse_args(argv)
    try:
        args.out_dir = Path(os.path.abspath(args.out_dir))
        _fresh_output(args.out_dir)
        opts = Options(**{name: getattr(args, name) for name in Options.__dataclass_fields__})
        report = parse_bounded(args.input, opts)
        _fresh_output(args.out_dir)
        args.out_dir.mkdir(parents=True, exist_ok=False)
        with (args.out_dir / "document.json").open("x", encoding="utf-8", newline="\n") as output:
            json.dump(report, output, ensure_ascii=False, indent=2)
            output.write("\n")
        print(json.dumps({"output": str(args.out_dir / 'document.json'),
                          "status": report["status"], "pages": len(report["pages"]),
                          "tables": len(report["tables"])}, ensure_ascii=False))
        return 3 if report["status"] == "partial" else 0
    except (OSError, ParseError) as exc:
        print(f"literature-parse: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
