"""Builds a small ebook library on disk for end-to-end tests."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import pymupdf

SHOP_JIGS = (
    "Shop Jigs -- Bill Hylton -- 2005 -- Taunton -- "
    "1aa655cf2e3d698418bca2d8523a2531 -- Anna's Archive.epub"
)
BOOKENDS = "arch-top-bookends.pdf" if os.name == "nt" else "arch-top-bookends<.pdf"


@dataclass
class FixtureSource:
    root: Path

    @property
    def sources(self) -> dict[str, str]:
        return {"remote": str(self.root)}

    def path(self, rel: str) -> str:
        return str(self.root.joinpath(*rel.split("/")))

    def exists(self, rel: str) -> bool:
        return self.root.joinpath(*rel.split("/")).exists()


def make_pdf(path: Path, pages: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page()
        page.insert_text((72, 72), text)
    doc.save(str(path))
    doc.close()


def build(root) -> FixtureSource:
    root = Path(root)
    wood = root / "Woodworking"
    wood.mkdir(parents=True, exist_ok=True)

    make_pdf(wood / "dovetail-sawing-guide.pdf", ["Dovetail tips"])
    # pymupdf output is not reproducible, so copy the bytes for a true duplicate.
    (wood / "saw cuts quick sheet.pdf").write_bytes(
        (wood / "dovetail-sawing-guide.pdf").read_bytes())

    (wood / "Router Table.epub").write_bytes(b"junk epub bytes")
    (wood / "Router Table.lit").write_bytes(b"junk lit bytes")

    make_pdf(wood / "a" / "Morris Chair.pdf", ["Morris chair, short"])
    make_pdf(wood / "b" / "Morris Chair.pdf", ["Morris chair", "Morris chair, long"])

    make_pdf(wood / "011167038.pdf", ["WOOD Router Jigs", "contents", "ads"])
    make_pdf(wood / BOOKENDS, ["Arch top bookends", "cutting list"])

    (wood / "broken.pdf").write_bytes(b"this is not a pdf at all\x00\x01\x02")
    (wood / SHOP_JIGS).write_bytes(b"shop jigs epub bytes")
    (wood / "notes.txt").write_text("remember to buy glue\n")
    return FixtureSource(root)
