#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
from collections import defaultdict
from collections.abc import Iterator, Sequence
from pathlib import Path

from litsearch.core import classify_and_score, norm_doi, passes_core_relevance
from litsearch.models import Record
from litsearch.outputs import excel_safe


FIELDS = ["title", "DOI", "date", "publicationTitle", "abstractNote", "url"]


def connect_readonly(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
    con.row_factory = sqlite3.Row
    return con


def normalize_doi(value: str) -> str:
    return norm_doi(value)


def doi_filename(doi: str) -> str:
    return re.sub(r"[^a-z0-9._-]+", "_", normalize_doi(doi))


def is_valid_pdf(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return path.stat().st_size > 1_000 and handle.read(4) == b"%PDF"
    except OSError:
        return False


def chunks(values: Sequence[int], size: int = 800) -> Iterator[Sequence[int]]:
    for offset in range(0, len(values), size):
        yield values[offset : offset + size]


def index_doi_pdfs(pdfs: list[Path]) -> dict[str, str]:
    index: dict[str, str] = {}
    for pdf in pdfs:
        normalized_stem = re.sub(r"[^a-z0-9._-]+", "_", pdf.stem.lower())
        if normalized_stem and normalized_stem not in index and is_valid_pdf(pdf):
            index[normalized_stem] = str(pdf)
    return index


def find_doi_pdf(doi: str, pdf_index: dict[str, str]) -> str:
    if not doi:
        return ""
    needle = doi_filename(doi)
    if not needle:
        return ""
    return pdf_index.get(needle, "")


def audit(zotero_dir: Path, search_roots: list[Path]) -> list[dict[str, object]]:
    with connect_readonly(zotero_dir / "zotero.sqlite") as con:
        cur = con.cursor()
        parents = cur.execute(
            """
            select i.itemID, i.key, t.typeName
            from items i
            join itemTypes t on t.itemTypeID = i.itemTypeID
            where t.typeName not in ('attachment', 'note', 'annotation')
              and i.itemID not in (select itemID from deletedItems)
            """
        ).fetchall()
        item_ids = [row["itemID"] for row in parents]
        if not item_ids:
            return []
        fields: dict[int, dict[str, str]] = defaultdict(dict)
        collections: dict[int, list[str]] = defaultdict(list)
        attachments: dict[int, list[str]] = defaultdict(list)
        for item_chunk in chunks(item_ids):
            placeholders = ",".join("?" for _ in item_chunk)
            field_rows = cur.execute(
                f"""
                select d.itemID, f.fieldName, v.value
                from itemData d
                join fields f on f.fieldID = d.fieldID
                join itemDataValues v on v.valueID = d.valueID
                where d.itemID in ({placeholders})
                  and f.fieldName in ({",".join("?" for _ in FIELDS)})
                """,
                [*item_chunk, *FIELDS],
            ).fetchall()
            for row in field_rows:
                fields[row["itemID"]][row["fieldName"]] = row["value"]

            collection_rows = cur.execute(
                f"""
                select ci.itemID, c.collectionName
                from collectionItems ci
                join collections c on c.collectionID = ci.collectionID
                where ci.itemID in ({placeholders})
                """,
                item_chunk,
            ).fetchall()
            for row in collection_rows:
                collections[row["itemID"]].append(row["collectionName"])

            attachment_rows = cur.execute(
                f"""
                select a.parentItemID, a.path, a.contentType, i.key attachmentKey
                from itemAttachments a
                join items i on i.itemID = a.itemID
                where a.parentItemID in ({placeholders})
                  and lower(coalesce(a.contentType, '')) like '%pdf%'
                """,
                item_chunk,
            ).fetchall()
            for row in attachment_rows:
                value = row["path"] or ""
                path = (
                    zotero_dir / "storage" / row["attachmentKey"] / value.split(":", 1)[1]
                    if value.startswith("storage:")
                    else Path(value)
                )
                if is_valid_pdf(path):
                    attachments[row["parentItemID"]].append(str(path))

    pdfs = []
    for root in search_roots:
        if root.exists():
            pdfs.extend(root.rglob("*.pdf"))
    pdf_index = index_doi_pdfs(pdfs)

    rows = []
    for parent in parents:
        meta = fields[parent["itemID"]]
        rec = Record(
            title=meta.get("title", ""),
            doi=normalize_doi(meta.get("DOI", "")),
            year=meta.get("date", "")[:4],
            journal=meta.get("publicationTitle", ""),
            abstract=meta.get("abstractNote", ""),
            url=meta.get("url", ""),
        )
        classify_and_score(rec)
        existing = attachments[parent["itemID"]]
        rows.append(
            {
                "itemID": parent["itemID"],
                "itemKey": parent["key"],
                "title": rec.title,
                "doi": rec.doi,
                "date": meta.get("date", ""),
                "collections": "; ".join(collections[parent["itemID"]]),
                "relevance_score": rec.relevance_score,
                "core_relevant": passes_core_relevance(rec),
                "has_pdf": bool(existing),
                "zotero_pdf": existing[0] if existing else "",
                "matched_external_pdf": "" if existing else find_doi_pdf(rec.doi, pdf_index),
                "abstract": rec.abstract,
                "url": rec.url,
            }
        )
    return sorted(
        rows,
        key=lambda row: (
            bool(row["core_relevant"]),
            not bool(row["has_pdf"]),
            int(row["relevance_score"]),
        ),
        reverse=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--zotero-dir", default=str(Path.home() / "Zotero"))
    parser.add_argument("--out-dir", default="outputs_zotero_audit")
    parser.add_argument("--search-root", action="append", default=[])
    args = parser.parse_args()

    zotero_dir = Path(args.zotero_dir)
    roots = [Path(path) for path in args.search_root]
    rows = audit(zotero_dir, roots)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    (out_dir / "zotero_pdf_audit.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    fieldnames = (
        [key for key in rows[0] if key != "abstract"]
        if rows
        else [
            "itemID",
            "itemKey",
            "title",
            "doi",
            "date",
            "collections",
            "relevance_score",
            "core_relevant",
            "has_pdf",
            "zotero_pdf",
            "matched_external_pdf",
            "url",
        ]
    )
    with (out_dir / "zotero_pdf_audit.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(
            {key: excel_safe(value) for key, value in row.items()}
            for row in rows
        )

    missing = [row for row in rows if row["core_relevant"] and not row["has_pdf"]]
    (out_dir / "missing_core_records.json").write_text(
        json.dumps(missing, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "all_items": len(rows),
                "core_relevant": sum(bool(row["core_relevant"]) for row in rows),
                "core_missing_pdf": len(missing),
                "core_missing_with_external_match": sum(
                    bool(row["matched_external_pdf"]) for row in missing
                ),
                "out_dir": str(out_dir.resolve()),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
