"""taklab: transmisje na żywo /api/taklab/video/live.

Projekt: ~/taklab docs/superpowers/specs/2026-11-25-live-video-design.md, część 1.
Odpowiedzi MediaMTX to prawdziwe odczyty z bluenviron/mediamtx:1.20.1-ffmpeg
(tests/fixtures/mediamtx_1_20_1).
"""

import importlib
import json
import pathlib
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
import requests

API = "opentakserver.blueprints.ots_api.taklab_live_video_api"
FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "mediamtx_1_20_1"
URL = "/api/taklab/video/live"


def fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


@pytest.fixture
def module():
    mod = importlib.import_module(API)
    mod._samples.clear()
    return mod


@pytest.fixture
def as_cert(monkeypatch, module):
    current = {"username": None}

    def fake_cert():
        if current["username"] is None:
            return None
        cert = MagicMock()
        cert.subject.get_attributes_for_oid.return_value = [MagicMock(value=current["username"])]
        return cert

    monkeypatch.setattr(module, "verify_client_cert", fake_cert)
    return current


class FakeMediaMTX:
    """Udaje lokalne API MediaMTX: url -> odpowiedź JSON, wyjątek albo 404."""

    def __init__(self):
        self.routes = {}
        self.calls = []
        self.error = None

    def add(self, resource, name, body):
        self.routes[f"/v3/{resource}/get/{name}"] = body

    def __call__(self, url, timeout=None):
        self.calls.append((url, timeout))
        if self.error is not None:
            raise self.error
        for suffix, body in self.routes.items():
            if url.endswith(suffix):
                response = MagicMock(status_code=200)
                response.json.return_value = body
                return response
        return MagicMock(status_code=404, json=MagicMock(return_value={"status": "error"}))


@pytest.fixture
def mediamtx(monkeypatch, module):
    fake = FakeMediaMTX()
    monkeypatch.setattr(module.requests, "get", fake)
    return fake


def make_user(app, username):
    from opentakserver.extensions import db

    with app.app_context():
        app.security.datastore.create_user(username=username, password="x")
        db.session.commit()


def add_stream(app, path, username=None, ready=True):
    from opentakserver.extensions import db
    from opentakserver.models.VideoStream import VideoStream

    with app.app_context():
        stream = VideoStream()
        stream.path = path
        stream.username = username
        stream.ready = ready
        stream.mediamtx_settings = ""
        db.session.add(stream)
        db.session.commit()


def rtsp_operator(mediamtx):
    path = fixture("path_rtsp_operator")
    session = fixture("rtspsession_tcp_av")
    mediamtx.add("paths", "operator", path)
    mediamtx.add("rtspsessions", path["source"]["id"], session)
    return path, session


def test_requires_login(client, mediamtx):
    response = client.get(URL)
    assert response.status_code == 401
    assert response.headers["Cache-Control"] == "no-store"


def test_any_account_with_certificate_sees_streams(app, client, as_cert, mediamtx):
    make_user(app, "operator")
    make_user(app, "viewer")
    add_stream(app, "operator", "operator")
    add_stream(app, "stopped", "operator", ready=False)
    rtsp_operator(mediamtx)
    as_cert["username"] = "viewer"

    response = client.get(URL)

    assert response.status_code == 200
    body = response.json
    assert body["generated_at"].endswith("Z")
    assert [s["path"] for s in body["streams"]] == ["operator"]
    stream = body["streams"][0]
    assert stream["username"] == "operator"
    assert stream["started_at"] == "2026-11-25T14:58:27Z"
    assert stream["tech"] == {
        "source": "rtspSession",
        "transport": "TCP",
        "readers": 1,
        "bytes_received": 26604049,
        "bitrate_kbps": stream["tech"]["bitrate_kbps"],
        "packets_lost": 0,
        "jitter_ms": None,  # wideo + dźwięk: średnia w różnych jednostkach zegara RTP
        "video": {"codec": "H264", "width": 1280, "height": 720, "profile": "High", "level": "3.1", "fps": None},
        "audio": {"codec": "MPEG-4 Audio", "sample_rate": 44100, "channels": 1},
    }
    assert all(timeout == 1.5 for _, timeout in mediamtx.calls)


def test_unknown_or_inactive_certificate_is_refused(app, client, as_cert, mediamtx):
    as_cert["username"] = "nobody"
    assert client.get(URL).status_code == 401


def test_session_token_works(app, auth, mediamtx):
    add_stream(app, "operator", "TestUser")
    rtsp_operator(mediamtx)
    response = auth.get(URL)
    assert response.status_code == 200
    assert response.json["streams"][0]["tech"]["video"]["width"] == 1280


def test_no_remote_addresses_in_reply(app, client, as_cert, mediamtx):
    make_user(app, "viewer")
    add_stream(app, "operator")
    _, session = rtsp_operator(mediamtx)
    as_cert["username"] = "viewer"
    text = client.get(URL).get_data(as_text=True)
    assert session["remoteAddr"].split(":")[0] not in text
    assert "remoteAddr" not in text


def test_mediamtx_down_gives_null_tech(app, client, as_cert, mediamtx):
    make_user(app, "viewer")
    add_stream(app, "a")
    add_stream(app, "b")
    mediamtx.error = requests.Timeout("timeout")
    as_cert["username"] = "viewer"

    response = client.get(URL)

    assert response.status_code == 200
    assert [(s["path"], s["tech"], s["started_at"]) for s in response.json["streams"]] == [
        ("a", None, None),
        ("b", None, None),
    ]
    # po pierwszym błędzie sieci nie pyta dalej, żeby nie czekać po 1,5 s na każdą ścieżkę
    assert len(mediamtx.calls) == 1


