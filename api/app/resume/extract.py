"""Local PDF text extraction and deterministic normalization.

Pages are extracted locally in source page order without OCR, a model call,
an external converter, or a network service. The extractor fails closed on
malformed, encrypted, password-protected, or extractor-error PDFs and never
silently drops a page index: every reported page must be accounted for in the
returned list, with empty strings (not skips) for otherwise unreadable pages
so the completeness gate can reject them.

``content_hash`` is lowercase-hex SHA-256 over the exact UTF-8 bytes of the
final normalized Markdown. ``raw_sha256`` is lowercase-hex SHA-256 over the
exact raw response bytes and is computed by the service before extraction.
"""

from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import dataclass

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from app.resume.normalize import normalize_markdown

PDF_PARSE_ERROR = "extraction.pdf_parse_error"
PDF_ENCRYPTED = "extraction.pdf_encrypted"
PAGE_MISSING = "extraction.page_missing"
PAGE_NO_LETTERS_OR_NUMBERS = "extraction.page_no_letters_or_numbers"
DOCUMENT_EMPTY = "extraction.document_empty"
DOCUMENT_TOO_FEW_LETTERS = "extraction.document_too_few_letters"
DOCUMENT_TOO_FEW_TOKENS = "extraction.document_too_few_tokens"


@dataclass(frozen=True)
class ExtractionRejection:
    code: str
    page_count: int | None = None
    page_index: int | None = None
    byte_count: int | None = None
    content_char_count: int | None = None
    content_token_count: int | None = None


@dataclass(frozen=True)
class ExtractionResult:
    """Successful extraction of a complete PDF.

    ``page_normalized`` carries the per-page normalized strings in source page
    order. ``content_md`` is the deterministic Markdown for the complete
    document with page boundaries preserved as blank lines. ``content_hash``
    is lowercase-hex SHA-256 over the UTF-8 bytes of ``content_md``.
    """

    page_count: int
    page_normalized: tuple[str, ...]
    content_md: str
    content_hash: str
    content_char_count: int
    content_token_count: int


def extract_and_normalize(raw_bytes: bytes) -> ExtractionResult | ExtractionRejection:
    """Extract every page in order and run the completeness gate.

    Returns :class:`ExtractionRejection` for any rejection path. Returns
    :class:`ExtractionResult` only when every reported page is accounted for
    and the document clears the completeness minimums.
    """

    try:
        reader = PdfReader(stream_from_memory(raw_bytes))
    except PdfReadError:
        return ExtractionRejection(code=PDF_PARSE_ERROR, byte_count=len(raw_bytes))
    except Exception:  # noqa: BLE001 - pypdf raises a grab-bag of error types
        return ExtractionRejection(code=PDF_PARSE_ERROR, byte_count=len(raw_bytes))

    if reader.is_encrypted:
        return ExtractionRejection(code=PDF_ENCRYPTED, byte_count=len(raw_bytes))

    pages = reader.pages
    page_count = len(pages)
    if page_count == 0:
        return ExtractionRejection(code=DOCUMENT_EMPTY, page_count=0)

    page_texts: list[str] = []
    for index, page in enumerate(pages):
        try:
            raw_text = page.extract_text() or ""
        except PdfReadError:
            return ExtractionRejection(
                code=PAGE_MISSING,
                page_index=index,
                page_count=page_count,
                byte_count=len(raw_bytes),
            )
        except Exception:  # noqa: BLE001
            return ExtractionRejection(
                code=PAGE_MISSING,
                page_index=index,
                page_count=page_count,
                byte_count=len(raw_bytes),
            )
        normalized = normalize_markdown(raw_text)
        if _letter_or_number_count(normalized) == 0:
            return ExtractionRejection(
                code=PAGE_NO_LETTERS_OR_NUMBERS,
                page_index=index,
                page_count=page_count,
                byte_count=len(raw_bytes),
            )
        page_texts.append(normalized)

    content_md = _join_pages(page_texts)
    content_hash = _sha256_hex(content_md.encode("utf-8"))
    char_count = _letter_or_number_count(content_md)
    token_count = _letter_or_number_token_count(content_md)

    if char_count == 0:
        return ExtractionRejection(
            code=DOCUMENT_EMPTY,
            page_count=page_count,
            byte_count=len(raw_bytes),
            content_char_count=0,
            content_token_count=0,
        )
    if char_count < MIN_LETTERS_OR_NUMBERS:
        return ExtractionRejection(
            code=DOCUMENT_TOO_FEW_LETTERS,
            page_count=page_count,
            byte_count=len(raw_bytes),
            content_char_count=char_count,
            content_token_count=token_count,
        )
    if token_count < MIN_TOKENS:
        return ExtractionRejection(
            code=DOCUMENT_TOO_FEW_TOKENS,
            page_count=page_count,
            byte_count=len(raw_bytes),
            content_char_count=char_count,
            content_token_count=token_count,
        )

    return ExtractionResult(
        page_count=page_count,
        page_normalized=tuple(page_texts),
        content_md=content_md,
        content_hash=content_hash,
        content_char_count=char_count,
        content_token_count=token_count,
    )


def stream_from_memory(raw_bytes: bytes):
    """Return a pypdf-compatible bytes stream over ``raw_bytes``."""

    import io

    return io.BytesIO(raw_bytes)


def _join_pages(page_texts: list[str]) -> str:
    # Page boundaries are preserved as a single blank line between pages.
    # Each page_text already ends with a single trailing newline.
    joined = []
    for index, page_text in enumerate(page_texts):
        if index > 0:
            joined.append("")  # blank-line page boundary
        joined.append(page_text.rstrip("\n"))
    return "\n".join(joined) + "\n"


def sha256_hex_of(data: bytes) -> str:
    """Lowercase hexadecimal SHA-256 over the exact raw bytes."""

    return _sha256_hex(data)


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------
# Completeness minimums and counting helpers.
# ---------------------------------------------------------------------
# Task 003 names these two constants explicitly and forbids basing
# acceptance on someone's name, headings, or production wording; the letters
# and tokens counted here are Unicode-general and content-agnostic.
MIN_LETTERS_OR_NUMBERS = 500
MIN_TOKENS = 75


def _letter_or_number_count(text: str) -> int:
    return sum(1 for ch in text if _is_letter_or_number(ch))


def _letter_or_number_token_count(text: str) -> int:
    count = 0
    in_token = False
    for ch in text:
        if _is_letter_or_number(ch):
            if not in_token:
                count += 1
                in_token = True
        else:
            in_token = False
    return count


def _is_letter_or_number(ch: str) -> bool:
    return unicodedata.category(ch)[0] in {"L", "N"}
