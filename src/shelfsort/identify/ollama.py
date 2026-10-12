"""Optional Ollama vision resolver for magazine covers."""

from __future__ import annotations

import base64
import json
import re

import httpx

from shelfsort.models import Identity

_DATE_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

PROMPT = (
    "This image is a magazine cover. Reply with ONLY a JSON object with the keys "
    '"title" (the magazine name), "issue_date" (YYYY-MM), "issue_number" '
    '(integer or null) and "cover_story" (the main cover headline).'
)


class OllamaUnavailable(Exception):
    """Ollama could not be used: unreachable, bad URL, or any other httpx error."""


# Every exception class httpx exports (HTTPError, InvalidURL, StreamError,
# CookieConflict and their subclasses): none of them may abort a scan.
_HTTPX_ERRORS = tuple(
    obj for name in httpx.__all__
    if isinstance(obj := getattr(httpx, name), type) and issubclass(obj, Exception))


def resolve(png: bytes, url: str, model: str, timeout: float = 120) -> Identity | None:
    payload = {
        "model": model,
        "prompt": PROMPT,
        "images": [base64.b64encode(png).decode("ascii")],
        "format": "json",
        "stream": False,
    }
    try:
        resp = httpx.post(url.rstrip("/") + "/api/generate", json=payload, timeout=timeout)
    except _HTTPX_ERRORS as e:
        raise OllamaUnavailable(str(e) or type(e).__name__) from e
    if resp.status_code != 200:
        return None
    try:
        data = json.loads(resp.json()["response"])
    except (ValueError, KeyError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    title = data.get("title")
    if not isinstance(title, str) or not title.strip():
        return None
    date = data.get("issue_date")
    if not (isinstance(date, str) and _DATE_RE.match(date)):
        date = None
    number = data.get("issue_number")
    if isinstance(number, bool) or not isinstance(number, int):
        number = None
    story = data.get("cover_story")
    if not isinstance(story, str) or not story.strip():
        story = None
    return Identity(magazine=title.strip(), issue_date=date, issue_number=number,
                    cover_story=story, method="ollama", confidence=0.75)
