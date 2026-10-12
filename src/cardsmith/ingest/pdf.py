"""PDF text extraction through pymupdf."""

from __future__ import annotations

import pymupdf

from cardsmith.ingest.chunking import Chunk, merge_units


def extract_pdf(path: str, *, warnings: list[str] | None = None) -> list[Chunk]:
    """Extract page text, optionally collecting warnings about omitted PDF content.

    Images and vector graphics are not interpreted. Equations are included only
    as extracted text, which may lose their layout or symbols.
    """
    units: list[tuple[str, str]] = []
    with pymupdf.open(path) as doc:
        for i, page in enumerate(doc, start=1):
            text = page.get_text("text")
            if warnings is not None:
                if not text.strip():
                    warnings.append(
                        f"PDF page {i} has no extractable text and was skipped. "
                        "For scanned text, run OCR first. Transcribe image-only equations "
                        "or describe diagrams in a text file before generating cards."
                    )
                elif page.get_image_info() or page.get_drawings():
                    warnings.append(
                        f"PDF page {i} contains images or vector graphics. Cards use extracted "
                        "text only. Image-only text, equations and diagrams are not interpreted. "
                        "Review this page and transcribe or describe missing content in a text file."
                    )
            units.append((f"page {i}", text))
    return merge_units(units)
