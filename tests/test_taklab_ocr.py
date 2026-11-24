"""/Marti/api/taklab/ocr: skan dokumentu -> tekst dla wtyczki TakLab.

OTS sprawdza konto z certyfikatu, rozmiar i typ zdjęcia, a zadanie oddaje
centralnej kolejce taklab-ocr (żeton środowiska, konto w nagłówku). Kolejka
pilnuje właściciela zadania i limitu 3 zadań konta; OTS przekazuje jej kody.
"""

import importlib
import json
from unittest.mock import MagicMock

import pytest
import requests

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 100
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
JOB = "1b4e28ba-2fa1-11d2-883f-0016d3cca427"
MODULE = "opentakserver.blueprints.marti_api.taklab_ocr_marti_api"


@pytest.fixture
def ocr_env(monkeypatch):
    monkeypatch.setenv("OTS_TAKLAB_OCR_URL", "http://taklab-ocr.taklab-system.svc:8000/")
    monkeypatch.setenv("OTS_TAKLAB_OCR_TOKEN", "tok-env")
    monkeypatch.setenv("OTS_TAKLAB_OCR_CLIENT", "public")
    monkeypatch.setenv("OTS_TAKLAB_OCR_MAX_BYTES", "1000")


@pytest.fixture
def cert_user(monkeypatch):
    module = importlib.import_module(MODULE)
    current = {"username": "alice"}

    def fake_cert():
        if current["username"] is None:
            return None
        cert = MagicMock()
        cert.subject.get_attributes_for_oid.return_value = [MagicMock(value=current["username"])]
        return cert

    monkeypatch.setattr(module, "verify_client_cert", fake_cert)
    config_module = importlib.import_module("opentakserver.blueprints.marti_api.taklab_config_marti_api")
    monkeypatch.setattr(config_module, "verify_client_cert", fake_cert)
    return current


