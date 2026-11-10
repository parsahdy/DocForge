"""Parsers. Each yields small text blocks, so memory stays bounded wherever the library allows.

* txt, xlsx: truly streaming (line by line, row by row).
* pdf: page by page, but the library keeps the document structure in memory.
* docx: the library loads the whole document into memory (a known limit, measured in Stage 3).
"""

from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from app.core.errors import PermanentError

SUPPORTED_EXTENSIONS = frozenset({".txt", ".pdf", ".docx", ".xlsx"})

_MAX_BLOCK_CHARS = 8000
_MAX_LINE_CHARS = 1_000_000  # a single enormous line must not become a single enormous block


@dataclass(frozen=True)
class Block:
    loc: str
    text: str


def _batched(parts: Iterable[str], label: str) -> Iterator[Block]:
    buffer: list[str] = []
    size = 0
    number = 0
    for part in parts:
        buffer.append(part)
        size += len(part) + 1
        if size >= _MAX_BLOCK_CHARS:
            number += 1
            yield Block(f"{label} {number}", "\n".join(buffer))
            buffer, size = [], 0
    if buffer:
        number += 1
        yield Block(f"{label} {number}", "\n".join(buffer))


def _check_magic(path: Path, ext: str) -> None:
    """Trust the content, not the extension."""
    with path.open("rb") as handle:
        head = handle.read(4096)
    if ext == ".pdf" and b"%PDF-" not in head[:1024]:
        raise PermanentError("TYPE_MISMATCH", "file does not look like a PDF")
    if ext in {".docx", ".xlsx"} and not head.startswith(b"PK\x03\x04"):
        raise PermanentError("TYPE_MISMATCH", f"file does not look like a {ext} document")
    if ext == ".txt" and b"\x00" in head:
        raise PermanentError("TYPE_MISMATCH", "file looks binary, not text")


def _parse_txt(path: Path) -> Iterator[Block]:
    def lines() -> Iterator[str]:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            while True:
                line = handle.readline(_MAX_LINE_CHARS)
                if not line:
                    return
                line = line.rstrip("\r\n")
                if line.strip():
                    yield line

    yield from _batched(lines(), "text part")


def _parse_pdf(path: Path) -> Iterator[Block]:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    if reader.is_encrypted:
        try:
            opened = reader.decrypt("")  # some PDFs are "encrypted" with an empty password
        except Exception as exc:
            raise PermanentError("ENCRYPTED", "PDF is encrypted") from exc
        if not opened:
            raise PermanentError("ENCRYPTED", "PDF is password protected")
    for number, page in enumerate(reader.pages, start=1):
        text = (page.extract_text() or "").strip()
        if text:
            yield Block(f"page {number}", text)


def _parse_docx(path: Path) -> Iterator[Block]:
    from docx import Document
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    document = Document(str(path))

    def pieces() -> Iterator[str]:
        for child in document.element.body.iterchildren():  # keeps paragraphs and tables in order
            if child.tag == qn("w:p"):
                text = Paragraph(child, document).text.strip()
                if text:
                    yield text
            elif child.tag == qn("w:tbl"):
                for row in Table(child, document).rows:
                    cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
                    if cells:
                        yield " | ".join(cells)

    yield from _batched(pieces(), "body part")


def _parse_xlsx(path: Path) -> Iterator[Block]:
    from openpyxl import load_workbook

    workbook = load_workbook(str(path), read_only=True, data_only=True)  # read_only = streaming
    try:
        for sheet in workbook.worksheets:

            def rows(sheet=sheet) -> Iterator[str]:
                for row in sheet.iter_rows(values_only=True):
                    cells = [str(v).strip() for v in row if v is not None and str(v).strip()]
                    if cells:
                        yield " | ".join(cells)

            yield from _batched(rows(), f"sheet {sheet.title} part")
    finally:
        workbook.close()


_PARSERS: dict[str, Callable[[Path], Iterator[Block]]] = {
    ".txt": _parse_txt,
    ".pdf": _parse_pdf,
    ".docx": _parse_docx,
    ".xlsx": _parse_xlsx,
}


def parse_file(path: Path, filename: str) -> Iterator[Block]:
    ext = PurePosixPath(filename).suffix.lower()
    parser = _PARSERS.get(ext)
    if parser is None:
        raise PermanentError("UNSUPPORTED_TYPE", f"unsupported file type: {ext or 'none'}")
    _check_magic(path, ext)
    try:
        yield from parser(path)
    except (PermanentError, MemoryError):
        raise
    except Exception as exc:  # a library failing on a local file means the file is bad
        raise PermanentError("CORRUPT_FILE", f"could not read {ext} file: {type(exc).__name__}") from exc
