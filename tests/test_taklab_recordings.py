"""taklab: rejestr nagrań /api/taklab/recordings (wideo, głosówki, skany OCR).

Projekt: ~/taklab docs/superpowers/specs/2026-11-24-recordings-design.md.
Sprawdza uprawnienia po grupach (te same zasady co routing CoT), CRUD, Range,
sekret przyjmowania, treść znacznika CoT, łączenie segmentów wideo, retencję
i miejsce na dysku.
"""

import importlib
import io
import json
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

API = "opentakserver.blueprints.ots_api.taklab_recordings_api"
CORE = "opentakserver.taklab_recordings"
OGG = b"OggS" + b"\x00" * 200
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 300
INGEST_TOKEN = "sekret-srodowiska"
OCR_TOKEN = "zeton-ocr"


def now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.fixture
def rec_env(monkeypatch, tmp_path):
    root = tmp_path / "recordings"
    root.mkdir()
    monkeypatch.setenv("OTS_TAKLAB_RECORDINGS_DIR", str(root))
    monkeypatch.setenv("OTS_TAKLAB_RECORDINGS_INGEST_TOKEN", INGEST_TOKEN)
    monkeypatch.setenv("OTS_TAKLAB_OCR_TOKEN", OCR_TOKEN)
    monkeypatch.setenv("OTS_TAKLAB_RECORDINGS_RETENTION_DAYS", "30")
    monkeypatch.setenv("OTS_TAKLAB_RECORDINGS_LIMIT_BYTES", str(10**9))
    monkeypatch.setenv("OTS_TAKLAB_RECORDINGS_MIN_FREE_BYTES", "0")
    monkeypatch.setenv("OTS_TAKLAB_RECORDINGS_LINK", "https://public-mapa.taklab.eu/")
    return root


@pytest.fixture
def published(monkeypatch):
    """CoT, które OTS wysłałby do grup autora: (xml, konto)."""
    sent = []
    core = importlib.import_module(CORE)
    monkeypatch.setattr(core, "_route", lambda xml, user: sent.append((xml, user.username)))
    return sent


@pytest.fixture
def as_cert(monkeypatch):
    """Żądanie z certyfikatem klienta konta (CloudTAK przez marti-tls)."""
    module = importlib.import_module(API)
    current = {"username": None}

    def fake_cert():
        if current["username"] is None:
            return None
        cert = MagicMock()
        cert.subject.get_attributes_for_oid.return_value = [MagicMock(value=current["username"])]
        return cert

    monkeypatch.setattr(module, "verify_client_cert", fake_cert)
    return current


def make_user(app, username, admin=False):
    from opentakserver.extensions import db

    with app.app_context():
        app.security.datastore.find_or_create_role(name="administrator", permissions={"administrator"})
        user = app.security.datastore.create_user(
            username=username, password="x", roles=["administrator"] if admin else []
        )
        db.session.commit()
        return user.id


def join(app, username, group_name, direction, enabled=True):
    from opentakserver.extensions import db
    from opentakserver.models.Group import Group
    from opentakserver.models.GroupUser import GroupUser

    with app.app_context():
        user = app.security.datastore.find_user(username=username)
        group = db.session.query(Group).filter_by(name=group_name).first()
        if group is None:
            group = Group()
            group.name = group_name
            group.type = Group.SYSTEM
            group.bitpos = 3
            db.session.add(group)
            db.session.commit()
        db.session.add(GroupUser(user_id=user.id, group_id=group.id, direction=direction, enabled=enabled))
        db.session.commit()


def device(app, username, uid, callsign, points):
    """EUD konta i jego własne pozycje: [(lat, lon, czas), ...]."""
    from opentakserver.extensions import db
    from opentakserver.models.EUD import EUD
    from opentakserver.models.Point import Point

    with app.app_context():
        user = app.security.datastore.find_user(username=username)
        db.session.add(EUD(uid=uid, callsign=callsign, user_id=user.id))
        db.session.commit()
        for lat, lon, ts in points:
            db.session.add(Point(uid=uid, device_uid=uid, latitude=lat, longitude=lon, timestamp=ts))
        db.session.commit()


