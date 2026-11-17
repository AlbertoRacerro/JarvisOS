"""Isolated Docling entrypoint. The API starts this only for a validated local PDF."""
from __future__ import annotations

import json
import resource
import sys
from pathlib import Path

# Fixed limits, applied before Docling is imported so they cover model loading too.
ADDRESS_SPACE = 12 * 1024**3
CPU_SECONDS = 90
OUTPUT_BYTES = 2 * 1024**2
MAX_BLOCKS = 1500
MAX_UNLOCATED = 200


def apply_limits() -> None:
    for name, value in ((resource.RLIMIT_AS, ADDRESS_SPACE), (resource.RLIMIT_CPU, CPU_SECONDS),
                        (resource.RLIMIT_FSIZE, OUTPUT_BYTES)):
        resource.setrlimit(name, (value, value))


def _table_block(item: object, document: object) -> dict:
    grid: list[list[str]] = []
    for cell in getattr(getattr(item, "data", None), "table_cells", []):
        row = int(getattr(cell, "start_row_offset_idx", 0))
        col = int(getattr(cell, "start_col_offset_idx", 0))
        if not 0 <= row < 100 or not 0 <= col < 30:
            continue
        while len(grid) <= row:
            grid.append([])
        while len(grid[row]) <= col:
            grid[row].append("")
        grid[row][col] = str(getattr(cell, "text", ""))[:500]
    caption = item.caption_text(document) if hasattr(item, "caption_text") else ""
    return {"kind": "table", "text": str(caption)[:4000], "rows": grid}


def main(path: Path, artifacts: Path) -> None:
    apply_limits()
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    options = PdfPipelineOptions(artifacts_path=artifacts, do_ocr=False, do_table_structure=True)
    converter = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)})
    document = converter.convert(path).document
    pages: dict[int, list[dict]] = {}
    unlocated: list[dict] = []
    total = 0
    for item, _ in document.iterate_items():
        if total >= MAX_BLOCKS:
            break
        if getattr(item, "label", None) == "table":
            block = _table_block(item, document)
        else:
            text = str(getattr(item, "text", ""))[:4000]
            if not text:
                continue
            block = {"kind": "text", "text": text}
        provenance = getattr(item, "prov", None) or []
        page = getattr(provenance[0], "page_no", None) if provenance else None
        if isinstance(page, int) and 1 <= page <= 200:
            pages.setdefault(page, []).append(block)
        elif len(unlocated) < MAX_UNLOCATED:
            unlocated.append(block)
        else:
            continue
        total += 1
    result = {"pages": [{"page": page, "blocks": blocks} for page, blocks in sorted(pages.items())],
              "unlocated": unlocated}
    sys.stdout.write(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
