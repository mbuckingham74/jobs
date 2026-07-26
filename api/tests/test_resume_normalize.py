"""Golden tests for the deterministic Markdown normalization function.

Each case locks one normalization rule against a fixed expected output. The
expected strings are literals, not computed, so a regression changes the
actual output and the test fails with the before/after visible.
"""

from __future__ import annotations

from app.resume.normalize import normalize_markdown


def test_normalizes_crlf_and_cr_line_endings() -> None:
    raw = "alpha bravo\r\ncharlie delta\r echo"
    assert normalize_markdown(raw) == "alpha bravo charlie delta echo\n"


def test_normalizes_unicode_via_nfkc() -> None:
    # Fullwidth "A" (U+FF21) normalizes to ASCII "A" under NFKC.
    raw = "\uff21lpha bravo"
    assert normalize_markdown(raw) == "Alpha bravo\n"


def test_strips_nul_soft_hyphen_and_zero_width_controls() -> None:
    raw = "al\u0000ph\u00ada bravo\u200b charlie"
    # Soft hyphen is deleted so the word rejoins: "alpha bravo charlie"
    assert normalize_markdown(raw) == "alpha bravo charlie\n"


def test_strips_bom() -> None:
    raw = "\ufeffHeader content line one here"
    assert normalize_markdown(raw) == "Header content line one here\n"


def test_collapses_non_breaking_and_repeated_horizontal_whitespace() -> None:
    raw = "alpha\u00a0\u00a0bravo\t\tcharlie"
    assert normalize_markdown(raw) == "alpha bravo charlie\n"


def test_trims_line_edge_whitespace() -> None:
    raw = "   alpha bravo   \n   charlie   "
    assert normalize_markdown(raw) == "alpha bravo charlie\n"


def test_joins_ordinary_wrapped_lines_without_paragraph_break() -> None:
    raw = (
        "This is a first line that wraps onto a second line\n"
        "which should be joined into a single paragraph as one sentence"
    )
    assert (
        normalize_markdown(raw) == "This is a first line that wraps onto a second line "
        "which should be joined into a single paragraph as one sentence\n"
    )


def test_does_not_join_across_blank_line_boundary() -> None:
    raw = "alpha bravo\n\ncharlie delta"
    assert normalize_markdown(raw) == "alpha bravo\n\ncharlie delta\n"


def test_does_not_join_after_paragraph_ending_punctuation() -> None:
    raw = "alpha bravo.\ncharlie delta"
    assert normalize_markdown(raw) == "alpha bravo.\ncharlie delta\n"


def test_preserves_bullet_items_as_markdown_list_items() -> None:
    raw = "- alpha bravo\n- charlie delta\n- echo foxtrot"
    assert normalize_markdown(raw) == "- alpha bravo\n- charlie delta\n- echo foxtrot\n"


def test_normalizes_symbol_bullets_to_hyphen() -> None:
    raw = "• alpha bravo\n* charlie delta\n+ echo foxtrot"
    assert normalize_markdown(raw) == "- alpha bravo\n- charlie delta\n- echo foxtrot\n"


def test_preserves_ordered_list_items() -> None:
    raw = "1. alpha bravo\n2. charlie delta\n3. echo foxtrot"
    assert normalize_markdown(raw) == "1. alpha bravo\n2. charlie delta\n3. echo foxtrot\n"


def test_collapses_repeated_blank_lines_to_one() -> None:
    raw = "alpha\n\n\n\ncharlie"
    assert normalize_markdown(raw) == "alpha\n\ncharlie\n"


def test_emits_single_trailing_newline() -> None:
    raw = "alpha bravo"
    assert normalize_markdown(raw) == "alpha bravo\n"
    # Idempotent for the trailing-newline rule.
    assert normalize_markdown("alpha bravo\n") == "alpha bravo\n"


def test_strips_leading_and_trailing_blank_lines() -> None:
    raw = "\n\n  \nalpha bravo\n\n\n"
    assert normalize_markdown(raw) == "alpha bravo\n"


def test_preserves_page_boundary_as_blank_line() -> None:
    # The extractor emits page boundaries as a blank line between pages.
    raw = "page one content here\n\npage two content here"
    assert normalize_markdown(raw) == "page one content here\n\npage two content here\n"


def test_does_not_infer_headings_not_present() -> None:
    raw = "alpha bravo charlie delta echo foxtrot golf hotel india"
    out = normalize_markdown(raw)
    # No "# " Markdown heading is introduced by the normalizer.
    assert "# " not in out
    assert out == raw + "\n"


def test_empty_input_yields_empty_string_with_trailing_newline() -> None:
    assert normalize_markdown("") == "\n"


def test_deterministic_against_only_whitespace() -> None:
    assert normalize_markdown("\n\n\t\t   \r\n\n") == "\n"


def test_en_dash_as_bullet_normalizes_to_hyphen() -> None:
    raw = "– alpha bravo\n– charlie delta"
    assert normalize_markdown(raw) == "- alpha bravo\n- charlie delta\n"
