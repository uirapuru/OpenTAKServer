"""taklab: /api/taklab/vision - trasy administratora i plik nagrania po podpisie."""
import importlib
import time
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import pytest
import requests

from opentakserver import taklab_vision as tv

from tests.test_taklab_recordings import make_user

HOST = "public-ots.taklab.eu"
TOKEN = "vision-token"
DATA = bytes(range(256)) * 8


class FakeResponse:
    def __init__(self, status=200, body=None, content=b""):
        self.status_code = status
        self._body = body
        self.content = content

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeSession:
    def __init__(self):
        self.calls = []
        self.routes = {}
        self.down = False

    def request(self, method, url, headers=None, json=None, **kwargs):
        if self.down:
            raise requests.ConnectionError("boom")
        self.calls.append((method, url, json))
        path = urlparse(url).path
        return self.routes.get((method, path), FakeResponse(404, {"error": "brak"}))


@pytest.fixture
def vision_env(monkeypatch, tmp_path):
    root = tmp_path / "recordings"
    root.mkdir()
    monkeypatch.setenv("OTS_TAKLAB_RECORDINGS_DIR", str(root))
    monkeypatch.setenv("OTS_TAKLAB_RECORDINGS_MIN_FREE_BYTES", "0")
    monkeypatch.setenv("OTS_TAKLAB_VISION_URL", "http://vision")
    monkeypatch.setenv("OTS_TAKLAB_VISION_TOKEN", TOKEN)
    monkeypatch.setenv("OTS_TAKLAB_VISION_CLIENT", "public")
    monkeypatch.setenv("OTS_TAKLAB_HOST", HOST)
    fake = FakeSession()
    monkeypatch.setattr(tv, "_session", fake)
    return fake, root


@pytest.fixture
def as_viewer(app, monkeypatch):
    api_module = importlib.import_module("opentakserver.blueprints.ots_api.taklab_vision_api")
    make_user(app, "admin", admin=True)
    make_user(app, "alice")
    current = {"name": None}

    def viewer():
        if current["name"] is None:
            return None
        return app.security.datastore.find_user(username=current["name"])

    monkeypatch.setattr(api_module, "_viewer", viewer)
    return current


def add_recording(app, root, kind="video", state="ready", deleted=False, with_file=True, name="Nagranie A",
                  minutes_ago=10):
    from opentakserver.extensions import db
    from opentakserver.models.TaklabRecording import TaklabRecording

    rec_id = str(uuid.uuid4())
    rel = f"{rec_id}.mp4"
    if with_file:
        (root / rel).write_bytes(DATA)
    start = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=minutes_ago)
    with app.app_context():
        rec = TaklabRecording()
        rec.id, rec.kind, rec.state, rec.deleted = rec_id, kind, state, deleted
        rec.name, rec.account = name, "alice"
        rec.started_at, rec.ended_at = start, start + timedelta(seconds=90)
        rec.created_at = start
        rec.file_path, rec.mime = rel, "video/mp4"
        db.session.add(rec)
        db.session.commit()
    return rec_id


def signed_path(rec_id, now=None, token=TOKEN):
    cfg = tv.VisionSettings("http://vision", token, "public", HOST)
    url = tv.signed_file_url(cfg, rec_id, now if now is not None else time.time())
    parsed = urlparse(url)
    return f"{parsed.path}?{parsed.query}"


def test_routes_need_admin(client, vision_env, as_viewer):
    assert client.get("/api/taklab/vision/sources").status_code == 401
    as_viewer["name"] = "alice"
    for method, url in (
        ("get", "/api/taklab/vision/sources"),
        ("get", "/api/taklab/vision/analyses"),
        ("post", "/api/taklab/vision/analyses"),
        ("post", "/api/taklab/vision/analyses/1/stop"),
    ):
        res = getattr(client, method)(url)
        assert res.status_code == 403, url
        assert res.json["error"] == "Analiza wideo jest dostępna tylko dla administratora."