def ingest(client, kind="voice", data=OGG, mime="audio/ogg", token=INGEST_TOKEN, headers=None, **fields):
    form = {"kind": kind, **fields}
    form["file"] = (io.BytesIO(data), "plik", mime)
    hdrs = {"Authorization": f"Bearer {token}"} if token else {}
    hdrs.update(headers or {})
    return client.post("/api/taklab/recordings/ingest", data=form, headers=hdrs,
                       content_type="multipart/form-data")


@pytest.fixture
def world(app, rec_env, published, as_cert):
    """alice i bob w grupie niebiescy, carol w czerwoni, dave bez grup, admin."""
    for name in ("alice", "bob", "carol", "dave", "erin"):
        make_user(app, name)
    make_user(app, "admin", admin=True)
    for name in ("alice", "bob"):
        join(app, name, "niebiescy", "IN")
        join(app, name, "niebiescy", "OUT")
    join(app, "carol", "czerwoni", "IN")
    join(app, "carol", "czerwoni", "OUT")
    t0 = now() - timedelta(minutes=5)
    device(app, "alice", "ANDROID-alice", "Alfa", [(52.0, 21.0, t0), (52.001, 21.001, t0 + timedelta(seconds=30))])
    device(app, "dave", "ANDROID-dave", "Delta", [(50.0, 19.0, t0)])
    return {"t0": t0, "sent": published, "cert": as_cert}


def as_user(world, name):
    world["cert"]["username"] = name


# --- przyjmowanie -------------------------------------------------------------


def test_ingest_wymaga_sekretu(client, world):
    assert ingest(client, token=None).status_code == 401
    assert ingest(client, token="zly").status_code == 401


def test_ingest_odrzuca_zadanie_przez_posrednika(client, world):
    res = ingest(client, author="Alfa (alice)", headers={"X-Forwarded-For": "1.2.3.4"})
    assert res.status_code == 404


def test_zeton_ocr_przyjmuje_tylko_skany(client, world):
    assert ingest(client, token=OCR_TOKEN, author="Alfa (alice)").status_code == 403
    res = ingest(client, kind="scan", data=JPEG, mime="image/jpeg", token=OCR_TOKEN,
                 account="alice", text="Rozkaz nr 1")
    assert res.status_code == 201, res.text
    assert res.json["kind"] == "scan" and res.json["has_text"] is True


def test_ingest_sprawdza_typ_pliku(client, world):
    assert ingest(client, data=b"nie-ogg" * 10, author="Alfa (alice)").status_code == 415
    assert ingest(client, mime="video/mp4", author="Alfa (alice)").status_code == 415


def test_glosowka_dostaje_konto_znak_pozycje_i_slad(client, world, app):
    start = world["t0"] + timedelta(seconds=10)
    res = ingest(client, author="Alfa (alice)", started_at=start.isoformat() + "Z", duration_s="40",
                 text="Jestem na pozycji", source_ref="voice:7")
    assert res.status_code == 201, res.text
    body = res.json
    assert body["account"] == "alice" and body["callsign"] == "Alfa"
    assert body["uid"] == "ANDROID-alice"
    assert (body["lat"], body["lon"]) == (52.0, 21.0)
    assert body["duration_s"] == 40.0
    as_user(world, "admin")
    detail = client.get(f"/api/taklab/recordings/{body['id']}").json
    assert detail["text"] == "Jestem na pozycji"
    assert [p[:2] for p in detail["track"]] == [[52.001, 21.001]]


def test_ingest_jest_idempotentny_po_source_ref(client, world):
    first = ingest(client, author="Alfa (alice)", source_ref="voice:1")
    second = ingest(client, author="Alfa (alice)", source_ref="voice:1")
    assert first.status_code == 201 and second.status_code == 200
    assert first.json["id"] == second.json["id"]


# --- znacznik na mapie --------------------------------------------------------


