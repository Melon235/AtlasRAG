#!/usr/bin/env python3
"""Generate deterministic, dependency-free Stage 3 parser input fixtures."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIRECTORY = ROOT / "tests" / "fixtures" / "stage3"


def _pdf_bytes() -> bytes:
    content = b"""q
0.8 w
72 346 360 72 re S
72 370 m 432 370 l S
240 346 m 240 418 l S
Q
q
0.94 g
72 462 360 92 re f
Q
BT
/F3 26 Tf
156 742 Td
(AtlasRAG PDF Fixture) Tj
ET
BT
/F1 12 Tf
72 680 Td
(Canonical paragraph from MinerU flash.) Tj
0 -18 Td
(This fixture preserves stable source order.) Tj
ET
BT
/F1 12 Tf
84 616 Td
(a. Alpha item) Tj
0 -16 Td
(b. Beta item) Tj
0 -16 Td
(c. Gamma item) Tj
ET
BT
/F2 10 Tf
84 530 Td
(def parse_pdf\(\):) Tj
0 -14 Td
(    items = [\"alpha\", \"beta\"]) Tj
0 -14 Td
(    for item in items:) Tj
0 -14 Td
(        print\(item\)) Tj
0 -14 Td
(    return items) Tj
ET
BT
/F1 11 Tf
84 398 Td
(Name) Tj
174 0 Td
(Value) Tj
-174 -24 Td
(alpha) Tj
174 0 Td
(1) Tj
-174 -24 Td
(beta) Tj
174 0 Td
(2) Tj
ET
"""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 4 0 R /F2 5 0 R /F3 6 0 R >> >> "
            b"/Contents 7 0 R >>"
        ),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>",
        b"<< /Length "
        + str(len(content)).encode("ascii")
        + b" >>\nstream\n"
        + content
        + b"endstream",
    ]

    document = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, body in enumerate(objects, start=1):
        offsets.append(len(document))
        document.extend(f"{number} 0 obj\n".encode("ascii"))
        document.extend(body)
        document.extend(b"\nendobj\n")

    xref_offset = len(document)
    document.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    document.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        document.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    document.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode("ascii")
    )
    return bytes(document)


def main() -> None:
    FIXTURE_DIRECTORY.mkdir(parents=True, exist_ok=True)
    (FIXTURE_DIRECTORY / "simple.pdf").write_bytes(_pdf_bytes())


if __name__ == "__main__":
    main()