class Upstream:
    def __init__(self):
        self.calls = []
        self.status = 202
        self.body = {"id": JOB, "status": "queued", "position": 1}
        self.raise_error = None

    def __call__(self, method, url, timeout=None, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        if self.raise_error:
            raise self.raise_error
        res = MagicMock()
        res.status_code = self.status
        res.content = b"" if self.body is None else json.dumps(self.body).encode()
        res.json.return_value = self.body
        return res


@pytest.fixture
def upstream(monkeypatch):
    fake = Upstream()
    monkeypatch.setattr(importlib.import_module(MODULE).requests, "request", fake)
    return fake


def _user(app, username="alice", active=True):
    from opentakserver.extensions import db

    with app.app_context():
        app.security.datastore.create_user(username=username, password="x", active=active)
        db.session.commit()


def _post(client, data=JPEG, content_type="image/jpeg", **headers):
    return client.post("/Marti/api/taklab/ocr", data=data, content_type=content_type, headers=headers)


def test_submit_forwards_with_env_token_and_account(app, client, ocr_env, cert_user, upstream):
    _user(app)
    res = _post(client, **{"X-Taklab-Filename": "20261124-101500.jpg"})
    assert res.status_code == 202, res.text
    assert res.json == {"id": JOB, "status": "queued", "position": 1}
    assert res.headers["Cache-Control"] == "no-store"
    call = upstream.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "http://taklab-ocr.taklab-system.svc:8000/v1/jobs"
    assert call["data"] == JPEG
    assert call["headers"]["Authorization"] == "Bearer tok-env"
    assert call["headers"]["X-Taklab-Ocr-Client"] == "public"
    assert call["headers"]["X-Taklab-Ocr-Account"] == "alice"
    assert call["headers"]["Content-Type"] == "image/jpeg"
    assert call["headers"]["X-Taklab-Filename"] == "20261124-101500.jpg"


def test_png_accepted(app, client, ocr_env, cert_user, upstream):
    _user(app)
    assert _post(client, data=PNG, content_type="image/png").status_code == 202


def test_filename_control_characters_removed(app, client, ocr_env, cert_user, upstream):
    _user(app)
    _post(client, **{"X-Taklab-Filename": "a\tb\x7f.jpg"})
    assert upstream.calls[0]["headers"]["X-Taklab-Filename"] == "ab.jpg"


def test_without_certificate_401(app, client, ocr_env, cert_user, upstream):
    cert_user["username"] = None
    assert _post(client).status_code == 401
    assert client.get(f"/Marti/api/taklab/ocr/{JOB}").status_code == 401
    assert client.delete(f"/Marti/api/taklab/ocr/{JOB}").status_code == 401
    assert upstream.calls == []


def test_unknown_or_inactive_account_403(app, client, ocr_env, cert_user, upstream):
    assert _post(client).status_code == 403
    _user(app, "bob", active=False)
    cert_user["username"] = "bob"
    assert _post(client).status_code == 403
    assert upstream.calls == []


def test_too_large_413(app, client, ocr_env, cert_user, upstream):
    _user(app)
    res = _post(client, data=b"\xff\xd8\xff" + b"\x00" * 1000)
    assert res.status_code == 413
    assert res.headers["Cache-Control"] == "no-store"
    assert upstream.calls == []


def test_exactly_at_limit_accepted(app, client, ocr_env, cert_user, upstream):
    _user(app)
    assert _post(client, data=b"\xff\xd8\xff" + b"\x00" * 997).status_code == 202


@pytest.mark.parametrize("data,content_type", [
    (JPEG, "image/gif"),
    (JPEG, "application/octet-stream"),
    (b"GIF89a" + b"\x00" * 10, "image/jpeg"),
    (PNG, "image/jpeg"),
    (b"", "image/png"),
])
def test_wrong_type_415(app, client, ocr_env, cert_user, upstream, data, content_type):
    _user(app)
    assert _post(client, data=data, content_type=content_type).status_code == 415
    assert upstream.calls == []


def test_limit_from_queue_passed_through(app, client, ocr_env, cert_user, upstream):
    _user(app)
    upstream.status = 429
    upstream.body = {"error": "Masz już 3 zadania w toku."}
    res = _post(client)
    assert res.status_code == 429
    assert res.json["error"]


def test_get_job_forwards_account(app, client, ocr_env, cert_user, upstream):
    _user(app)
    upstream.status = 200
    upstream.body = {"id": JOB, "status": "done", "text": "Zażółć gęślą jaźń"}
    res = client.get(f"/Marti/api/taklab/ocr/{JOB}")
    assert res.status_code == 200
    assert res.json["text"] == "Zażółć gęślą jaźń"
    assert res.headers["Cache-Control"] == "no-store"
    call = upstream.calls[0]
    assert call["method"] == "GET"
    assert call["url"].endswith(f"/v1/jobs/{JOB}")
    assert call["headers"]["X-Taklab-Ocr-Account"] == "alice"


def test_foreign_job_is_404_from_queue(app, client, ocr_env, cert_user, upstream):
    _user(app, "mallory")
    cert_user["username"] = "mallory"
    upstream.status = 404
    upstream.body = {"error": "Nie ma takiego zadania."}
    assert client.get(f"/Marti/api/taklab/ocr/{JOB}").status_code == 404
    assert upstream.calls[0]["headers"]["X-Taklab-Ocr-Account"] == "mallory"


def test_delete_job(app, client, ocr_env, cert_user, upstream):
    _user(app)
    upstream.status = 204
    upstream.body = None
    res = client.delete(f"/Marti/api/taklab/ocr/{JOB}")
    assert res.status_code == 204
    assert res.headers["Cache-Control"] == "no-store"
    assert upstream.calls[0]["method"] == "DELETE"


def test_bad_job_id_404_without_forwarding(app, client, ocr_env, cert_user, upstream):
    _user(app)
    assert client.get("/Marti/api/taklab/ocr/..%2Fadmin").status_code == 404
    assert client.get("/Marti/api/taklab/ocr/not-a-uuid").status_code == 404
    assert upstream.calls == []


def test_queue_down_503(app, client, ocr_env, cert_user, upstream):
    _user(app)
    upstream.raise_error = requests.ConnectionError("down")
    res = _post(client)
    assert res.status_code == 503
    assert res.json["error"]


def test_queue_unexpected_status_503(app, client, ocr_env, cert_user, upstream):
    _user(app)
    upstream.status = 500
    upstream.body = {"detail": "boom"}
    assert _post(client).status_code == 503


def test_ocr_off_404(app, client, cert_user, upstream, monkeypatch):
    monkeypatch.delenv("OTS_TAKLAB_OCR_URL", raising=False)
    _user(app)
    assert _post(client).status_code == 404
    assert upstream.calls == []


def test_config_has_ocr_section(app, client, ocr_env, cert_user):
    _user(app)
    body = client.get("/Marti/api/taklab/config").json
    assert body["ocr"] == {"maxBytes": 1000}


def test_config_default_limit_15mb(app, client, ocr_env, cert_user, monkeypatch):
    monkeypatch.delenv("OTS_TAKLAB_OCR_MAX_BYTES")
    _user(app)
    assert client.get("/Marti/api/taklab/config").json["ocr"] == {"maxBytes": 15728640}


def test_config_ocr_null_without_settings(app, client, cert_user, monkeypatch):
    for key in ("OTS_TAKLAB_OCR_URL", "OTS_TAKLAB_OCR_TOKEN", "OTS_TAKLAB_OCR_CLIENT"):
        monkeypatch.delenv(key, raising=False)
    _user(app)
    body = client.get("/Marti/api/taklab/config").json
    assert "ocr" in body and body["ocr"] is None


def test_image_and_text_not_logged(app, client, ocr_env, cert_user, upstream, caplog):
    _user(app)
    upstream.status = 200
    upstream.body = {"id": JOB, "status": "done", "text": "TAJNY-TEKST"}
    with caplog.at_level("DEBUG"):
        _post(client, data=JPEG + b"SEKRETNE-ZDJECIE")
        client.get(f"/Marti/api/taklab/ocr/{JOB}")
    assert "TAJNY-TEKST" not in caplog.text
    assert "SEKRETNE-ZDJECIE" not in caplog.text
