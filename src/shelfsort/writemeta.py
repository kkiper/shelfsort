"""Write identified metadata into files (embedded) and next to them (sidecars)."""

from __future__ import annotations

import io
import os
import shutil
import tempfile
import zipfile
from xml.sax.saxutils import escape

from PIL import Image

from shelfsort.models import Identity
from shelfsort.pdftext import render_page_png
from shelfsort.tools import run_tool

_MONTHS = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]
_EMBED_EXTS = {"epub", "pdf", "mobi", "azw", "azw3"}
_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")


def _year_month(ident: Identity) -> tuple[int, int] | None:
    if not ident.issue_date:
        return None
    try:
        y, m = ident.issue_date.split("-")[:2]
        year, month = int(y), int(m)
    except ValueError:
        return None
    return (year, month) if 1 <= month <= 12 else None


def magazine_display_title(ident: Identity) -> str:
    name = ident.magazine or ""
    ym = _year_month(ident)
    if ym is None:
        return name
    return f"{name} - {_MONTHS[ym[1] - 1]} {ym[0]}"


def _title(ident: Identity) -> str | None:
    return magazine_display_title(ident) if ident.magazine else ident.title


def _date(ident: Identity) -> str | None:
    if ident.magazine:
        ym = _year_month(ident)
        return f"{ym[0]:04d}-{ym[1]:02d}-01" if ym else None
    return f"{ident.year}-01-01" if ident.year else None


def opf_xml(ident: Identity) -> str:
    e = escape
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<package xmlns="http://www.idpf.org/2007/opf" version="2.0">',
        '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:opf="http://www.idpf.org/2007/opf">',
    ]
    title = _title(ident)
    if title:
        lines.append(f"    <dc:title>{e(title)}</dc:title>")
    for a in ident.authors:
        lines.append(f"    <dc:creator>{e(a)}</dc:creator>")
    if ident.publisher:
        lines.append(f"    <dc:publisher>{e(ident.publisher)}</dc:publisher>")
    date = _date(ident)
    if date:
        lines.append(f"    <dc:date>{date}</dc:date>")
    if ident.isbn:
        lines.append(f'    <dc:identifier opf:scheme="ISBN">{e(ident.isbn)}</dc:identifier>')
    if ident.magazine and ident.cover_story:
        lines.append(f"    <dc:description>{e(ident.cover_story)}</dc:description>")
    if ident.magazine:
        lines.append(f'    <meta name="calibre:series" content="{e(ident.magazine, {chr(34): "&quot;"})}"/>')
        if ident.issue_number is not None:
            lines.append(f'    <meta name="calibre:series_index" content="{ident.issue_number}"/>')
    lines += ["  </metadata>", "</package>", ""]
    return "\n".join(lines)


def ebook_meta_args(path: str, ident: Identity) -> list[str]:
    """Full `ebook-meta` command line for `path`; only present fields get flags."""
    args = ["ebook-meta", path]
    title = _title(ident)
    if title:
        args += ["-t", title]
    if ident.authors:
        args += ["-a", " & ".join(ident.authors)]
    if ident.publisher:
        args += ["--publisher", ident.publisher]
    date = _date(ident)
    if date:
        args += ["--date", date]
    if ident.isbn:
        args += ["--isbn", ident.isbn]
    if ident.magazine:
        if ident.cover_story:
            args += ["--comments", ident.cover_story]
        args += ["--series", ident.magazine]
        if ident.issue_number is not None:
            args += ["--index", str(ident.issue_number)]
    return args


def backup_original(source_root: str, relpath: str) -> str:
    dest = os.path.join(source_root, "_quarantine", "_originals", relpath)
    if not os.path.exists(dest):
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy2(os.path.join(source_root, relpath), dest)
    return dest


def _comicinfo_xml(ident: Identity) -> str:
    fields: list[tuple[str, str]] = []
    title = _title(ident)
    if title:
        fields.append(("Title", title))
    if ident.magazine:
        fields.append(("Series", ident.magazine))
        if ident.issue_number is not None:
            fields.append(("Number", str(ident.issue_number)))
    ym = _year_month(ident) if ident.magazine else None
    if ym:
        fields += [("Year", str(ym[0])), ("Month", str(ym[1]))]
    elif ident.year and not ident.magazine:
        fields.append(("Year", str(ident.year)))
    if ident.magazine and ident.cover_story:
        fields.append(("Summary", ident.cover_story))
    if ident.publisher:
        fields.append(("Publisher", ident.publisher))
    if ident.authors:
        fields.append(("Writer", ", ".join(ident.authors)))
    body = "".join(f"  <{k}>{escape(v)}</{k}>\n" for k, v in fields)
    return f'<?xml version="1.0" encoding="utf-8"?>\n<ComicInfo>\n{body}</ComicInfo>\n'


def _write_cbz(path: str, ident: Identity) -> None:
    fd, tmp = tempfile.mkstemp(suffix=".tmp", prefix=".cbz-", dir=os.path.dirname(path))
    os.close(fd)
    try:
        with zipfile.ZipFile(path) as zin, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
            for info in zin.infolist():
                if info.filename == "ComicInfo.xml":
                    continue
                zout.writestr(info, zin.read(info.filename))
            zout.writestr("ComicInfo.xml", _comicinfo_xml(ident))
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def _to_jpeg(data: bytes) -> bytes:
    with Image.open(io.BytesIO(data)) as im:
        buf = io.BytesIO()
        im.convert("RGB").save(buf, "JPEG", quality=85)
        return buf.getvalue()


def _cover_jpeg(path: str, ext: str) -> bytes | None:
    try:
        if ext == "pdf":
            return _to_jpeg(render_page_png(path, 0))
        if ext == "cbz":
            with zipfile.ZipFile(path) as z:
                names = sorted(n for n in z.namelist() if n.lower().endswith(_IMAGE_EXTS))
                return _to_jpeg(z.read(names[0])) if names else None
    except Exception:  # render/convert failure: skip the cover silently
        return None
    return None


def _write_sidecar(source_root: str, rel: str, data: bytes) -> str:
    full = os.path.abspath(os.path.join(source_root, rel))
    if os.path.exists(full):
        backup_original(source_root, rel)
    with open(full, "wb") as f:
        f.write(data)
    return full


def write_metadata(source_root: str, relpath: str, ident: Identity, ext: str, sidecar_all: bool) -> list[str]:
    ext = ext.lower().lstrip(".")
    full = os.path.abspath(os.path.join(source_root, relpath))
    written: list[str] = []
    embeddable = ext in _EMBED_EXTS or ext == "cbz"
    if embeddable:
        backup_original(source_root, relpath)
        if ext == "cbz":
            _write_cbz(full, ident)
        else:
            run_tool(ebook_meta_args(full, ident), timeout=120)
        written.append(full)
    if sidecar_all or not embeddable:
        stem = os.path.splitext(relpath)[0]
        written.append(_write_sidecar(source_root, stem + ".opf", opf_xml(ident).encode("utf-8")))
        cover = _cover_jpeg(full, ext)
        if cover is not None:
            written.append(_write_sidecar(source_root, stem + ".cover.jpg", cover))
    return written
