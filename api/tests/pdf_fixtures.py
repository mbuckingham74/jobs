"""Synthetic PDF fixture builder for resume tests.

Builds tiny synthetic single-or-multi-page PDFs containing invented,
non-personal text (Latin alphabet prose) so the extraction, normalization, and
completeness tests never need the production résumé, a private résumé, or a
captured production HTTP response. The builder emits a minimal but valid PDF
that pypdf can read deterministically.
"""

from __future__ import annotations


def _escape(text: str) -> str:
    return (
        text.replace("\\", r"\\")
        .replace("(", r"\(")
        .replace(")", r"\)")
        .replace("\r", " ")
        .replace("\n", " ")
    )


def _page_content_stream(lines: list[str]) -> bytes:
    out = [b"BT", b"/F1 12 Tf", b"72 720 Td", b"14 TL"]
    for i, line in enumerate(lines):
        if i == 0:
            out.append(f"({_escape(line)}) Tj".encode("latin-1"))
        else:
            out.append(b"T*")
            out.append(f"({_escape(line)}) Tj".encode("latin-1"))
    out.append(b"ET")
    return b"\n".join(out)


def build_pdf(pages: list[list[str]]) -> bytes:
    """Return a valid PDF with one page per inner list of lines.

    Lines within a page are drawn top-to-bottom with a fixed leading so pypdf's
    ``extract_text`` returns one line per input line separated by ``\\n``.
    """

    page_count = len(pages)
    assert page_count >= 1, "build_pdf requires at least one page"
    bodies: list[tuple[int, bytes]] = []
    page_obj_nums = list(range(3, 3 + page_count * 2, 2))
    content_obj_nums = list(range(4, 4 + page_count * 2, 2))
    font_obj_num = 3 + page_count * 2

    bodies.append((1, b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"))
    kid_refs = " ".join(f"{n} 0 R" for n in page_obj_nums)
    bodies.append(
        (
            2,
            f"2 0 obj\n<< /Type /Pages /Kids [{kid_refs}] /Count {page_count} >>\n"
            "endobj\n".encode("latin-1"),
        )
    )

    for i, lines in enumerate(pages):
        content = _page_content_stream(lines)
        content_obj = content_obj_nums[i]
        page_obj = page_obj_nums[i]
        bodies.append(
            (
                content_obj,
                f"{content_obj} 0 obj\n<< /Length {len(content)} >>\nstream\n".encode("latin-1")
                + content
                + b"\nendstream\nendobj\n",
            )
        )
        bodies.append(
            (
                page_obj,
                (
                    f"{page_obj} 0 obj\n<< /Type /Page /Parent 2 0 R "
                    f"/MediaBox [0 0 612 792] "
                    f"/Resources << /Font << /F1 {font_obj_num} 0 R >> >> "
                    f"/Contents {content_obj} 0 R >>\nendobj\n"
                ).encode("latin-1"),
            )
        )

    bodies.append(
        (
            font_obj_num,
            (
                f"{font_obj_num} 0 obj\n<< /Type /Font /Subtype /Type1 "
                "/BaseFont /Helvetica >>\nendobj\n"
            ).encode("latin-1"),
        )
    )

    bodies.sort(key=lambda t: t[0])
    pdf = b"%PDF-1.4\n"
    offsets: list[tuple[int, int]] = []
    for num, data in bodies:
        offsets.append((num, len(pdf)))
        pdf += data
    last_obj = max(n for n, _ in bodies)
    xref_pos = len(pdf)
    pdf += b"xref\n"
    pdf += f"0 {last_obj + 1}\n".encode("latin-1")
    pdf += b"0000000000 65535 f \n"
    for i in range(1, last_obj + 1):
        offset = next((o for n, o in offsets if n == i), 0)
        pdf += f"{offset:010d} 00000 n \n".encode("latin-1")
    pdf += b"trailer\n"
    pdf += f"<< /Size {last_obj + 1} /Root 1 0 R >>\n".encode("latin-1")
    pdf += b"startxref\n"
    pdf += f"{xref_pos}\n".encode("latin-1")
    pdf += b"%%EOF\n"
    return pdf
