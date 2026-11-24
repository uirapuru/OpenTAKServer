"""/Marti/api/taklab/report: raporty misji dla wtyczki TakLab, i trasy wewnętrzne usługi raportów.

OTS sprawdza konto z certyfikatu i przekazuje wywołanie usłudze raportów
środowiska (żeton w X-Report-Token, konto w X-Report-User). Usługa pilnuje
uprawnień i plików, OTS przekazuje jej kody i treść.

Trasy /api/taklab/report/... woła sama usługa na 8081: plik po skrócie
i sprawdzenie hasła misji. Bez żetonu w środowisku ich nie ma (404).
"""

import importlib
import json
import os
from unittest.mock import MagicMock

import pytest
import requests

MODULE = "opentakserver.blueprints.marti_api.taklab_report_marti_api"
REPORT = "0f1e2d3c4b5a69788796a5b4c3d2e1f0"
TOKEN = "tok-report"
HASH = "a" * 64


@pytest.fixture
def report_env(monkeypatch):
    monkeypatch.setenv("OTS_TAKLAB_REPORT_URL", "http://e-report:8080/")
    monkeypatch.setenv("OTS_TAKLAB_REPORT_TOKEN", TOKEN)


@pytest.fixture
def report_off(monkeypatch):
    monkeypatch.delenv("OTS_TAKLAB_REPORT_URL", raising=False)
    monkeypatch.delenv("OTS_TAKLAB_REPORT_TOKEN", raising=False)


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
        self.body = {"id": REPORT, "status": "queued"}
        self.content = None
        self.headers = {}
        self.raise_error = None

    def __call__(self, method, url, timeout=None, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        if self.raise_error:
            raise self.raise_error
        res = MagicMock()
        res.status_code = self.status
        res.headers = self.headers
        raw = self.content if self.content is not None else json.dumps(self.body).encode()
        res.content = raw
        res.iter_content.return_value = [raw[i:i + 3] for i in range(0, len(raw), 3)]
        if self.content is not None:
            res.json.side_effect = ValueError("not json")
        else:
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


ORDER = {"scope": "mission", "mission": "Ćwiczenie", "since": "2026-11-24T08:00:00+01:00",
         "until": "2026-11-24T12:00:00+01:00", "mission_password": "TAJNE-HASLO"}


def _order(client, body=ORDER):
    return client.post("/Marti/api/taklab/report", data=json.dumps(body), content_type="application/json")


# --- Trasy dla ATAK ---


def test_report_requires_cert(app, client, report_env, cert_user, upstream):
    cert_user["username"] = None
    assert _order(client).status_code == 403
    assert client.get("/Marti/api/taklab/report").status_code == 403
    assert client.get(f"/Marti/api/taklab/report/{REPORT}").status_code == 403
    assert client.get(f"/Marti/api/taklab/report/{REPORT}.pdf").status_code == 403
    assert client.get(f"/Marti/api/taklab/report/{REPORT}.txt").status_code == 403
    assert client.delete(f"/Marti/api/taklab/report/{REPORT}").status_code == 403
    assert upstream.calls == []


def test_unknown_or_inactive_account_403(app, client, report_env, cert_user, upstream):
    assert _order(client).status_code == 403
    _user(app, "bob", active=False)
    cert_user["username"] = "bob"
    assert client.get("/Marti/api/taklab/report").status_code == 403
    assert upstream.calls == []


def test_cert_without_common_name_403(app, client, report_env, cert_user, upstream, monkeypatch):
    def cert_without_cn():
        cert = MagicMock()
        cert.subject.get_attributes_for_oid.return_value = []
        return cert

    monkeypatch.setattr(importlib.import_module(MODULE), "verify_client_cert", cert_without_cn)
    assert _order(client).status_code == 403
    assert client.get("/Marti/api/taklab/report").status_code == 403
    assert upstream.calls == []


def test_client_sent_report_headers_are_overwritten(app, client, report_env, cert_user, upstream):
    _user(app)
    upstream.status = 200
    upstream.body = {"items": []}
    client.get("/Marti/api/taklab/report", headers={"X-Report-User": "admin", "X-Report-Token": "zly"})
    client.post("/Marti/api/taklab/report", data=json.dumps(ORDER), content_type="application/json",
                headers={"X-Report-User": "admin"})
    for call in upstream.calls:
        assert call["headers"]["X-Report-User"] == "alice"
        assert call["headers"]["X-Report-Token"] == TOKEN


def test_forward_read_timeout_below_nginx_30s():
    connect, read = importlib.import_module(MODULE).FORWARD_TIMEOUT
    assert read <= 25


def test_report_forwards_account_from_cert(app, client, report_env, cert_user, upstream):
    _user(app)
    res = _order(client)
    assert res.status_code == 202, res.text
    assert res.json == {"id": REPORT, "status": "queued"}
    assert res.headers["Cache-Control"] == "no-store"
    call = upstream.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "http://e-report:8080/api/v1/reports"
    assert json.loads(call["data"]) == ORDER
    assert call["headers"]["X-Report-Token"] == TOKEN
    assert call["headers"]["X-Report-User"] == "alice"
    assert call["headers"]["Content-Type"] == "application/json"


def test_list_get_and_delete_forward(app, client, report_env, cert_user, upstream):
    _user(app)
    upstream.status = 200
    upstream.body = {"items": []}
    assert client.get("/Marti/api/taklab/report").json == {"items": []}
    upstream.body = {"id": REPORT, "status": "done"}
    assert client.get(f"/Marti/api/taklab/report/{REPORT}").json["status"] == "done"
    upstream.body = {"deleted": True}
    assert client.delete(f"/Marti/api/taklab/report/{REPORT}").json == {"deleted": True}
    assert [(c["method"], c["url"]) for c in upstream.calls] == [
        ("GET", "http://e-report:8080/api/v1/reports"),
        ("GET", f"http://e-report:8080/api/v1/reports/{REPORT}"),
        ("DELETE", f"http://e-report:8080/api/v1/reports/{REPORT}"),
    ]
    assert all(c["headers"]["X-Report-User"] == "alice" for c in upstream.calls)


@pytest.mark.parametrize("status", [400, 404, 409, 429, 503])
def test_service_errors_passed_through(app, client, report_env, cert_user, upstream, status):
    _user(app)
    upstream.status = status
    upstream.body = {"detail": "queue_full", "error": "Masz już 3 raporty w kolejce."}
    res = _order(client)
    assert res.status_code == status
    assert res.json == upstream.body


def test_download_streams_file_with_upstream_headers(app, client, report_env, cert_user, upstream):
    _user(app)
    upstream.status = 200
    upstream.content = b"%PDF-1.7 raport"
    upstream.headers = {"Content-Type": "application/pdf",
                        "Content-Disposition": "attachment; filename=raport.pdf"}
    res = client.get(f"/Marti/api/taklab/report/{REPORT}.pdf")
    assert res.status_code == 200
    assert res.data == b"%PDF-1.7 raport"
    assert res.headers["Content-Type"] == "application/pdf"
    assert res.headers["Content-Disposition"] == "attachment; filename=raport.pdf"
    assert res.headers["Cache-Control"] == "no-store"
    call = upstream.calls[0]
    assert call["url"] == f"http://e-report:8080/api/v1/reports/{REPORT}.pdf"
    assert call["stream"] is True
    assert call["headers"]["X-Report-User"] == "alice"

    upstream.content = b"Raport tekstowy"
    upstream.headers = {"Content-Type": "text/plain; charset=utf-8"}
    res = client.get(f"/Marti/api/taklab/report/{REPORT}.txt")
    assert res.data == b"Raport tekstowy"
    assert upstream.calls[1]["url"].endswith(f"/{REPORT}.txt")


def test_download_not_ready_passes_409(app, client, report_env, cert_user, upstream):
    _user(app)
    upstream.status = 409
    upstream.body = {"detail": "not_ready", "error": "Raport nie jest jeszcze gotowy."}
    res = client.get(f"/Marti/api/taklab/report/{REPORT}.pdf")
    assert res.status_code == 409
    assert res.json["detail"] == "not_ready"


def test_report_bad_id_400(app, client, report_env, cert_user, upstream):
    _user(app)
    assert client.get("/Marti/api/taklab/report/a_b").status_code == 400
    assert client.delete("/Marti/api/taklab/report/" + "a" * 65).status_code == 400
    assert client.get("/Marti/api/taklab/report/a.b.pdf").status_code == 400
    assert client.get("/Marti/api/taklab/report/%2E%2E.txt").status_code == 400
    assert upstream.calls == []


def test_service_down_503(app, client, report_env, cert_user, upstream):
    _user(app)
    upstream.raise_error = requests.ConnectionError("down")
    res = _order(client)
    assert res.status_code == 503
    assert res.json["error"]
    assert client.get(f"/Marti/api/taklab/report/{REPORT}.pdf").status_code == 503


def test_service_unexpected_status_503(app, client, report_env, cert_user, upstream):
    _user(app)
    upstream.status = 500
    upstream.body = {"detail": "boom"}
    assert _order(client).status_code == 503


def test_report_off_404(app, client, report_off, cert_user, upstream):
    _user(app)
    assert _order(client).status_code == 404
    assert upstream.calls == []


def test_order_body_and_password_not_logged(app, client, report_env, cert_user, upstream, caplog):
    _user(app)
    with caplog.at_level("DEBUG"):
        _order(client)
        upstream.raise_error = requests.ConnectionError("down " + TOKEN)
        _order(client)
    assert "TAJNE-HASLO" not in caplog.text
    assert TOKEN not in caplog.text


def test_config_has_report_section(app, client, report_env, cert_user):
    _user(app)
    assert client.get("/Marti/api/taklab/config").json["report"] == {"enabled": True}


def test_config_report_null_when_off(app, client, report_off, cert_user, monkeypatch):
    _user(app)
    body = client.get("/Marti/api/taklab/config").json
    assert "report" in body and body["report"] is None
    monkeypatch.setenv("OTS_TAKLAB_REPORT_URL", "http://e-report:8080")
    assert client.get("/Marti/api/taklab/config").json["report"] is None


# --- Trasy wewnętrzne ---


@pytest.fixture
def stored(app, tmp_path):
    """Data package and mission content rows with files where OTS keeps them."""
    from datetime import datetime

    from opentakserver.extensions import db
    from opentakserver.models.DataPackage import DataPackage
    from opentakserver.models.MissionContent import MissionContent

    uploads = tmp_path / "uploads"
    missions = tmp_path / "missions"
    uploads.mkdir()
    missions.mkdir()
    app.config["UPLOAD_FOLDER"] = str(uploads)
    app.config["OTS_DATA_FOLDER"] = str(tmp_path)

    def add(kind, file_hash, filename, mime, data):
        with app.app_context():
            if kind == "package":
                (uploads / f"{file_hash}{os.path.splitext(filename)[1]}").write_bytes(data)
                db.session.add(DataPackage(
                    filename=filename, hash=file_hash, creator_uid=None, submission_time=datetime.now(),
                    mime_type=mime, size=len(data)))
            else:
                (missions / filename).write_bytes(data)
                db.session.add(MissionContent(
                    filename=filename, hash=file_hash, uid=file_hash, mime_type=mime, size=len(data)))
            db.session.commit()

    return add


def _internal_get(client, file_hash=HASH, token=TOKEN, **headers):
    if token is not None:
        headers["X-Report-Token"] = token
    return client.get(f"/api/taklab/report/file/{file_hash}", headers=headers)


def test_internal_file_requires_token(app, client, report_env, stored):
    stored("package", HASH, "paczka.zip", "application/zip", b"PK-dane")
    assert _internal_get(client, token=None).status_code == 404
    assert _internal_get(client, token="zly").status_code == 404
    # Przez nginx panelu (/api/ z internetu) - nawet z dobrym żetonem.
    assert _internal_get(client, **{"X-Forwarded-For": "203.0.113.7"}).status_code == 404
    res = _internal_get(client)
    assert res.status_code == 200
    assert res.data == b"PK-dane"
    assert res.mimetype == "application/zip"


def test_internal_routes_absent_without_token_env(app, client, report_off, stored):
    stored("package", HASH, "paczka.zip", "application/zip", b"PK-dane")
    assert _internal_get(client, token="").status_code == 404
    assert _internal_get(client).status_code == 404
    res = client.post("/api/taklab/report/mission-password", json={"name": "m", "password": "p"},
                      headers={"X-Report-Token": TOKEN})
    assert res.status_code == 404


def test_internal_file_from_mission_content(app, client, report_env, stored):
    other = "b" * 64
    stored("content", other, "zdjecie.jpg", "image/jpeg", b"\xff\xd8\xffJPEG")
    res = _internal_get(client, other)
    assert res.status_code == 200
    assert res.data == b"\xff\xd8\xffJPEG"
    assert res.mimetype == "image/jpeg"


def test_internal_file_bad_hash_400_and_missing_404(app, client, report_env, stored):
    assert _internal_get(client, "ABC" + "a" * 61).status_code == 400
    assert _internal_get(client, "a" * 31).status_code == 400
    assert _internal_get(client, "a" * 65).status_code == 400
    assert _internal_get(client, "g" * 64).status_code == 400
    assert _internal_get(client, "c" * 64).status_code == 404


def test_internal_file_size_limit(app, client, report_env, stored):
    big = "d" * 64
    stored("package", big, "duza.zip", "application/zip", b"x" * (5 * 1024 * 1024 + 1))
    assert _internal_get(client, big).status_code == 413
    edge = "e" * 64
    stored("package", edge, "rowna.zip", "application/zip", b"x" * (5 * 1024 * 1024))
    assert _internal_get(client, edge).status_code == 200


def _mission(app, name, password=None):
    from flask_security import hash_password

    from opentakserver.extensions import db
    from opentakserver.models.Mission import Mission

    with app.app_context():
        db.session.add(Mission(
            name=name, guid=name, password_protected=bool(password),
            password=hash_password(password) if password else None))
        db.session.commit()


def _check(client, body, token=TOKEN):
    return client.post("/api/taklab/report/mission-password", json=body,
                       headers={"X-Report-Token": token})


def test_mission_password_check(app, client, report_env, caplog):
    _mission(app, "Ćwiczenie", "TAJNE-HASLO")
    _mission(app, "Otwarta")
    with caplog.at_level("DEBUG"):
        assert _check(client, {"name": "Ćwiczenie", "password": "TAJNE-HASLO"}).json == {"ok": True}
        assert _check(client, {"name": "Ćwiczenie", "password": "zle"}).json == {"ok": False}
        assert _check(client, {"name": "Nie ma", "password": "TAJNE-HASLO"}).json == {"ok": False}
        assert _check(client, {"name": "Otwarta", "password": "cokolwiek"}).json == {"ok": False}
        assert _check(client, {"name": "Ćwiczenie"}).json == {"ok": False}
        assert _check(client, ["nie", "slownik"]).json == {"ok": False}
    assert "TAJNE-HASLO" not in caplog.text
    assert _check(client, {"name": "Ćwiczenie", "password": "TAJNE-HASLO"}, token="zly").status_code == 404


def test_unknown_mission_still_verifies_a_hash(app, client, report_env, monkeypatch):
    module = importlib.import_module(MODULE)
    calls = []
    real = module.verify_password

    def counting(password, hashed):
        calls.append(password)
        return real(password, hashed)

    monkeypatch.setattr(module, "verify_password", counting)
    _mission(app, "Otwarta")
    assert _check(client, {"name": "Nie ma", "password": "p1"}).json == {"ok": False}
    assert _check(client, {"name": "Otwarta", "password": "p2"}).json == {"ok": False}
    assert calls == ["p1", "p2"]
