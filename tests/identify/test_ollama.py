import base64
import json

import httpx
import pytest

from shelfsort.identify import ollama


def _body(**kw):
    return {"response": json.dumps(kw)}


def test_ollama_rejects_bad_date(httpx_mock):
    httpx_mock.add_response(json={"response": '{"title":"Wood","issue_date":"spring","issue_number":"x","cover_story":"Jigs"}'})
    i = ollama.resolve(b"png", "http://o:11434", "llava")
    assert i.issue_date is None and i.issue_number is None and i.magazine == "Wood"


def test_ollama_valid_response_and_request(httpx_mock):
    httpx_mock.add_response(json=_body(title="Wood", issue_date="2021-03", issue_number=256, cover_story="Jigs"))
    i = ollama.resolve(b"png", "http://o:11434/", "llava")
    assert (i.magazine, i.issue_date, i.issue_number, i.cover_story) == ("Wood", "2021-03", 256, "Jigs")
    assert i.title is None and i.method == "ollama" and i.confidence == 0.75
    req = httpx_mock.get_request()
    assert str(req.url) == "http://o:11434/api/generate"
    sent = json.loads(req.content)
    assert sent["model"] == "llava" and sent["format"] == "json" and sent["stream"] is False
    assert sent["images"] == [base64.b64encode(b"png").decode()]
    assert "JSON" in sent["prompt"]


def test_ollama_null_issue_number_and_month_13(httpx_mock):
    httpx_mock.add_response(json=_body(title="Wood", issue_date="2021-13", issue_number=None, cover_story=None))
    i = ollama.resolve(b"png", "http://o", "llava")
    assert i.issue_date is None and i.issue_number is None and i.cover_story is None


def test_ollama_non_200_returns_none(httpx_mock):
    httpx_mock.add_response(status_code=500)
    assert ollama.resolve(b"png", "http://o", "llava") is None


def test_ollama_invalid_json_returns_none(httpx_mock):
    httpx_mock.add_response(json={"response": "not json"})
    assert ollama.resolve(b"png", "http://o", "llava") is None


def test_ollama_connection_error_raises(httpx_mock):
    httpx_mock.add_exception(httpx.ConnectError("refused"))
    with pytest.raises(ollama.OllamaUnavailable):
        ollama.resolve(b"png", "http://o", "llava")


def test_ollama_timeout_raises(httpx_mock):
    httpx_mock.add_exception(httpx.ReadTimeout("slow"))
    with pytest.raises(ollama.OllamaUnavailable):
        ollama.resolve(b"png", "http://o", "llava")