def test_status_reports_enabled_and_disabled(client, vision_env, as_viewer, monkeypatch):
    as_viewer["name"] = "admin"
    assert client.get("/api/taklab/vision/status").json == {"enabled": True}
    monkeypatch.setenv("OTS_TAKLAB_VISION_TOKEN", "")
    res = client.get("/api/taklab/vision/status")
    assert res.status_code == 200 and res.json == {"enabled": False}
    assert client.get("/api/taklab/vision/sources").status_code == 404


def test_sources_list_live_from_service_and_ready_videos_only(app, client, vision_env, as_viewer):
    fake, root = vision_env
    as_viewer["name"] = "admin"
    fake.routes[("GET", "/v1/env/streams")] = FakeResponse(200, {"streams": [{"path": "alice"}]})
    good = add_recording(app, root, minutes_ago=5)
    older = add_recording(app, root, name="", minutes_ago=30)
    add_recording(app, root, kind="voice")
    add_recording(app, root, state="recording")
    add_recording(app, root, deleted=True)
    add_recording(app, root, with_file=False)
    res = client.get("/api/taklab/vision/sources")
    assert res.status_code == 200
    assert res.json["live"] == [{"path": "alice"}]
    assert [r["id"] for r in res.json["recordings"]] == [good, older]
    first = res.json["recordings"][0]
    assert first["name"] == "Nagranie A" and first["account"] == "alice" and first["duration_s"] == 90.0
    assert res.json["recordings"][1]["name"]


def test_start_recording_analysis_sends_signed_url_and_creator(app, client, vision_env, as_viewer):
    fake, root = vision_env
    as_viewer["name"] = "admin"
    rec_id = add_recording(app, root)
    fake.routes[("POST", "/v1/env/analyses")] = FakeResponse(201, {"id": 7, "state": "queued"})
    res = client.post("/api/taklab/vision/analyses", json={"source": "recording", "recording_id": rec_id})
    assert res.status_code == 201 and res.json["id"] == 7
    method, url, body = fake.calls[-1]
    # Keys read by the service start route (taklab vision server.py start_analysis).
    assert set(body) == {"source", "recording_id", "label", "account", "started_at", "duration_s", "url",
                         "created_by"}
    assert body["source"] == "recording" and body["recording_id"] == rec_id
    assert body["created_by"] == "admin" and body["label"] == "Nagranie A"
    assert body["account"] == "alice" and body["duration_s"] == 90.0
    assert body["url"].startswith(f"https://{HOST}/api/taklab/vision/recordings/")
    assert isinstance(body["started_at"], float)


def test_start_recording_without_host_409(app, client, vision_env, as_viewer, monkeypatch):
    fake, root = vision_env
    as_viewer["name"] = "admin"
    rec_id = add_recording(app, root)
    monkeypatch.setenv("OTS_TAKLAB_HOST", "")
    res = client.post("/api/taklab/vision/analyses", json={"source": "recording", "recording_id": rec_id})
    assert res.status_code == 409
    assert res.json["error"] == "Serwer nie ma publicznego adresu, więc nagrań nie da się analizować."
    assert fake.calls == []


def test_start_recording_unknown_404(client, vision_env, as_viewer):
    as_viewer["name"] = "admin"
    res = client.post("/api/taklab/vision/analyses", json={"source": "recording", "recording_id": "nope"})
    assert res.status_code == 404 and res.json["error"] == "Nie ma takiego nagrania."


def test_start_live_passes_path(client, vision_env, as_viewer):
    fake, _ = vision_env
    as_viewer["name"] = "admin"
    fake.routes[("POST", "/v1/env/analyses")] = FakeResponse(201, {"id": 3})
    res = client.post("/api/taklab/vision/analyses", json={"source": "live", "path": "alice"})
    assert res.status_code == 201
    assert fake.calls[-1][2] == {"source": "live", "path": "alice", "created_by": "admin"}


def test_bad_start_body_400(client, vision_env, as_viewer):
    as_viewer["name"] = "admin"
    for body in ({}, {"source": "x"}, {"source": "live"}):
        res = client.post("/api/taklab/vision/analyses", json=body)
        assert res.status_code == 400 and res.json["error"] == "Wybierz transmisję albo nagranie."


