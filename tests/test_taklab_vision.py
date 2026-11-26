"""taklab_vision: ustawienia, podpis adresu pliku i klient usługi (bez bazy)."""
from urllib.parse import parse_qs, urlparse

import pytest
import requests

from opentakserver import taklab_vision as tv
from opentakserver.taklab_vision import (
    FILE_URL_TTL_S,
    VisionSettings,
    VisionUnavailable,
    call,
    file_signature,
    settings,
    signature_valid,
    signed_file_url,
)

CFG = VisionSettings("http://v", "tok", "public", "public-ots.taklab.eu")


def _set(monkeypatch, url="http://v", token="tok", client="public", host="h.eu"):
    for k, v in (
        ("OTS_TAKLAB_VISION_URL", url),
        ("OTS_TAKLAB_VISION_TOKEN", token),
        ("OTS_TAKLAB_VISION_CLIENT", client),
        ("OTS_TAKLAB_HOST", host),
    ):
        monkeypatch.setenv(k, v)


def test_settings_none_without_url_token_or_client(monkeypatch):
    _set(monkeypatch)
    assert settings() == VisionSettings("http://v", "tok", "public", "h.eu")
    for missing in ("url", "token", "client"):
        _set(monkeypatch, **{missing: ""})
        assert settings() is None
    _set(monkeypatch, host="")
    assert settings().host == ""


def test_signed_url_roundtrip():
    url = signed_file_url(CFG, "a/b", now=1000.0)
    assert url.startswith(
        "https://public-ots.taklab.eu/api/taklab/vision/recordings/a%2Fb/file?expires=22600&signature="
    )
    q = parse_qs(urlparse(url).query)
    assert signature_valid(CFG, "a/b", q["expires"][0], q["signature"][0], now=1000.0)


def test_signature_rejects_other_recording_expired_far_future_and_garbage():
    exp = 1000 + FILE_URL_TTL_S
    sig = file_signature("tok", "r1", exp)
    assert signature_valid(CFG, "r1", str(exp), sig, now=1000.0)
    assert not signature_valid(CFG, "r2", str(exp), sig, now=1000.0)
    assert not signature_valid(CFG, "r1", str(exp), sig, now=float(exp))
    assert not signature_valid(CFG, "r1", str(exp), sig, now=exp + 5.0)
    far = 1000 + 2 * FILE_URL_TTL_S
    assert not signature_valid(CFG, "r1", str(far), file_signature("tok", "r1", far), now=1000.0)
    assert not signature_valid(CFG, "r1", "abc", sig, now=1000.0)
    assert not signature_valid(CFG, "r1", None, sig, now=1000.0)
    assert not signature_valid(CFG, "r1", str(exp), None, now=1000.0)
    assert not signature_valid(CFG, "r1", str(exp), "zz", now=1000.0)
    assert not signature_valid(VisionSettings("u", "other", "c", "h"), "r1", str(exp), sig, now=1000.0)


def test_call_sends_env_headers_and_maps_errors(monkeypatch):
    seen = {}

    class Fake:
        def request(self, method, url, **kw):
            seen.update(method=method, url=url, **kw)
            return "resp"

    monkeypatch.setattr(tv, "_session", Fake())
    assert call(CFG, "POST", "/v1/env/analyses", json_body={"a": 1}) == "resp"
    assert seen["url"] == "http://v/v1/env/analyses"
    assert seen["headers"] == {"Authorization": "Bearer tok", "X-Taklab-Vision-Client": "public"}
    assert seen["allow_redirects"] is False and seen["json"] == {"a": 1}

    class Boom:
        def request(self, *a, **k):
            raise requests.ConnectionError("x")

    monkeypatch.setattr(tv, "_session", Boom())
    with pytest.raises(VisionUnavailable):
        call(CFG, "GET", "/x")
