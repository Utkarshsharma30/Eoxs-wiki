"""Extracts readable text from downloaded attachment bytes. Supports the
formats that actually show up in real ingested mail in meaningful volume
(checked live against real data before choosing this list): pdf, docx,
xlsx, csv. Legacy .doc/.xls (pre-2007 binary Office formats) and images
are explicitly out of scope -- .doc/.xls would need a different library
for a handful of real instances, and images need OCR, a genuinely
separate project. Both degrade to a clear, honest "not supported" note
rather than silently doing nothing.

Never raises -- a single bad attachment (corrupted file, unexpected
internal structure, password-protected) degrades to a short bracketed
note stored in the same place real extracted text would go, so a caller
never needs a separate try/except and a query for "did extraction
actually happen" is just "is extracted_text NULL" (not attempted, e.g.
an unsupported binary format skipped before download) vs "here's what
happened" (a real value, success or an explained failure).
"""
import csv
import io
import logging

logger = logging.getLogger("ingestion.attachment_extract")

MAX_EXTRACT_CHARS = 50000  # generous cap -- keeps one huge spreadsheet from dominating a row indefinitely

SUPPORTED_EXTENSIONS = {"pdf", "docx", "xlsx", "csv"}


def _extract_pdf(data):
    import pypdf
    reader = pypdf.PdfReader(io.BytesIO(data))
    pages = []
    for i, page in enumerate(reader.pages):
        text = (page.extract_text() or "").strip()
        if text:
            pages.append(f"--- page {i + 1} ---\n{text}")
    return "\n\n".join(pages)


def _extract_docx(data):
    import docx
    doc = docx.Document(io.BytesIO(data))
    parts = [p.text for p in doc.paragraphs if p.text.strip()]
    for table in doc.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells]
            if any(cells):
                parts.append(" | ".join(cells))
    return "\n".join(parts)


def _extract_xlsx(data):
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    sections = []
    for sheet in wb.worksheets:
        rows = []
        for row in sheet.iter_rows(values_only=True):
            if any(c is not None for c in row):
                rows.append(" | ".join("" if c is None else str(c) for c in row))
        if rows:
            sections.append(f"--- sheet: {sheet.title} ---\n" + "\n".join(rows))
    return "\n\n".join(sections)


def _extract_csv(data):
    text = data.decode("utf-8", errors="replace")
    reader = csv.reader(io.StringIO(text))
    return "\n".join(" | ".join(row) for row in reader)


_EXTRACTORS = {
    "pdf": _extract_pdf,
    "docx": _extract_docx,
    "xlsx": _extract_xlsx,
    "csv": _extract_csv,
}


def extractable(filename):
    """Cheap pre-check, called before spending an API call downloading
    the actual bytes -- no point fetching a binary this module can't do
    anything with."""
    ext = (filename or "").rsplit(".", 1)[-1].lower() if "." in (filename or "") else ""
    return ext in SUPPORTED_EXTENSIONS


def extract_text(filename, data):
    """Returns extracted text, or a short bracketed explanatory note --
    never raises, never returns None or empty."""
    ext = (filename or "").rsplit(".", 1)[-1].lower() if "." in (filename or "") else ""
    extractor = _EXTRACTORS.get(ext)
    if not extractor:
        if ext in ("xls", "doc"):
            return f"[legacy .{ext} format not supported for extraction -- re-save as .{ext}x to enable this]"
        return f"[unsupported format for text extraction: .{ext or 'unknown'}]"
    try:
        text = extractor(data)
        # Postgres text columns reject embedded NUL bytes outright ("A
        # string literal cannot contain NUL (0x00) characters") -- some
        # malformed/corrupted source PDFs decode to text containing them.
        # Stripping here (not just at the DB-write call site) means every
        # caller gets a value that's always safely storable.
        text = text.replace("\x00", "")
        if not text.strip():
            return "[no extractable text found -- file may be empty, scanned/image-based, or password-protected]"
        return text[:MAX_EXTRACT_CHARS]
    except Exception as e:
        logger.warning("attachment extraction failed for %s: %s", filename, e)
        return f"[extraction failed: {type(e).__name__}]"
