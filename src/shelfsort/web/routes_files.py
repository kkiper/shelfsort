"""File thumbnails."""

from __future__ import annotations

import io
import os

from fastapi import APIRouter, Request, Response

from shelfsort import db, pdftext
from shelfsort import settings as settings_mod
from shelfsort.web.app import effective_sources

router = APIRouter()

_MAX_SIDE = 600
_IMAGE_EXTS = frozenset({"jpg", "jpeg", "png"})


def _render(path: str, ext: str) -> bytes:
    if ext == "pdf":
        return pdftext.render_page_png(path, 0, 100)
    from PIL import Image

    with Image.open(path) as im:
        im.thumbnail((_MAX_SIDE, _MAX_SIDE))
        out = io.BytesIO()
        im.convert("RGBA" if im.mode in ("RGBA", "LA", "P") else "RGB").save(out, "PNG")
        return out.getvalue()


@router.get("/files/{file_id}/cover.png")
def cover(request: Request, file_id: int):
    st = request.app.state
    conn = db.connect(st.db_path)
    try:
        f = conn.execute("select * from files where id = ?", (file_id,)).fetchone()
        sources = effective_sources(settings_mod.load(conn), st.sources_root)
    finally:
        conn.close()
    if f is None or not f["sha256"] or f["ext"] not in _IMAGE_EXTS | {"pdf"}:
        return Response(status_code=404)
    root = sources.get(f["source"])
    if root is None:
        return Response(status_code=404)
    real_root = os.path.realpath(root)
    path = os.path.realpath(os.path.join(root, *f["relpath"].split("/")))
    try:
        inside = os.path.commonpath([real_root, path]) == real_root
    except ValueError:  # different drives
        inside = False
    if not inside:
        return Response(status_code=404)
    thumbs = os.path.join(os.path.dirname(os.path.abspath(st.db_path)), "thumbs")
    cached = os.path.join(thumbs, f"{f['sha256']}.png")
    if os.path.isfile(cached):
        with open(cached, "rb") as fh:
            return Response(fh.read(), media_type="image/png")
    try:
        data = _render(path, f["ext"])
    except Exception:  # unreadable / corrupt file: no thumbnail
        return Response(status_code=404)
    try:
        os.makedirs(thumbs, exist_ok=True)
        tmp = f"{cached}.{os.getpid()}.tmp"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, cached)
    except OSError:
        pass  # caching is best effort
    return Response(data, media_type="image/png")
