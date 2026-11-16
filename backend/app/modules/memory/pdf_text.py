from __future__ import annotations

import json
import os
import tempfile
from importlib.metadata import version
from pathlib import Path


class PdfTextError(ValueError):
    pass


def read_pdf_pages(path: Path) -> list[str]:
    """Read bounded PDF text layers without OCR."""
    try:
        import pypdfium2 as pdfium

        document = pdfium.PdfDocument(path)
        try:
            if len(document) > 2000:
                raise PdfTextError("PDF has too many pages")
            pages = []
            size = 0
            for page in document:
                try:
                    textpage = page.get_textpage()
                    try:
                        text = textpage.get_text_range()
                    finally:
                        textpage.close()
                finally:
                    page.close()
                size += len(text)
                if size > 2_000_000:
                    raise PdfTextError("PDF text is too large")
                pages.append(text)
            return pages
        finally:
            document.close()
    except Exception as exc:
        raise PdfTextError("PDF text could not be read") from exc


def cached_pdf_pages(path: Path) -> list[str]:
    """Read or atomically rebuild the derived cache next to the stored PDF."""
    try:
        extractor_version = version("pypdfium2")
        cache = path.with_suffix(".pages.json")
        if not cache.is_symlink():
            try:
                data = json.loads(cache.read_text(encoding="utf-8"))
                pages = data["pages"]
                if (data["extractor"] == "pypdfium2" and data["extractor_version"] == extractor_version
                        and isinstance(pages, list) and len(pages) <= 2000
                        and all(isinstance(page, str) for page in pages)
                        and sum(map(len, pages)) <= 2_000_000):
                    return pages
            except (OSError, ValueError, KeyError, TypeError):
                pass
        pages = read_pdf_pages(path)
        if not any(page.strip() for page in pages):
            pages = []
        handle, name = tempfile.mkstemp(dir=path.parent, prefix=".pages-", suffix=".part")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump({"extractor": "pypdfium2", "extractor_version": extractor_version, "pages": pages}, stream)
            os.replace(name, cache)
        finally:
            Path(name).unlink(missing_ok=True)
        return pages
    except Exception as exc:
        raise PdfTextError("PDF text could not be read") from exc
