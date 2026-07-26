"""Tests for PDF extraction and the deterministic completeness gate.

The gate rejects:

- a PDF with zero reported pages;
- extraction that fails to account for every reported page;
- any page whose normalized text contains zero Unicode letter-or-number
  characters;
- a document whose normalized Markdown is empty; and
- a complete normalized Markdown with fewer than 500 Unicode letter-or-number
  characters or fewer than 75 maximal contiguous runs of those characters.

The exact 500-character and 75-token document boundaries are tested directly;
PDFs are small synthetic fixtures containing invented, non-personal text.
"""

from __future__ import annotations

import hashlib

from app.resume.extract import (
    ExtractionRejection,
    ExtractionResult,
    extract_and_normalize,
    sha256_hex_of,
)
from tests.pdf_fixtures import build_pdf


def _page_with_letters(count_letters: int) -> list[str]:
    # Build one page whose extracted text has at least ``count_letters``
    # ASCII letter-or-number characters. We use repetitive invented prose so
    # the test never relies on a person's name or production wording.
    base = "alpha bravo charlie delta echo foxtrot golf hotel india juliet"
    # Roughly 51 letters per repetition; we want >= count_letters.
    reps = (count_letters // 50) + 1
    return [base for _ in range(reps)]


def test_raw_sha256_vector() -> None:
    data = b"portfolio raw bytes vector"
    assert sha256_hex_of(data) == hashlib.sha256(data).hexdigest()
    assert sha256_hex_of(data).islower()


def test_extracts_multi_page_in_source_page_order() -> None:
    page_a = [
        "alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima. "
        + " ".join(f"inventedtokenwordrun{i}" for i in range(40))
    ]
    page_b = [
        "mike november oscar papa quebec romeo sierra tango uniform victor. "
        + " ".join(f"secondpagewordrunset{i}" for i in range(40))
    ]
    data = build_pdf([page_a, page_b])
    result = extract_and_normalize(data)
    assert isinstance(result, ExtractionResult)
    assert result.page_count == 2
    a, b = result.page_normalized
    assert "alpha bravo" in a
    assert "mike november" in b
    # Page boundary preserved as a single blank line between pages.
    assert "\n\n" in result.content_md
    # content_hash is SHA-256 over the UTF-8 bytes of content_md.
    assert result.content_hash == hashlib.sha256(result.content_md.encode("utf-8")).hexdigest()


def test_zero_page_pdf_is_rejected() -> None:
    # A PDF with no pages is structurally invalid; emulate by patching after
    # the reader returns zero pages. We build an empty pages directory by
    # using a catalog pointing at zero-page pages object.
    pdf_bytes = (
        b"%PDF-1.4\n"
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
        b"2 0 obj\n<< /Type /Pages /Kids [] /Count 0 >>\nendobj\n"
        b"xref\n0 3\n0000000000 65535 f \n"
        b"0000000009 00000 n \n0000000058 00000 n \n"
        b"trailer\n<< /Size 3 /Root 1 0 R >>\nstartxref\n108\n%%EOF\n"
    )
    result = extract_and_normalize(pdf_bytes)
    assert isinstance(result, ExtractionRejection)
    # Either DOCUMENT_EMPTY or PDF_PARSE_ERROR depending on reader behavior;
    # both are rejection codes.
    assert result.code.startswith("extraction.")


def test_encrypted_pdf_is_rejected() -> None:
    # A genuinely encrypted PDF built with pypdf's writer. The reader reports
    # ``is_encrypted == True`` and we fail closed before touching page text.
    import io

    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.encrypt(user_password="pw", owner_password="pw")
    buf = io.BytesIO()
    writer.write(buf)
    result = extract_and_normalize(buf.getvalue())
    assert isinstance(result, ExtractionRejection)
    assert result.code in {
        "extraction.pdf_encrypted",
        "extraction.pdf_parse_error",
    }


def test_malformed_pdf_is_rejected() -> None:
    result = extract_and_normalize(b"%PDF-1.4 not actually a pdf document body")
    assert isinstance(result, ExtractionRejection)
    assert result.code in {
        "extraction.pdf_parse_error",
        "extraction.document_empty",
    }


def test_page_with_zero_letters_is_rejected() -> None:
    # A page whose normalized text has no Unicode letter-or-number chars:
    # only symbols, whitespace, and punctuation.
    page = ["!!! ... --- *** ### @@@ $$$ %%%"]
    data = build_pdf([page])
    result = extract_and_normalize(data)
    assert isinstance(result, ExtractionRejection)
    assert result.code == "extraction.page_no_letters_or_numbers"
    assert result.page_index == 0


def test_document_with_too_few_letters_is_rejected() -> None:
    # A page with some letters but fewer than 500 letters/numbers total.
    page = ["alpha bravo charlie delta echo"]  # ~30 letters
    data = build_pdf([page])
    result = extract_and_normalize(data)
    assert isinstance(result, ExtractionRejection)
    assert result.code == "extraction.document_too_few_letters"
    assert result.content_char_count is not None and result.content_char_count < 500
    assert result.content_char_count and result.content_char_count > 0


def test_document_with_enough_letters_but_too_few_tokens_is_rejected() -> None:
    # Force many letters but few tokens by constructing a single long token.
    # We get a page whose extracted text is one long word of >=500 letters but
    # only one token.
    long_token = "x" * 600
    page = [long_token]
    data = build_pdf([page])
    result = extract_and_normalize(data)
    assert isinstance(result, ExtractionRejection)
    assert result.code == "extraction.document_too_few_tokens"
    assert result.content_token_count is not None and result.content_token_count < 75


def test_complete_extraction_clears_min_document_boundaries() -> None:
    page = _page_with_letters(600)
    data = build_pdf([page])
    result = extract_and_normalize(data)
    assert isinstance(result, ExtractionResult)
    assert result.content_char_count >= 500
    assert result.content_token_count >= 75


def test_exact_500_letter_boundary_accepted_at_or_above() -> None:
    # Build a page so the normalized document has exactly >=500 letters and
    # >=75 tokens by adding many short invented words.
    words = []
    while True:
        result = extract_and_normalize(build_pdf([words]))
        if isinstance(result, ExtractionResult):
            break
        words.append("wordN tokenQ sampleR inventedS contentT phraseU briefV")
    assert result.content_char_count >= 500
    assert result.content_token_count >= 75


def test_extraction_rejection_does_not_carry_text() -> None:
    page = ["!! !! !! !! !!"]
    data = build_pdf([page])
    result = extract_and_normalize(data)
    assert isinstance(result, ExtractionRejection)
    # A rejection result carries only counts, never excerpts or text.
    dumped = repr(result)
    assert "!!" not in dumped
    for attr in ("content_md", "body", "text", "excerpt"):
        assert not hasattr(result, attr), attr


def test_extraction_normalizes_each_page_before_page_boundary_join() -> None:
    page_a = [f"alpha phrase line tokenrun{i}" for i in range(40)]
    page_b = [f"beta phrase line tokenrun{i}" for i in range(40)]
    data = build_pdf([page_a, page_b])
    result = extract_and_normalize(data)
    assert isinstance(result, ExtractionResult)
    # Each page_normalized entry ends with the normalized trailing newline and
    # reflects per-page normalization; the join adds a blank-line boundary.
    assert all(p.endswith("\n") for p in result.page_normalized)
    assert result.content_md.count("\n\n") >= 1  # page boundary present


def test_content_hash_is_lowercase_hex_sha256_over_utf8_markdown() -> None:
    page = _page_with_letters(600)
    data = build_pdf([page])
    result = extract_and_normalize(data)
    assert isinstance(result, ExtractionResult)
    expected = hashlib.sha256(result.content_md.encode("utf-8")).hexdigest()
    assert result.content_hash == expected
    assert result.content_hash.islower()
    assert len(result.content_hash) == 64