def test_faces_thumb_urls_point_at_ots(client, vision_env, as_viewer):
    fake, _ = vision_env
    as_viewer["name"] = "admin"
    fake.routes[("GET", "/v1/env/analyses/5/faces")] = FakeResponse(
        200, {"items": [{"id": 2, "thumb_url": "/v1/env/analyses/5/persons/2/thumb.jpg"}]}
    )
    res = client.get("/api/taklab/vision/analyses/5/faces")
    assert res.json["items"][0]["thumb_url"] == "/api/taklab/vision/analyses/5/persons/2/thumb.jpg"


def test_thumb_proxied_without_caching(client, vision_env, as_viewer):
    fake, _ = vision_env
    as_viewer["name"] = "admin"
    fake.routes[("GET", "/v1/env/analyses/5/persons/2/thumb.jpg")] = FakeResponse(200, content=b"\xff\xd8jpg")
    res = client.get("/api/taklab/vision/analyses/5/persons/2/thumb.jpg")
    assert res.status_code == 200 and res.data == b"\xff\xd8jpg"
    assert res.mimetype == "image/jpeg" and res.headers["Cache-Control"] == "private, no-store"


def test_service_down_503(client, vision_env, as_viewer):
    fake, _ = vision_env
    as_viewer["name"] = "admin"
    fake.down = True
    res = client.get("/api/taklab/vision/analyses")
    assert res.status_code == 503 and res.json["error"] == "Usługa analizy wideo nie odpowiada."


def test_service_error_passes_status_and_message(client, vision_env, as_viewer):
    fake, _ = vision_env
    as_viewer["name"] = "admin"
    fake.routes[("POST", "/v1/env/analyses")] = FakeResponse(409, {"error": "Ta transmisja jest już analizowana."})
    res = client.post("/api/taklab/vision/analyses", json={"source": "live", "path": "alice"})
    assert res.status_code == 409 and res.json["error"] == "Ta transmisja jest już analizowana."


def test_file_route_serves_with_valid_signature_and_range(app, client, vision_env):
    _, root = vision_env
    rec_id = add_recording(app, root)
    res = client.get(signed_path(rec_id))
    assert res.status_code == 200 and res.data == DATA
    assert res.headers["Cache-Control"] == "private, no-store"
    assert res.headers["X-Accel-Buffering"] == "no"
    part = client.get(signed_path(rec_id), headers={"Range": "bytes=0-9"})
    assert part.status_code == 206 and part.data == DATA[:10]


def test_file_route_rejects_bad_signatures(app, client, vision_env):
    _, root = vision_env
    rec_id = add_recording(app, root)
    other = add_recording(app, root)
    good = signed_path(rec_id)
    assert client.get(signed_path(other).replace(other, rec_id)).status_code == 403
    tampered = good[:-1] + ("0" if good[-1] != "0" else "1")
    assert client.get(tampered).status_code == 403
    assert client.get(signed_path(rec_id, now=time.time() - tv.FILE_URL_TTL_S - 100)).status_code == 403
    assert client.get(signed_path(rec_id, now=time.time() + 2 * tv.FILE_URL_TTL_S)).status_code == 403
    assert client.get(signed_path(rec_id, token="inny")).status_code == 403
    assert client.get(f"/api/taklab/vision/recordings/{rec_id}/file").status_code == 403
    res = client.get(tampered)
    assert res.json["error"] == "Podpis adresu jest nieprawidłowy albo wygasł."


def test_file_route_404_for_unavailable_recordings(app, client, vision_env):
    _, root = vision_env
    for kwargs in ({"deleted": True}, {"kind": "voice"}, {"state": "recording"}, {"with_file": False}):
        rec_id = add_recording(app, root, **kwargs)
        assert client.get(signed_path(rec_id)).status_code == 404, kwargs


def test_file_route_404_when_analysis_disabled(app, client, vision_env, monkeypatch):
    _, root = vision_env
    rec_id = add_recording(app, root)
    path = signed_path(rec_id)
    monkeypatch.setenv("OTS_TAKLAB_VISION_TOKEN", "")
    assert client.get(path).status_code == 404
