"""File content parsing — ported from core/message-handler.ts:parseFileContent().

Parses uploaded file contents for injection into LLM context.
Supports DOCX, PDF, and plain text formats, with automatic
encoding fallback for text files.

All optional dependencies (``python-docx``, ``pypdf``, ``PyMuPDF``) are
handled via `try/except ImportError` — missing libraries produce a
descriptive placeholder string instead of crashing.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import NamedTuple, Optional


class ParsedContent(NamedTuple):
    """Parsed file content result.

    Attributes:
        text: Extracted text content.
        format: File format identifier (``"docx"`` / ``"pdf"`` / ``"text"``).
        file_name: Original file name.
        file_size: File size in bytes.
    """

    text: str
    format: str
    file_name: str
    file_size: int


async def parse_file_content(file_path: str) -> Optional[ParsedContent]:
    """Parse file content based on file extension.

    Args:
        file_path: Absolute path to the file.

    Returns:
        A :class:`ParsedContent` named tuple, or ``None`` if the file
        does not exist or cannot be read.
    """
    path = Path(file_path)
    if not path.exists():
        return None

    ext = path.suffix.lower()
    file_name = path.name
    file_size = path.stat().st_size

    if ext == ".docx":
        text = await _parse_docx(path)
        return ParsedContent(text=text, format="docx", file_name=file_name, file_size=file_size)

    if ext == ".pdf":
        text = await _parse_pdf(path)
        return ParsedContent(text=text, format="pdf", file_name=file_name, file_size=file_size)

    text = await _read_text(path)
    return ParsedContent(text=text, format="text", file_name=file_name, file_size=file_size)


async def _parse_docx(path: Path) -> str:
    """Parse a ``.docx`` file and return its text content.

    Requires: ``python-docx`` (included in project dependencies).
    """
    try:
        import docx

        doc = await asyncio.to_thread(docx.Document, str(path))
        paragraphs = [p.text for p in doc.paragraphs]
        return "\n".join(paragraphs)
    except ImportError:
        return "[DOCX parsing unavailable: python-docx not installed]"
    except Exception:
        return "[DOCX parsing error]"


async def _parse_pdf(path: Path) -> str:
    """Parse a PDF file and return its text content.

    Tries ``pypdf`` first (project dependency), then falls back to
    ``PyMuPDF`` (``fitz``) if available.
    """
    # Try pypdf first (primary dependency)
    try:
        import pypdf

        reader = await asyncio.to_thread(pypdf.PdfReader, str(path))
        pages: list[str] = []
        for page in reader.pages:
            text = page.extract_text()
            if text:
                pages.append(text)
        return "\n\n".join(pages)
    except ImportError:
        pass
    except Exception:
        return "[PDF parsing error]"

    # Fallback: PyMuPDF
    try:
        import fitz  # type: ignore[import-untyped]

        doc = await asyncio.to_thread(fitz.open, str(path))
        pages = []
        for page in doc:
            pages.append(page.get_text())
        return "\n\n".join(pages)
    except ImportError:
        return "[PDF parsing unavailable: install pypdf or PyMuPDF]"


async def _read_text(path: Path) -> str:
    """Read a plain text file with encoding fallback.

    Tries UTF-8 first, then GBK (common on Windows), then Latin-1 (never
    raises on decode).
    """
    try:
        return await asyncio.to_thread(path.read_text, encoding="utf-8")
    except UnicodeDecodeError:
        pass
    try:
        return await asyncio.to_thread(path.read_text, encoding="gbk")
    except UnicodeDecodeError:
        pass
    return await asyncio.to_thread(path.read_text, encoding="latin-1")


__all__ = ["ParsedContent", "parse_file_content"]
