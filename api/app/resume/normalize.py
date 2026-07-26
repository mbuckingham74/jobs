"""Deterministic Markdown normalization for extracted résumé text.

A single small pure function, :func:`normalize_markdown`, whose complete rules
are documented in its docstring and locked by golden tests in
``api/tests/test_resume_normalize.py``.

The function does NOT infer or rewrite résumé meaning. It never summarizes,
corrects, reorders, enriches, classifies, or generates headings that are not
present in the extraction. It only normalizes whitespace, line endings, and
Unicode, drops layout/encoding artifacts, joins line-wrapped prose without
joining across blank-line paragraph boundaries or page boundaries, preserves
recognizable bullet/list items as Markdown list items, and emits one canonical
UTF-8 string with a single trailing newline.

Normalization rules
-------------------

1. **Line endings.** CRLF / CR are collapsed to a single ``\\n``.
2. **Unicode.** Input is run through ``unicodedata.normalize("NFKC", ...)`` so
   visually identical but distinct code points unify.
3. **Removed characters.** NUL (``\\x00``), soft hyphen (``\\xad``), zero-width
   space (``\\u200b``), zero-width non-joiner (``\\u200c``), word joiner
   (``\\u2060``), BOM (``\\ufeff``), and every other Cc/ Cf control character
   that is not tab/newline are stripped. Soft hyphens that sit inside a wrapped
   word are deleted so the word rejoins.
4. **Horizontal whitespace.** Non-breaking space and every Unicode whitespace
   run are collapsed to a single regular space; tabs become a single space.
5. **Line-edge trimming.** Each line has its leading and trailing whitespace
   removed.
6. **Wrapped-line joining.** A line is joined to the previous line when both it
   and its predecessor are non-empty AND the previous line does not end in a
   paragraph-terminating punctuation (``.`` ``:`` ``;`` ``?`` ``!``) and the
   current line does not begin a list item or a structural heading. Joining
   inserts a single space and never collapses across blank lines (which form
   paragraph boundaries) or across page boundaries (which are emitted as
   explicit blank lines by the extractor).
7. **List items.** A line beginning with one of ``-``, ``*``, ``+``, ``•``,
   ``·``, ``–``-then-space, or a number followed by ``)`` or ``.`` is preserved
   as a Markdown list item: ``- item`` for symbols, ``N. item`` for ordered
   items. Recognized symbol prefixes are normalized to ``-``.
8. **Blank lines.** Runs of two or more blank lines are collapsed to exactly
   one blank line. A leading/trailing run of blank lines is removed.
9. **Trailing newline.** The result is concatenated with a single trailing
   ``\\n``.
"""

from __future__ import annotations

import re
import unicodedata

# Characters removed entirely before any whitespace normalization.
_DELETED_CHARS = "".join(
    chr(c)
    for c in range(0x110000)
    if
    (
        # NUL, soft hyphen, zero-width controls, BOM, plus any other Cc/Cf
        # control that is not newline (\n) or tab (\t).
        unicodedata.category(chr(c)) in {"Cc", "Cf"} and chr(c) not in {"\n", "\t"}
    )
)
_DELETED_TABLE = str.maketrans("", "", _DELETED_CHARS)

# Every Unicode whitespace and non-breaking space collapses to a single
# regular space. ``\n`` is excluded so paragraph breaks survive step 5/6.
_HORIZONTAL_WS = re.compile(r"[^\S\n]+")

# Lines beginning with a Markdown-unfriendly bullet/symbol prefix, normalized
# to a hyphen list item.
_SYMBOL_BULLET_RE = re.compile(r"^\s*[-*+•·]\s+(?=\S)")
# An en-dash or em-dash used as a bullet, optionally with a leading space.
_DASH_BULLET_RE = re.compile(r"^\s*[–—]\s+(?=\S)")
# Ordered list item: ``1.``, ``12)``, ``3.`` etc.
_ORDERED_BULLET_RE = re.compile(r"^\s*(\d+)[.)]\s+(?=\S)")

# Paragraph-terminating punctuation that blocks wrapped-line joining.
_PARAGRAPH_END = (
    ".",
    ":",
    ";",
    "?",
    "!",
)

_MD_SYMBOL_PREFIX = "- "


def _clean_line(line: str) -> str:
    # Collapse horizontal whitespace runs to a single space and trim edges.
    return _HORIZONTAL_WS.sub(" ", line).strip(" ")


def _normalize_list_prefix(line: str) -> str:
    """Normalize recognizable list/bullet prefixes to Markdown forms."""

    if _SYMBOL_BULLET_RE.match(line):
        return _SYMBOL_BULLET_RE.sub(_MD_SYMBOL_PREFIX, line, count=1)
    if _DASH_BULLET_RE.match(line):
        return _DASH_BULLET_RE.sub(_MD_SYMBOL_PREFIX, line, count=1)
    ordered = _ORDERED_BULLET_RE.match(line)
    if ordered:
        return _ORDERED_BULLET_RE.sub(lambda m: f"{m.group(1)}. ", line, count=1)
    return line


def _is_list_item(line: str) -> bool:
    return bool(
        _SYMBOL_BULLET_RE.match(line)
        or _DASH_BULLET_RE.match(line)
        or _ORDERED_BULLET_RE.match(line)
    )


def normalize_markdown(raw: str) -> str:
    """Normalize extracted résumé text to canonical Markdown.

    Pure and deterministic: given identical input it always returns the same
    canonical UTF-8 string with a single trailing newline. Rules are listed in
    this module's docstring and locked by golden tests.
    """

    # Step 1: line endings.
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    # Step 2: Unicode NFKC.
    text = unicodedata.normalize("NFKC", text)
    # Step 3: delete control/format characters.
    text = text.translate(_DELETED_TABLE)
    # Step 4 & 5: collapse horizontal whitespace and trim every line.
    lines = [_clean_line(line) for line in text.split("\n")]

    # Step 6 & 7: join wrapped prose and normalize list prefixes. Walk the
    # cleaned lines once, building output paragraphs.
    output: list[str] = []
    for line in lines:
        if not line:
            output.append("")
            continue
        normalized = _normalize_list_prefix(line)
        if _is_list_item(normalized) or not output:
            output.append(normalized)
            continue
        prev = output[-1]
        if not prev:
            output.append(normalized)
            continue
        if prev.endswith(_PARAGRAPH_END):
            output.append(normalized)
            continue
        # Join the wrapped line to the previous line with a single space.
        output[-1] = f"{prev} {normalized}"

    # Step 8: collapse runs of blank lines to a single blank line.
    collapsed: list[str] = []
    prev_blank = False
    for line in output:
        if not line:
            if prev_blank:
                continue
            prev_blank = True
            collapsed.append("")
        else:
            prev_blank = False
            collapsed.append(line)

    # Trim leading blank lines.
    while collapsed and not collapsed[0]:
        collapsed.pop(0)
    # Trim trailing blank lines.
    while collapsed and not collapsed[-1]:
        collapsed.pop()

    # Step 9: exactly one trailing newline.
    return "\n".join(collapsed) + "\n"