def test_path_unknown_to_mediamtx_gives_null_tech(app, client, as_cert, mediamtx):
    make_user(app, "viewer")
    add_stream(app, "ghost")
    as_cert["username"] = "viewer"
    assert client.get(URL).json["streams"][0]["tech"] is None


def test_session_lookup_failure_keeps_path_data(app, client, as_cert, mediamtx):
    make_user(app, "viewer")
    add_stream(app, "operator")
    mediamtx.add("paths", "operator", fixture("path_rtsp_operator"))
    as_cert["username"] = "viewer"
    tech = client.get(URL).json["streams"][0]["tech"]
    assert tech["source"] == "rtspSession"
    assert tech["transport"] is None and tech["packets_lost"] is None
    assert tech["video"]["codec"] == "H264"


def test_rtmp_source(app, client, as_cert, mediamtx):
    make_user(app, "viewer")
    add_stream(app, "rtmpcam")
    path = fixture("path_rtmp")
    mediamtx.add("paths", "rtmpcam", path)
    mediamtx.add("rtmpconns", path["source"]["id"], fixture("rtmpconn"))
    as_cert["username"] = "viewer"
    tech = client.get(URL).json["streams"][0]["tech"]
    assert tech["source"] == "rtmpConn"
    assert tech["transport"] is None
    assert tech["packets_lost"] is None and tech["jitter_ms"] is None
    assert tech["readers"] == 0
    assert tech["video"]["level"] == "3"
    assert tech["audio"] is None
    assert any("/v3/rtmpconns/get/" in url for url, _ in mediamtx.calls)


def test_srt_source_reports_loss(app, client, as_cert, mediamtx):
    make_user(app, "viewer")
    add_stream(app, "srtcam")
    path = fixture("path_srt")
    mediamtx.add("paths", "srtcam", path)
    mediamtx.add("srtconns", path["source"]["id"], fixture("srtconn"))
    as_cert["username"] = "viewer"
    tech = client.get(URL).json["streams"][0]["tech"]
    assert tech["source"] == "srtConn"
    assert tech["packets_lost"] == 0


def test_jitter_in_ms_for_single_track(module):
    session = fixture("rtspsession_udp_video_only")
    tracks = [{"codec": "H264", "codecProps": {"width": 640, "height": 360}}]
    assert module._jitter_ms(session["rtpPacketsJitter"], tracks) == round(2.4695004048150104 / 90, 2)
    assert module._jitter_ms(900, [{"codec": "MPEG-4 Audio", "codecProps": {"sampleRate": 44100}}]) == 20.41
    assert module._jitter_ms(675.9, [{"codec": "H264"}, {"codec": "Opus"}]) is None


def test_bitrate_first_read_is_average_since_ready(module):
    ready = datetime.now(timezone.utc) - timedelta(seconds=10)
    raw = ready.strftime("%Y-%m-%dT%H:%M:%S.%f") + "123Z"
    kbps = module._bitrate_kbps("p", 2_500_000, raw, module.parse_mediamtx_time(raw))
    assert kbps == pytest.approx(2000, rel=0.02)


def test_bitrate_from_two_samples(module, monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(module.time, "monotonic", lambda: clock["t"])
    ready = datetime.now(timezone.utc) - timedelta(seconds=100)
    raw = ready.strftime("%Y-%m-%dT%H:%M:%SZ")

    module._bitrate_kbps("p", 1_000_000, raw, ready)
    clock["t"] += 1.0
    # okno krótsze niż 2 s: bez nowego pomiaru, nadal średnia
    assert module._bitrate_kbps("p", 1_100_000, raw, ready) == pytest.approx(88, rel=0.05)
    clock["t"] += 3.0
    assert module._bitrate_kbps("p", 1_500_000, raw, ready) == 1000.0
    clock["t"] += 1.0
    assert module._bitrate_kbps("p", 1_600_000, raw, ready) == 1000.0  # ostatni pomiar
    clock["t"] += 2.0
    assert module._bitrate_kbps("p", 1_725_000, raw, ready) == 600.0  # 3 s od próbki bazowej


def test_bitrate_resets_when_stream_restarts(module, monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(module.time, "monotonic", lambda: clock["t"])
    first = datetime.now(timezone.utc) - timedelta(seconds=100)
    module._bitrate_kbps("p", 10_000_000, first.strftime("%Y-%m-%dT%H:%M:%SZ"), first)
    clock["t"] += 5
    second = datetime.now(timezone.utc) - timedelta(seconds=4)
    kbps = module._bitrate_kbps("p", 500_000, second.strftime("%Y-%m-%dT%H:%M:%SZ"), second)
    assert kbps == pytest.approx(1000, rel=0.3)


def test_parse_mediamtx_time(module):
    moment = module.parse_mediamtx_time("2026-11-25T14:58:27.437948712Z")
    assert moment == datetime(2026, 11, 25, 14, 58, 27, 437948, tzinfo=timezone.utc)
    assert module.parse_mediamtx_time("nonsense") is None
    assert module.parse_mediamtx_time(None) is None
