"""PDF text extraction and page rendering via PyMuPDF."""

from __future__ import annotations

from collections.abc import Iterable

import pymupdf

from shelfsort.tools import ToolError


def _open(path: str):
    try:
        return pymupdf.open(path)
    except Exception as e:
        raise ToolError(f"cannot open PDF {path}: {e}") from e


def page_count(path: str) -> int:
    doc = _open(path)
    try:
        return doc.page_count
    except Exception as e:
        raise ToolError(f"cannot read PDF {path}: {e}") from e
    finally:
        doc.close()


def page_text(path: str, pages: Iterable[int]) -> str:
    """Text of the given 0-based pages; out-of-range indexes are skipped."""
    doc = _open(path)
    try:
        n = doc.page_count
        return "\n".join(doc[i].get_text() for i in pages if 0 <= i < n)
    except Exception as e:
        raise ToolError(f"cannot read PDF {path}: {e}") from e
    finally:
        doc.close()


def render_page_png(path: str, page: int, dpi: int = 200) -> bytes:
    doc = _open(path)
    try:
        return doc[page].get_pixmap(dpi=dpi).tobytes("png")
    except Exception as e:
        raise ToolError(f"cannot render page {page} of {path}: {e}") from e
    finally:
        doc.close()