def test_znacznik_cot_bez_tokenu_do_grup_autora(client, world):
    res = ingest(client, author="Alfa (alice)", started_at=(world["t0"] + timedelta(seconds=5)).isoformat())
    rec_id = res.json["id"]
    xml, author = world["sent"][-1]
    assert author == "alice"
    event = ET.fromstring(xml)
    assert event.get("type") == "b-i-x-i"
    assert event.get("uid") == f"taklab-recording-{rec_id}"
    stale = datetime.strptime(event.get("stale"), "%Y-%m-%dT%H:%M:%S.000Z")
    assert stale - now() > timedelta(days=360)
    point = event.find("point")
    assert float(point.get("lat")) == pytest.approx(52.0)
    detail = event.find("detail")
    assert detail.find("archive") is not None
    link = detail.find("link")
    assert link.attrib == {"uid": f"rec:{rec_id}", "relation": "r-u", "type": "taklab-recording"}
    marker = detail.find("__taklab_recording")
    assert marker.get("id") == rec_id and marker.get("kind") == "voice"
    remarks = detail.find("remarks").text
    assert "Głosówka" in remarks and "Alfa (alice)" in remarks
    for secret in (INGEST_TOKEN, OCR_TOKEN, "token", "jwt", "password"):
        assert secret not in xml


def test_bez_pozycji_nie_ma_znacznika(client, world):
    ingest(client, author="Echo (erin)")
    assert world["sent"] == []


def test_usuniecie_wysyla_forcedelete(client, world):
    rec_id = ingest(client, author="Alfa (alice)").json["id"]
    as_user(world, "admin")
    assert client.delete(f"/api/taklab/recordings/{rec_id}").status_code == 200
    xml, author = world["sent"][-1]
    event = ET.fromstring(xml)
    assert event.get("type") == "t-x-d-d" and author == "alice"
    link = event.find("detail/link")
    assert link.get("uid") == f"taklab-recording-{rec_id}" and link.get("type") == "b-i-x-i"
    assert event.find("detail/__forcedelete") is not None


# --- uprawnienia --------------------------------------------------------------


def _ids(client):
    res = client.get("/api/taklab/recordings")
    assert res.status_code == 200, res.text
    return {item["id"] for item in res.json["items"]}


def test_nagrania_tylko_dla_administratora(client, world):
    """Decyzja usera z 24 XI 2026: autor ani jego grupa nie widzą nagrań, tylko administrator."""
    rec_id = ingest(client, author="Alfa (alice)").json["id"]
    for name in ("alice", "bob", "carol", "erin"):
        as_user(world, name)
        for res in (
            client.get("/api/taklab/recordings"),
            client.get("/api/taklab/recordings/usage"),
            client.get(f"/api/taklab/recordings/{rec_id}"),
            client.get(f"/api/taklab/recordings/{rec_id}/file"),
            client.get(f"/api/taklab/recordings/archive?ids={rec_id}"),
            client.get("/api/taklab/recordings/archive?all=1"),
            client.patch(f"/api/taklab/recordings/{rec_id}", json={"name": "x"}),
            client.delete(f"/api/taklab/recordings/{rec_id}"),
        ):
            assert res.status_code == 403, (name, res.request.path, res.status_code)
            assert res.json["error"] == "Nagrania są dostępne tylko dla administratora."
    as_user(world, "admin")
    listing = client.get("/api/taklab/recordings").json
    assert {i["id"] for i in listing["items"]} == {rec_id} and listing["admin"] is True
    assert listing["items"][0]["can_manage"] is True


def test_admin_widzi_wszystkie_takze_skasowanego_konta(client, world):
    alice = ingest(client, author="Alfa (alice)").json["id"]
    carol = ingest(client, account="carol").json["id"]
    ghost = ingest(client, account="nieistnieje").json["id"]
    as_user(world, "admin")
    assert _ids(client) == {alice, carol, ghost}


def test_zmiana_nazwy_i_usuwanie_przez_administratora(client, world):
    rec_id = ingest(client, author="Alfa (alice)").json["id"]
    as_user(world, "admin")
    res = client.patch(f"/api/taklab/recordings/{rec_id}", json={"name": "  Meldunek   rano "})
    assert res.status_code == 200 and res.json["name"] == "Meldunek rano"
    assert client.patch(f"/api/taklab/recordings/{rec_id}", json={"name": "  "}).status_code == 400
    assert client.delete(f"/api/taklab/recordings/{rec_id}").status_code == 200
    assert client.get(f"/api/taklab/recordings/{rec_id}").status_code == 404


def test_bez_logowania_401_a_zajetosc_dla_admina(client, world):
    as_user(world, None)
    assert client.get("/api/taklab/recordings").status_code == 401
    assert client.get("/api/taklab/recordings/archive?all=1").status_code == 401
    ingest(client, author="Alfa (alice)")
    as_user(world, "admin")
    usage = client.get("/api/taklab/recordings/usage").json
    assert usage["used_bytes"] == len(OGG) and usage["limit_bytes"] == 10**9 and usage["count"] == 1


def test_sesja_panelu_ots_tez_dziala(client, world, app):
    from tests.conftest import AuthActions

    rec_id = ingest(client, author="Alfa (alice)").json["id"]
    as_user(world, None)
    auth = AuthActions(app, client, username="paneladmin", password="TestPass")
    res = client.get("/api/taklab/recordings", headers=auth.headers)
    assert res.status_code == 200 and rec_id in {i["id"] for i in res.json["items"]}
    # <video>/<audio>/<img> w panelu: sam plik sesyjny, bez nagłówka, z Range
    part = client.get(f"/api/taklab/recordings/{rec_id}/file", headers={"Range": "bytes=0-3"})
    assert part.status_code == 206 and part.data == b"OggS"


def test_filtry_listy(client, world):
    t0 = world["t0"]
    old = ingest(client, author="Alfa (alice)", started_at=(t0 - timedelta(days=2)).isoformat()).json["id"]
    new = ingest(client, kind="scan", data=JPEG, mime="image/jpeg", account="alice",
                 started_at=t0.isoformat()).json["id"]
    as_user(world, "admin")
    assert {i["id"] for i in client.get("/api/taklab/recordings?kind=scan").json["items"]} == {new}
    # Format z JS toISOString(), jak wysyła panel OTS
    since = (t0 - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    assert {i["id"] for i in client.get(f"/api/taklab/recordings?from={since}").json["items"]} == {new}
    assert client.get("/api/taklab/recordings?account=alice").json["total"] == 2
    assert client.get("/api/taklab/recordings?kind=film").status_code == 400
    page = client.get("/api/taklab/recordings?per_page=1&page=2").json
    assert page["total_pages"] == 2 and [i["id"] for i in page["items"]] == [old]
    assert page["results"] == page["items"] and page["current_page"] == 2 and page["per_page"] == 1


# --- plik i Range -------------------------------------------------------------


def test_plik_z_range(client, world):
    data = OGG + bytes(range(256)) * 4
    rec_id = ingest(client, data=data, author="Alfa (alice)").json["id"]
    as_user(world, "admin")
    full = client.get(f"/api/taklab/recordings/{rec_id}/file")
    assert full.status_code == 200 and full.data == data and full.mimetype == "audio/ogg"
    part = client.get(f"/api/taklab/recordings/{rec_id}/file", headers={"Range": "bytes=4-13"})
    assert part.status_code == 206
    assert part.data == data[4:14]
    assert part.headers["Content-Range"] == f"bytes 4-13/{len(data)}"
    assert part.headers["Accept-Ranges"] == "bytes"
    bad = client.get(f"/api/taklab/recordings/{rec_id}/file", headers={"Range": f"bytes={len(data) + 5}-"})
    assert bad.status_code == 416


def test_plik_usunietego_nagrania_znika_z_dysku(client, world, rec_env):
    rec_id = ingest(client, author="Alfa (alice)").json["id"]
    files = [p for p in rec_env.rglob("*.ogg")]
    assert len(files) == 1
    as_user(world, "admin")
    client.delete(f"/api/taklab/recordings/{rec_id}")
    assert not files[0].exists()


# --- archiwum ZIP -------------------------------------------------------------


def _zip(res):
    import zipfile

    assert res.status_code == 200, res.text
    assert res.mimetype == "application/zip"
    assert res.headers["Content-Disposition"].startswith('attachment; filename="nagrania-')
    return zipfile.ZipFile(io.BytesIO(res.data))


def test_archiwum_wybranych_z_nazwami_po_zmianie(client, world):
    t0 = world["t0"].replace(microsecond=0)
    one = ingest(client, author="Alfa (alice)", started_at=t0.isoformat(), data=OGG + b"1").json["id"]
    two = ingest(client, kind="scan", data=JPEG, mime="image/jpeg", account="carol",
                 started_at=t0.isoformat()).json["id"]
    ingest(client, account="dave", data=OGG + b"3")
    as_user(world, "admin")
    client.patch(f"/api/taklab/recordings/{one}", json={"name": 'Meldunek: "rano"/las'})
    client.patch(f"/api/taklab/recordings/{two}", json={"name": "Meldunek: \"rano\"/las"})
    archive = _zip(client.get(f"/api/taklab/recordings/archive?ids={one},{two}"))
    stamp = t0.strftime("%Y%m%d-%H%M%S")
    names = sorted(archive.namelist())
    assert names == sorted([f"Meldunek_ _rano_las_{stamp}.ogg", f"Meldunek_ _rano_las_{stamp}.jpg"])
    assert archive.read(f"Meldunek_ _rano_las_{stamp}.ogg") == OGG + b"1"
    assert archive.read(f"Meldunek_ _rano_las_{stamp}.jpg") == JPEG
    assert archive.testzip() is None


def test_archiwum_wszystkich_z_filtrem_i_jednakowe_nazwy(client, world):
    t0 = world["t0"].replace(microsecond=0)
    for data in (OGG + b"a", OGG + b"b"):
        ingest(client, account="alice", started_at=t0.isoformat(), data=data, name="Raport")
    ingest(client, kind="scan", data=JPEG, mime="image/jpeg", account="alice")
    as_user(world, "admin")
    archive = _zip(client.get("/api/taklab/recordings/archive?all=1&kind=voice"))
    stamp = t0.strftime("%Y%m%d-%H%M%S")
    assert sorted(archive.namelist()) == [f"Raport_{stamp} (2).ogg", f"Raport_{stamp}.ogg"]
    assert {archive.read(n) for n in archive.namelist()} == {OGG + b"a", OGG + b"b"}
    everything = _zip(client.get("/api/taklab/recordings/archive?all=1"))
    assert len(everything.namelist()) == 3


def test_archiwum_pomija_brakujace_pliki_i_usuniete(client, world, rec_env):
    keep = ingest(client, account="alice", data=OGG + b"k").json["id"]
    gone = ingest(client, account="bob", data=OGG + b"g").json["id"]
    deleted = ingest(client, account="carol").json["id"]
    as_user(world, "admin")
    client.delete(f"/api/taklab/recordings/{deleted}")
    for path in rec_env.rglob("*.ogg"):
        if path.read_bytes() == OGG + b"g":
            path.unlink()
    archive = _zip(client.get(f"/api/taklab/recordings/archive?ids={keep},{gone},{deleted}"))
    names = archive.namelist()
    assert len(names) == 2 and "pominiete.txt" in names
    assert archive.read("pominiete.txt").decode().count(".ogg") == 1
    assert client.get(f"/api/taklab/recordings/archive?ids={gone}").status_code == 404


def test_archiwum_odrzuca_zle_zadania(client, world):
    as_user(world, "admin")
    assert client.get("/api/taklab/recordings/archive").status_code == 400
    many = ",".join(f"id{n}" for n in range(101))
    assert client.get(f"/api/taklab/recordings/archive?ids={many}").status_code == 400
    assert client.get("/api/taklab/recordings/archive?all=1&kind=film").status_code == 400
    assert client.get("/api/taklab/recordings/archive?all=1").status_code == 404


# --- wideo z MediaMTX ---------------------------------------------------------


def _segment(root, path, start, size=1000):
    folder = root / "video" / ".segments" / path
    folder.mkdir(parents=True, exist_ok=True)
    name = start.strftime("%Y-%m-%d_%H-%M-%S") + f"-{start.microsecond:06d}.mp4"
    segment = folder / name
    segment.write_bytes(b"\x00" * size)
    return segment


def _hook(client, app, **args):
    token = app.config.get("OTS_MEDIAMTX_TOKEN")
    query = "&".join(f"{k}={v}" for k, v in args.items())
    return client.get(f"/api/mediamtx/webhook?token={token}&{query}")


def test_segmenty_jednej_publikacji_to_jedno_nagranie(client, world, app, rec_env, monkeypatch):
    core = importlib.import_module(CORE)
    t0 = world["t0"] + timedelta(seconds=10)
    first = _segment(rec_env, "alice", t0)
    second = _segment(rec_env, "alice", t0 + timedelta(seconds=620))  # 20 s przerwy
    assert _hook(client, app, event="segment_record_complete", path="alice",
                 segment_path=first, segment_duration="600").status_code == 200
    _hook(client, app, event="segment_record_complete", path="alice", segment_path=second,
          segment_duration="120")
    with app.app_context():
        from opentakserver.models.TaklabRecording import TaklabRecording

        rows = TaklabRecording.query.filter_by(kind="video").all()
        assert len(rows) == 1
        rec = rows[0]
        assert rec.account == "alice" and rec.state == "recording"
        assert rec.duration_s() == 740.0
        assert (rec.latitude, rec.longitude) == (52.0, 21.0)
        assert len(rec.segment_list()) == 2

    joined = []

    def fake_run(cmd, **kwargs):
        joined.append(cmd)
        out = cmd[-1]
        open(out, "wb").write(b"mp4-razem")
        return MagicMock(returncode=0, stderr=b"")

    monkeypatch.setattr(core.subprocess, "run", fake_run)
    with app.app_context():
        config = core.settings()
        later = core.utcnow() + timedelta(seconds=120)
        assert core.finalize_idle_videos(config, now=later) == 1
        rec = TaklabRecording.query.filter_by(kind="video").one()
        assert rec.state == "ready" and rec.size_bytes == len(b"mp4-razem")
        assert rec.segments is None and not first.exists() and not second.exists()
    assert joined and joined[0][0] == "ffmpeg" and "-c" in joined[0] and "copy" in joined[0]
    xml, author = world["sent"][-1]
    assert ET.fromstring(xml).get("type") == "b-i-v" and author == "alice"


def test_przerwa_dluzsza_niz_minuta_to_nowe_nagranie(client, world, app, rec_env, monkeypatch):
    core = importlib.import_module(CORE)
    monkeypatch.setattr(core, "finalize_video", lambda rec, config: setattr(rec, "state", "ready"))
    t0 = world["t0"]
    _hook(client, app, event="segment_record_complete", path="alice",
          segment_path=_segment(rec_env, "alice", t0), segment_duration="30")
    _hook(client, app, event="segment_record_complete", path="alice",
          segment_path=_segment(rec_env, "alice", t0 + timedelta(seconds=120)), segment_duration="30")
    with app.app_context():
        from opentakserver.models.TaklabRecording import TaklabRecording

        assert TaklabRecording.query.filter_by(kind="video").count() == 2


def test_sciezka_inna_niz_konto_nie_jest_nagrywana(client, world, app, rec_env):
    segment = _segment(rec_env, "kamera1", world["t0"])
    _hook(client, app, event="segment_record_complete", path="kamera1",
          segment_path=segment, segment_duration="30")
    assert not segment.exists()
    with app.app_context():
        from opentakserver.models.TaklabRecording import TaklabRecording

        assert TaklabRecording.query.count() == 0


def test_segment_spoza_katalogu_nagran_zostaje(client, world, app, tmp_path):
    outside = tmp_path / "inny.mp4"
    outside.write_bytes(b"x")
    _hook(client, app, event="segment_record_complete", path="alice",
          segment_path=outside, segment_duration="30")
    assert outside.exists()


def test_cudza_sciezka_konta_odrzucona_przy_publikacji(client, world, app):
    importlib.import_module("opentakserver.blueprints.ots_api.mediamtx_api")
    from flask_security import hash_password

    from opentakserver.extensions import db

    with app.app_context():
        user = app.security.datastore.find_user(username="bob")
        user.password = hash_password("haslo123")
        db.session.commit()
    body = {"user": "bob", "password": "haslo123", "action": "publish", "query": "", "ip": "10.0.0.9",
            "protocol": "rtsp", "id": None}
    assert client.post("/api/external_auth", json={**body, "path": "alice"}).status_code == 401
    assert client.post("/api/external_auth", json={**body, "path": "bob"}).status_code == 200
    assert client.post("/api/external_auth", json={**body, "path": "kamera7"}).status_code == 200


# --- retencja i miejsce -------------------------------------------------------


def test_retencja_kasuje_stare_i_zdejmuje_znacznik(client, world, app):
    core = importlib.import_module(CORE)
    from opentakserver.extensions import db
    from opentakserver.models.Point import Point

    long_ago = world["t0"] - timedelta(days=31)
    with app.app_context():
        db.session.add(Point(uid="ANDROID-alice", device_uid="ANDROID-alice", latitude=51.0,
                             longitude=17.0, timestamp=long_ago - timedelta(minutes=1)))
        db.session.commit()
    old = ingest(client, author="Alfa (alice)", started_at=long_ago.isoformat()).json["id"]
    fresh = ingest(client, author="Alfa (alice)").json["id"]
    with app.app_context():
        assert core.expire(core.settings()) == 1
    as_user(world, "admin")
    assert _ids(client) == {fresh}
    deletes = [x for x, _ in world["sent"] if 't-x-d-d' in x]
    assert any(f"taklab-recording-{old}" in x for x in deletes)


def test_przy_90_procentach_najstarsze_ida_pierwsze(client, world, app, monkeypatch):
    core = importlib.import_module(CORE)
    monkeypatch.setenv("OTS_TAKLAB_RECORDINGS_LIMIT_BYTES", str(len(OGG) * 10))
    t0 = world["t0"]
    ids = [ingest(client, author="Alfa (alice)", started_at=(t0 - timedelta(hours=10 - i)).isoformat()).json["id"]
           for i in range(9)]
    as_user(world, "admin")
    assert _ids(client) == set(ids)  # 9/10 = 90 %, jeszcze nie ponad
    newest = ingest(client, author="Alfa (alice)").json["id"]  # 100 % -> do 85 %
    left = _ids(client)
    assert ids[0] not in left and ids[1] not in left
    assert newest in left and len(left) == 8


def test_malo_wolnego_dysku_tez_kasuje(client, world, app, monkeypatch):
    core = importlib.import_module(CORE)
    first = ingest(client, author="Alfa (alice)",
                   started_at=(world["t0"] - timedelta(hours=1)).isoformat()).json["id"]
    monkeypatch.setenv("OTS_TAKLAB_RECORDINGS_MIN_FREE_BYTES", str(10**6))
    calls = {"n": 0}

    def fake_free(config):
        calls["n"] += 1
        return (0 if calls["n"] <= 2 else 10**7), 10**8

    monkeypatch.setattr(core, "disk_free", fake_free)
    with app.app_context():
        assert core.enforce_space(core.settings()) == 1
    as_user(world, "admin")
    assert first not in _ids(client)


# --- pomocnicze ---------------------------------------------------------------


def test_parse_author():
    core = importlib.import_module(CORE)
    assert core.parse_author("Kaszub (operator)") == ("Kaszub", "operator")
    assert core.parse_author("Kaszub-CloudTAK") == ("Kaszub-CloudTAK", None)
    assert core.parse_author("") == (None, None)


def test_decimate_trzyma_konce_i_limit():
    core = importlib.import_module(CORE)
    points = [(52.0 + i * 0.0001, 21.0, i) for i in range(2000)]
    kept = core.decimate(points, max_points=100)
    assert len(kept) == 100 and kept[0] == points[0] and kept[-1] == points[-1]
    still = [(52.0, 21.0, i) for i in range(50)]
    assert core.decimate(still) == [still[0], still[-1]]


def test_bez_katalogu_rejestr_wylaczony(client, app, monkeypatch):
    monkeypatch.delenv("OTS_TAKLAB_RECORDINGS_DIR", raising=False)
    assert client.get("/api/taklab/recordings").status_code == 404
