"""taklab: dziennik zdarzeń /api/taklab/journal.

Projekt: ~/taklab docs/superpowers/specs/2026-11-24-journal-design.md.
Sprawdza klasyfikację CoT, decymację pozycji, odcisk powtórek, oczyszczanie
adresów wideo, nasłuch rejestru nagrań, odporność zapisu na błędy, uprawnienia,
filtry, PDF z polskimi znakami i ZIP z plikami.
"""

import hashlib
import importlib
import io
import json
import zipfile
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from bs4 import BeautifulSoup

API = "opentakserver.blueprints.ots_api.taklab_journal_api"
CORE = "opentakserver.taklab_journal"
OGG = b"OggS" + b"\x00" * 200


def now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def ev(xml: str):
    return BeautifulSoup(xml, "xml").find("event")


def cot(uid, cot_type, detail="", lat="52.1", lon="21.0"):
    return ev(
        f'<event version="2.0" uid="{uid}" type="{cot_type}" how="h-g-i-g-o" '
        f'time="2026-11-24T10:00:00Z" start="2026-11-24T10:00:00Z" stale="2026-11-24T11:00:00Z">'
        f'<point lat="{lat}" lon="{lon}" hae="0" ce="9" le="9"/><detail>{detail}</detail></event>'
    )


@pytest.fixture
def core():
    module = importlib.import_module(CORE)
    module.reset_state()
    yield module
    module.reset_state()


@pytest.fixture
def clock(core, monkeypatch):
    state = {"now": now()}
    monkeypatch.setattr(core, "_now", lambda: state["now"])
    return state


@pytest.fixture
def as_cert(monkeypatch):
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


def make_eud(app, username, uid, callsign):
    from opentakserver.extensions import db
    from opentakserver.models.EUD import EUD

    with app.app_context():
        user = app.security.datastore.find_user(username=username)
        db.session.add(EUD(uid=uid, callsign=callsign, user_id=user.id))
        db.session.commit()


def rows(app, kind=None):
    from opentakserver.extensions import db
    from opentakserver.models.TaklabJournal import TaklabJournal

    with app.app_context():
        query = db.select(TaklabJournal).order_by(TaklabJournal.id)
        if kind:
            query = query.where(TaklabJournal.kind == kind)
        return [r.to_json() for r in db.session.execute(query).scalars().all()]


def record(app, core, event, sender="ANDROID-alice", user_id=None):
    with app.app_context():
        core.record_cot(event, sender, user_id)


def add_entry(app, **values):
    from opentakserver.extensions import db
    from opentakserver.models.TaklabJournal import TaklabJournal

    values.setdefault("at", now())
    values.setdefault("title", values.get("kind", ""))
    for key in ("recipients", "file"):
        if key in values and not isinstance(values[key], str):
            values[key] = json.dumps(values[key])
    with app.app_context():
        entry = TaklabJournal(**values)
        db.session.add(entry)
        db.session.commit()
        return entry.id


# --- klasyfikacja ------------------------------------------------------------------


def test_klasyfikacja_obiektow_i_wlasnej_pozycji(core):
    marker = core.classify(cot("m1", "a-h-G", '<contact callsign="Wróg"/>'), "ANDROID-alice")
    assert marker["kind"] == "object" and marker["object_name"] == "Wróg"
    for cot_type in ("u-d-r", "u-d-f", "b-m-p-s-p-i", "b-m-r", "u-rb-a"):
        assert core.classify(cot("x", cot_type), "ANDROID-alice")["kind"] == "object", cot_type
    own = core.classify(cot("ANDROID-alice", "a-f-G-U-C", '<contact callsign="Alfa"/>'), "ANDROID-alice")
    assert own["kind"] == "position" and own["device_callsign"] == "Alfa"
    # urządzenie partnera federacji (takv) to też ruch, nie obiekt
    fed = core.classify(cot("ANDROID-obcy", "a-f-G-U-C", '<takv platform="ATAK"/>'), "fed-sidecar")
    assert fed["kind"] == "position" and fed["device_uid"] == "ANDROID-obcy"


def test_klasyfikacja_usuniecia_i_rozlaczenia(core):
    delete = core.classify(cot("d1", "t-x-d-d", '<link uid="m1" relation="none" type="a-h-G"/>'), "ANDROID-alice")
    assert delete == {"kind": "object_delete", "object_uid": "m1", "object_type": "a-h-G", "mission": None}
    gone = core.classify(cot("d2", "t-x-d-d", '<link uid="ANDROID-alice" relation="p-p"/>'), "ANDROID-alice")
    assert gone["kind"] == "disconnect"


def test_szum_jest_pomijany(core):
    for cot_type in ("t-x-takp-q", "t-x-takp-v", "t-x-c-t", "t-x-c-t-r", "t-x-m-c", "b-t-f-d", "b-t-f-r", "y-taklab-map"):
        assert core.classify(cot("n", cot_type), "ANDROID-alice") is None, cot_type
    marker = cot("taklab-recording-1", "b-i-v", '<__taklab_recording id="1" kind="video"/>')
    assert core.classify(marker, None) is None
    assert core.classify(cot("x", "t-x-d-d", '<link uid="taklab-recording-1"/>'), None) is None


CHAT_DM = (
    '<__chat parent="RootContactGroup" groupOwner="false" chatroom="BRAVO-2" id="ANDROID-bob" '
    'senderCallsign="ALFA-1"><chatgrp uid0="ANDROID-alice" uid1="ANDROID-bob" id="ANDROID-bob"/></__chat>'
    '<remarks source="BAO.F.ATAK.ANDROID-alice" to="ANDROID-bob" time="2026-11-24T10:00:00Z">tajne &lt;b&gt;</remarks>'
    '<marti><dest callsign="BRAVO-2"/></marti>'
)
CHAT_ALL = (
    '<__chat parent="RootContactGroup" chatroom="All Chat Rooms" id="All Chat Rooms" senderCallsign="ALFA-1">'
    '<chatgrp uid0="ANDROID-alice" uid1="All Chat Rooms" id="All Chat Rooms"/></__chat>'
    '<remarks time="2026-11-24T10:00:00Z">do wszystkich</remarks>'
)


def test_czat_prywatny_i_ogolny(core):
    dm = core.classify(cot("GeoChat.ANDROID-alice.ANDROID-bob.1", "b-t-f", CHAT_DM), "ANDROID-alice")
    assert dm["kind"] == "chat" and dm["private"] is True
    assert dm["text"] == "tajne <b>"
    assert dm["chatroom"] == "BRAVO-2"
    assert {"callsign": "BRAVO-2", "uid": None} in dm["recipients_raw"]
    assert {"callsign": None, "uid": "ANDROID-bob"} in dm["recipients_raw"]
    room = core.classify(cot("GeoChat.ANDROID-alice.All Chat Rooms.2", "b-t-f", CHAT_ALL), "ANDROID-alice")
    assert room["private"] is False and room["recipients_raw"] == []


def test_wideo_plik_alarm_casevac(core):
    video = core.classify(
        cot("kam1", "b-i-v", '<__video><ConnectionEntry protocol="rtsp" address="jan:haslo@kam.example" '
            'port="8554" path="/k1?token=TAJNY" alias="Kamera 1" uid="kam1"/></__video>'),
        "ANDROID-alice",
    )
    assert video["kind"] == "video_share" and video["title_name"] == "Kamera 1"
    assert video["text"] == "rtsp://kam.example:8554/k1"
    via_url = core.classify(cot("m5", "a-f-G", '<__video url="https://u:p@cam/x.m3u8?jwt=abc"/>'), "ANDROID-alice")
    assert via_url["kind"] == "video_share" and via_url["text"] == "https://cam/x.m3u8"
    send = core.classify(
        cot("ft1", "b-f-t-r", f'<fileshare filename="mapa.zip" sha256="{"a" * 64}" sizeInBytes="1234" '
            'senderUrl="https://h:8443/Marti/sync/content?hash=x" senderCallsign="ALFA-1" name="mapa"/>'
            '<marti><dest callsign="BRAVO-2"/></marti>'),
        "ANDROID-alice",
    )
    assert send["kind"] == "file_send" and send["file"]["ref"] == "a" * 64 and send["file"]["size"] == 1234
    alert = core.classify(cot("ANDROID-alice-9-1-1", "b-a-o-tbl", '<emergency type="911 Alert">ALFA-1</emergency>'), "ANDROID-alice")
    assert alert["kind"] == "alert" and alert["title_name"] == "911"
    casevac = core.classify(cot("cv1", "b-r-f-h-c", '<contact callsign="MEDEVAC 1"/><remarks>2 rannych</remarks>'), "ANDROID-alice")
    assert casevac["kind"] == "casevac" and casevac["text"] == "2 rannych"


@pytest.mark.parametrize(
    "raw, clean",
    [
        ("rtsp://user:pass@host:8554/live?token=abc", "rtsp://host:8554/live"),
        ("srt://h:9000?streamid=publish:x&passphrase=tajne", "srt://h:9000"),
        ("https://h/hls/a/index.m3u8?jwt=eyJ#frag", "https://h/hls/a/index.m3u8"),
        ("login:haslo@10.0.0.1:554/x?y=1", "10.0.0.1:554/x"),
        (None, None),
    ],
)
def test_adres_wideo_bez_hasla_i_tokenu(core, raw, clean):
    assert core.clean_url(raw) == clean


# --- stan: odcisk, decymacja, odporność -------------------------------------------------


def test_obiekt_dodanie_powtorka_zmiana_usuniecie(app, core, clock):
    uid = make_user(app, "alice")
    make_eud(app, "alice", "ANDROID-alice", "ALFA-1")

    def marker(lat):
        return cot("m1", "a-h-G", '<contact callsign="Wróg"/><remarks>czołg</remarks>', lat=lat)

    record(app, core, marker("52.10000"), user_id=uid)
    record(app, core, marker("52.10002"), user_id=uid)  # ~2 m: odświeżenie ATAK
    record(app, core, marker("52.10200"), user_id=uid)  # ~220 m
    core.reset_state()  # restart procesu: odcisk z bazy
    record(app, core, marker("52.10200"), user_id=uid)
    delete = cot("d1", "t-x-d-d", '<link uid="m1" relation="none" type="a-h-G"/><__forcedelete/>')
    record(app, core, delete, user_id=uid)
    record(app, core, delete, user_id=uid)
    kinds = [(r["kind"], r["title"]) for r in rows(app)]
    assert kinds == [
        ("object_add", "Dodanie obiektu Wróg"),
        ("object_change", "Zmiana obiektu Wróg"),
        ("object_delete", "Usunięcie obiektu Wróg"),
    ]
    first = rows(app)[0]
    assert first["account"] == "alice" and first["callsign"] == "ALFA-1"
    assert first["device_uid"] == "ANDROID-alice" and first["text"] == "czołg"
    # ten sam uid po usunięciu to znów dodanie
    record(app, core, marker("52.1"), user_id=uid)
    assert rows(app)[-1]["kind"] == "object_add"


def test_decymacja_pozycji_50_m_i_5_min(app, core, clock):
    uid = make_user(app, "alice")

    def sa(lat):
        return cot("ANDROID-alice", "a-f-G-U-C", '<contact callsign="ALFA-1"/><takv/>', lat=lat, lon="21.0")

    start = clock["now"]
    record(app, core, sa("52.00000"), user_id=uid)
    clock["now"] = start + timedelta(seconds=30)
    record(app, core, sa("52.00020"), user_id=uid)  # 22 m
    clock["now"] = start + timedelta(seconds=60)
    record(app, core, sa("52.00060"), user_id=uid)  # 67 m od pierwszego wpisu
    clock["now"] = start + timedelta(seconds=120)
    record(app, core, sa("52.00070"), user_id=uid)  # 11 m
    clock["now"] = start + timedelta(seconds=60 + 301)
    record(app, core, sa("52.00070"), user_id=uid)  # bez ruchu, ale 5 min
    positions = rows(app, "position")
    assert [p["lat"] for p in positions] == [52.0, 52.0006, 52.0007]
    assert positions[0]["callsign"] == "ALFA-1" and positions[0]["title"] == "Pozycja ALFA-1"
    core.reset_state()
    clock["now"] = start + timedelta(seconds=400)
    record(app, core, sa("52.00071"), user_id=uid)  # po restarcie: ostatni wpis z bazy
    assert len(rows(app, "position")) == 3


def test_czat_w_dzienniku_z_adresatami(app, core, clock):
    uid = make_user(app, "alice")
    make_user(app, "bob")
    make_eud(app, "alice", "ANDROID-alice", "ALFA-1")
    make_eud(app, "bob", "ANDROID-bob", "BRAVO-2")
    event = cot("GeoChat.ANDROID-alice.ANDROID-bob.1", "b-t-f", CHAT_DM)
    record(app, core, event, user_id=uid)
    record(app, core, event, user_id=uid)  # ta sama wiadomość drugi raz
    chats = rows(app, "chat")
    assert len(chats) == 1
    chat = chats[0]
    assert chat["private"] is True and chat["text"] == "tajne <b>"
    assert chat["title"] == "Wiadomość prywatna do BRAVO-2"
    assert chat["recipients"] == [{"callsign": "BRAVO-2", "uid": "ANDROID-bob", "account": "bob"}]
    assert chat["account"] == "alice" and chat["callsign"] == "ALFA-1"


def test_wideo_w_bazie_bez_hasla(app, core, clock):
    event = cot("kam1", "b-i-v", '<__video><ConnectionEntry protocol="rtsp" address="jan:haslo@kam" '
                'port="8554" path="k1?token=TAJNY" alias="K1" uid="kam1"/></__video>')
    record(app, core, event, sender=None)
    record(app, core, event, sender=None)
    videos = rows(app, "video_share")
    assert len(videos) == 1
    dumped = json.dumps(videos)
    assert "haslo" not in dumped and "TAJNY" not in dumped
    assert videos[0]["text"] == "rtsp://kam:8554/k1" and videos[0]["account"] == "serwer"


def test_rozlaczenie_z_obu_drog_raz(app, core, clock):
    uid = make_user(app, "alice")
    make_eud(app, "alice", "ANDROID-alice", "ALFA-1")
    record(app, core, cot("x", "t-x-d-d", '<link uid="ANDROID-alice" relation="p-p"/>'), user_id=uid)
    with app.app_context():
        core.record_disconnect("ANDROID-alice", uid)
    gone = rows(app, "disconnect")
    assert len(gone) == 1 and gone[0]["title"] == "Rozłączenie ALFA-1"


def test_blad_dziennika_nie_przerywa_cot(app, core, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("baza padła")

    monkeypatch.setattr(core, "_Short", broken)
    with app.app_context():
        core.record_cot(cot("m1", "a-h-G"), "ANDROID-alice", 2)
        core.record_disconnect("ANDROID-alice", 2)
        core.record_file_upload("data_package", "a" * 64, "x.zip", 1, "application/zip")
        core.record_export("admin", "pdf", {}, 0)
    monkeypatch.setattr(core, "classify", broken)
    with app.app_context():
        core.record_cot(cot("m1", "a-h-G"), "ANDROID-alice", 2)
    # bez kontekstu aplikacji (brak db.engine) też bez wyjątku
    core.record_cot(cot("m2", "a-h-G"), "ANDROID-alice", 2)
    core.record_cot(None, None, None)


def test_cot_parser_wola_dziennik_i_nie_pada(app, monkeypatch):
    """Zaczep w on_message: wpis po route_cot, wyjątek dziennika nie psuje potwierdzenia."""
    import opentakserver.cot_parser.cot_parser as parser_module

    calls = []
    monkeypatch.setattr(parser_module.taklab_journal, "record_cot", lambda *a: calls.append(("cot", a[1])))
    monkeypatch.setattr(parser_module.taklab_journal, "record_disconnect", lambda *a: calls.append(("gone", a[0])))
    controller = parser_module.CoTController(MagicMock(), MagicMock(), MagicMock(), MagicMock())
    controller.rabbit_channel = MagicMock()
    for name in ("insert_cot", "parse_point", "parse_geochat", "parse_video", "parse_alert", "parse_casevac",
                 "parse_marker", "parse_rbline", "parse_stats", "generate_mission_change",
                 "handle_shared_mission", "route_cot", "send_disconnect_cot", "strip_origin"):
        monkeypatch.setattr(controller, name, MagicMock())
    controller.context.app.config = {"OTS_NODE_ID": "node"}
    deliver = MagicMock(delivery_tag=1)
    body = json.dumps({"uid": "ANDROID-alice", "cot": str(cot("m1", "a-h-G")), "user_id": 2})
    controller.on_message(None, deliver, None, body.encode())
    controller.on_message(None, deliver, None, json.dumps({"uid": "ANDROID-alice", "disconnected": True}).encode())
    assert calls == [("cot", "ANDROID-alice"), ("gone", "ANDROID-alice")]
    assert controller.rabbit_channel.basic_ack.call_count == 2


# --- nasłuch rejestru nagrań --------------------------------------------------------------


def test_nagranie_i_jego_usuniecie_w_dzienniku(app, core):
    from opentakserver.extensions import db
    from opentakserver.models.TaklabRecording import TaklabRecording

    with app.app_context():
        rec = TaklabRecording(
            id="r1", kind="voice", account="alice", callsign="ALFA-1", device_uid="ANDROID-alice",
            name="Meldunek", started_at=now(), created_at=now(), latitude=52.0, longitude=21.0,
            file_path="voice/r1.ogg", size_bytes=204, mime="audio/ogg",
        )
        db.session.add(rec)
        db.session.commit()
        rec.name = "Meldunek 2"  # zwykła zmiana: bez wpisu
        db.session.commit()
        rec.deleted = True
        db.session.commit()
        other = TaklabRecording(id="r2", kind="scan", name="", started_at=now(), created_at=now())
        db.session.add(other)
        db.session.rollback()  # wycofane: bez wpisu
    entries = rows(app)
    assert [e["kind"] for e in entries] == ["recording_voice", "recording_delete"]
    voice = entries[0]
    assert voice["account"] == "alice" and voice["title"] == "Głosówka: Meldunek"
    assert voice["file"]["source"] == "recording" and voice["file"]["ref"] == "r1"
    assert entries[1]["object_uid"] == "r1"


# --- przyjęcie plików -----------------------------------------------------------------------


def _package(marker: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("MANIFEST/manifest.xml", "<MissionPackageManifest version='2'/>")
        archive.writestr(f"{marker}/{marker}.cot", f"<event uid='{marker}'/>")
    return buffer.getvalue()


def test_paczka_danych_i_tresc_misji_trafiaja_do_dziennika(app, client, core, monkeypatch, tmp_path):
    app.config["UPLOAD_FOLDER"] = str(tmp_path / "uploads")
    (tmp_path / "uploads").mkdir()
    app.config["OTS_DATA_FOLDER"] = str(tmp_path)
    data = _package("p1")
    sha = hashlib.sha256(data).hexdigest()
    res = client.post(
        f"/Marti/sync/missionupload?hash={sha}&filename=mapa.zip&creatorUid=ANDROID-1",
        data={"assetfile": (io.BytesIO(data), "mapa.zip", "application/x-zip-compressed")},
        content_type="multipart/form-data",
    )
    assert res.status_code == 200, res.text
    mission_api = importlib.import_module("opentakserver.blueprints.marti_api.mission_marti_api")
    cert = MagicMock()
    cert.subject.get_attributes_for_oid.return_value = [MagicMock(value="alice")]
    monkeypatch.setattr(mission_api, "verify_client_cert", lambda: cert)
    content = b"\x89PNG\r\n\x1a\n" + b"0" * 50
    res = client.post("/Marti/sync/upload?name=foto.png&creatorUid=ANDROID-alice", data=content,
                      content_type="image/png")
    assert res.status_code == 200, res.text
    uploads = rows(app, "file_upload")
    assert [u["file"]["source"] for u in uploads] == ["data_package", "mission_content"]
    assert uploads[0]["file"]["ref"] == sha and uploads[0]["file"]["filename"] == "mapa.zip"
    assert uploads[1]["account"] == "alice"
    assert uploads[1]["file"] == {
        "source": "mission_content", "ref": hashlib.sha256(content).hexdigest(), "filename": "foto.png",
        "size": len(content), "mime": "image/png", "available": False,
    }


# --- API: uprawnienia -------------------------------------------------------------------------

ROUTES = [
    "/api/taklab/journal",
    "/api/taklab/journal/accounts",
    "/api/taklab/journal/export/estimate",
    "/api/taklab/journal/export.pdf",
    "/api/taklab/journal/export.zip",
]


def test_bez_konta_401_zwykle_konto_403(app, client, as_cert):
    make_user(app, "alice")
    for route in ROUTES + ["/api/taklab/journal/access"]:
        res = client.get(route)
        assert res.status_code == 401, route
        assert res.headers["Cache-Control"] == "no-store"
    as_cert["username"] = "alice"
    for route in ROUTES:
        res = client.get(route)
        assert res.status_code == 403, route
        assert res.json["error"] == "Dziennik zdarzeń widzi tylko administrator."
    assert client.get("/api/taklab/journal/access").json == {"admin": False}
    as_cert["username"] = "nieistniejacy"
    assert client.get("/api/taklab/journal/access").status_code == 401


def test_administrator_z_certyfikatu_i_tokenu(app, client, as_cert, auth):
    make_user(app, "admin", admin=True)
    as_cert["username"] = "admin"
    assert client.get("/api/taklab/journal/access").json == {"admin": True}
    assert client.get("/api/taklab/journal").status_code == 200
    as_cert["username"] = None
    # TestUser z conftest ma rolę administrator, logowanie tokenem (panel OTS)
    assert auth.get("/api/taklab/journal").status_code == 200


def test_brak_tras_zmieniajacych_wpisy(app, client, as_cert):
    make_user(app, "admin", admin=True)
    as_cert["username"] = "admin"
    entry = add_entry(app, kind="chat", title="x")
    for method in ("delete", "patch", "put", "post"):
        assert getattr(client, method)(f"/api/taklab/journal/{entry}").status_code in (404, 405)
        assert getattr(client, method)("/api/taklab/journal").status_code == 405


# --- API: filtry i stronicowanie ---------------------------------------------------------------


@pytest.fixture
def admin(app, as_cert):
    make_user(app, "admin", admin=True)
    as_cert["username"] = "admin"


def test_filtry_i_stronicowanie(app, client, admin):
    base = datetime(2026, 11, 24, 10, 0, 0)
    add_entry(app, at=base, kind="chat", account="alice", callsign="ALFA-1", title="Wiadomość w pokoju A", text="zbiórka o 12")
    add_entry(app, at=base + timedelta(minutes=1), kind="position", account="alice", callsign="ALFA-1", title="Pozycja ALFA-1")
    add_entry(app, at=base + timedelta(minutes=2), kind="object_add", account="bob", callsign="BRAVO-2", title="Dodanie obiektu Most")
    add_entry(app, at=base + timedelta(minutes=3), kind="video_share", account=None, title="Udostępnienie wideo K1")
    add_entry(app, at=base + timedelta(minutes=4), kind="alert", account="bob", title="Alarm: 911 50%_x")

    def get(**params):
        res = client.get("/api/taklab/journal", query_string=params)
        assert res.status_code == 200, res.text
        assert res.headers["Cache-Control"] == "no-store"
        return res.json

    body = get()
    assert body["total"] == 4 and body["page"] == 1 and body["per_page"] == 50
    assert [i["kind"] for i in body["items"]] == ["chat", "object_add", "video_share", "alert"]
    assert get(positions=1)["total"] == 5
    assert get(kind="position")["total"] == 1
    assert [i["kind"] for i in get(kind="chat,alert")["items"]] == ["chat", "alert"]
    assert get(account="bob")["total"] == 2
    assert [i["title"] for i in get(account="serwer")["items"]] == ["Udostępnienie wideo K1"]
    assert get(account="serwer,alice")["total"] == 2
    assert get(since="2026-11-24T10:02:00Z", until="2026-11-24T10:03:00Z")["total"] == 2
    assert get(q="zbiórka")["total"] == 1 and get(q="bravo")["total"] == 1
    assert get(q="%")["total"] == 1  # % jest tekstem, nie wzorcem
    assert get(q="_x")["total"] == 1
    page = get(per_page=1, page=2)
    assert page["total"] == 4 and [i["kind"] for i in page["items"]] == ["object_add"]
    assert get(per_page=1000)["per_page"] == 200
    item = get(kind="chat")["items"][0]
    assert set(item) == {
        "id", "at", "kind", "account", "callsign", "device_uid", "object_uid", "object_type", "title",
        "text", "private", "recipients", "chatroom", "lat", "lon", "mission", "file",
    }
    assert item["at"] == "2026-11-24T10:00:00.000Z"
    assert client.get("/api/taklab/journal?kind=cokolwiek").status_code == 400
    assert client.get("/api/taklab/journal?since=wczoraj").status_code == 400
    assert client.get("/api/taklab/journal?page=x").status_code == 400


def test_lista_kont_ze_znakami(app, client, admin):
    add_entry(app, kind="chat", account="alice", callsign="ALFA-1")
    add_entry(app, kind="chat", account="alice", callsign="ALFA-2")
    add_entry(app, kind="position", account="alice", callsign="ALFA-1")
    add_entry(app, kind="video_share", account=None)
    body = client.get("/api/taklab/journal/accounts").json
    assert body == {"accounts": [
        {"account": "alice", "callsigns": ["ALFA-1", "ALFA-2"]},
        {"account": "serwer", "callsigns": []},
    ]}


# --- PDF ------------------------------------------------------------------------------------


def pdf_text(data: bytes) -> str:
    from pypdf import PdfReader

    return "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(data)).pages)


def test_pdf_z_polskimi_znakami_i_skryptem(app, client, admin):
    base = datetime(2026, 11, 24, 10, 0, 0)
    add_entry(app, at=base, kind="chat", account="alice", callsign="ŻÓŁW-1", title="Wiadomość w pokoju Zażółć",
              text="Zażółć gęślą jaźń <script>alert(1)</script>")
    add_entry(app, at=base, kind="position", account="alice", callsign="ŻÓŁW-1", device_uid="A1", latitude=52.0, longitude=21.0)
    add_entry(app, at=base + timedelta(minutes=30), kind="position", account="alice", callsign="ŻÓŁW-1", device_uid="A1",
              latitude=52.01, longitude=21.0)
    res = client.get("/api/taklab/journal/export.pdf")
    assert res.status_code == 200, res.text
    assert res.mimetype == "application/pdf"
    assert res.headers["Content-Disposition"].startswith('attachment; filename="dziennik-')
    assert res.data.startswith(b"%PDF")
    text = pdf_text(res.data)
    assert "Zażółć gęślą jaźń" in text
    assert "<script>alert(1)</script>" in text
    assert "Wygenerował: admin" in text
    assert "Uczestnicy" in text and "ŻÓŁW-1 (alice)" in text
    assert "1,11 km" in text and "30 min" in text
    exports = rows(app, "journal_export")
    assert len(exports) == 1 and exports[0]["account"] == "admin"
    assert json.loads(exports[0]["text"])["format"] == "pdf"


def test_pdf_za_duzo_wpisow_413(app, client, admin, monkeypatch):
    monkeypatch.setenv("OTS_TAKLAB_JOURNAL_PDF_LIMIT", "2")
    for _ in range(3):
        add_entry(app, kind="chat", account="alice")
    res = client.get("/api/taklab/journal/export.pdf")
    assert res.status_code == 413
    assert "Zawęź" in res.json["error"]
    assert client.get("/api/taklab/journal/export.pdf?account=bob").status_code == 200


def test_pdf_bez_czcionki_dejavu_tez_powstaje(app, client, admin, monkeypatch):
    export = importlib.import_module("opentakserver.taklab_journal_export")
    monkeypatch.setattr(export, "FONT_DIR", "/nie/ma")
    monkeypatch.setenv("OTS_TAKLAB_JOURNAL_FONT_DIR", "/tez/nie")
    add_entry(app, kind="chat", account="alice", text="gęś")
    res = client.get("/api/taklab/journal/export.pdf")
    assert res.status_code == 200 and "ges" in pdf_text(res.data)


# --- ZIP ------------------------------------------------------------------------------------


@pytest.fixture
def files(app, tmp_path, monkeypatch):
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    app.config["UPLOAD_FOLDER"] = str(uploads)
    app.config["OTS_DATA_FOLDER"] = str(tmp_path)
    root = tmp_path / "recordings"
    (root / "voice").mkdir(parents=True)
    monkeypatch.setenv("OTS_TAKLAB_RECORDINGS_DIR", str(root))
    return {"uploads": uploads, "root": root, "missions": tmp_path / "missions"}


def test_zip_zawartosc_brakujace_i_bezpieczne_nazwy(app, client, admin, files):
    from opentakserver.extensions import db
    from opentakserver.models.TaklabRecording import TaklabRecording

    base = datetime(2026, 11, 24, 10, 0, 0)
    package = _package("z")
    sha = hashlib.sha256(package).hexdigest()
    (files["uploads"] / f"{sha}.zip").write_bytes(package)
    (files["root"] / "voice" / "r1.ogg").write_bytes(OGG)
    files["missions"].mkdir()
    (files["missions"] / "foto.png").write_bytes(b"\x89PNG" + b"1" * 10)
    with app.app_context():
        db.session.add(TaklabRecording(id="r1", kind="voice", account="alice", name="Meldunek",
                                       started_at=base, created_at=base, file_path="voice/r1.ogg",
                                       size_bytes=len(OGG), mime="audio/ogg"))
        db.session.add(TaklabRecording(id="r2", kind="scan", account="alice", name="Rozkaz",
                                       started_at=base, created_at=base, file_path="scan/r2.jpg",
                                       mime="image/jpeg", text="treść"))
        db.session.commit()
    add_entry(app, at=base, kind="file_upload", account="alice",
              file={"source": "data_package", "ref": sha, "filename": "../../etc/passwd\x07.zip", "size": len(package)})
    add_entry(app, at=base, kind="file_send", account="alice",
              file={"source": "data_package", "ref": sha, "filename": "kopia.zip"})  # ten sam plik
    add_entry(app, at=base, kind="file_upload", account="a/../b",
              file={"source": "mission_content", "ref": "x", "filename": "foto.png", "size": 14})
    add_entry(app, at=base, kind="file_upload", account="bob",
              file={"source": "data_package", "ref": "b" * 64, "filename": "zgubiona.zip"})
    add_entry(app, at=base, kind="file_upload", account="bob",
              file={"source": "mission_content", "ref": "y", "filename": "foto.png", "size": 999})  # nadpisany
    for minute, lat in ((0, 52.0), (10, 52.001)):
        add_entry(app, at=base + timedelta(minutes=minute), kind="position", account="alice",
                  callsign="ALFA/1", device_uid="A1", latitude=lat, longitude=21.0)

    estimate = client.get("/api/taklab/journal/export/estimate").json
    # wpisy: 5 + 2 nagrania z nasłuchu; pliki: paczka, foto, głosówka; brak: 3
    assert estimate == {"entries": 7, "files": 3, "bytes": len(package) + 14 + len(OGG), "missing": 3}

    res = client.get("/api/taklab/journal/export.zip")
    assert res.status_code == 200
    assert res.mimetype == "application/zip"
    assert res.headers["Content-Disposition"].startswith('attachment; filename="dziennik-')
    archive = zipfile.ZipFile(io.BytesIO(res.data))
    names = archive.namelist()
    assert {"dziennik.pdf", "dziennik.csv", "dziennik.json", "BRAKUJACE.txt"} <= set(names)
    for name in names:
        assert ".." not in name and "\\" not in name and not name.startswith("/")
        assert all(ord(ch) >= 32 for ch in name)
        assert name.count("/") <= 1
    stored = [n for n in names if n.startswith("pliki/")]
    assert len(stored) == 3
    package_name = next(n for n in stored if n.endswith(".zip"))
    assert package_name.startswith("pliki/20261124-110000_alice_file_upload_")
    assert archive.read(package_name) == package
    assert archive.getinfo(package_name).compress_type == zipfile.ZIP_STORED
    voice = next(n for n in stored if n.endswith(".ogg"))
    assert archive.read(voice) == OGG and archive.getinfo(voice).compress_type == zipfile.ZIP_STORED
    photo = next(n for n in stored if n.endswith(".png"))
    assert "_a_b_" in photo or "a_.._b" not in photo
    missing = archive.read("BRAKUJACE.txt").decode("utf-8")
    assert "zgubiona.zip" in missing and "paczka usunięta z serwera" in missing
    assert "plik nadpisany innym o tej samej nazwie" in missing
    assert "brak pliku nagrania na serwerze" in missing
    gpx = [n for n in names if n.startswith("trasy/")]
    assert gpx == ["trasy/ALFA_1_alice.gpx"]
    assert archive.read(gpx[0]).count(b"<trkpt") == 2
    journal = json.loads(archive.read("dziennik.json"))
    assert len(journal) == 7  # bez pozycji (positions=0), z dwoma wpisami nagrań
    lost = next(e for e in journal if e["file"]["filename"] == "zgubiona.zip")
    assert lost["file"]["available"] is False
    csv_text = archive.read("dziennik.csv").decode("utf-8-sig")
    assert csv_text.splitlines()[0].startswith("id;czas;rodzaj")
    assert archive.read("dziennik.pdf").startswith(b"%PDF")
    exports = rows(app, "journal_export")
    assert [json.loads(e["text"])["format"] for e in exports] == ["zip"]


def test_bezpieczne_nazwy_skladnikow():
    export = importlib.import_module("opentakserver.taklab_journal_export")
    assert export.safe_component("../../etc/passwd") == "etc_passwd"
    assert export.safe_component("a\x00b\nc") == "abc"
    assert export.safe_component("..") == "plik"
    assert export.safe_component("Zażółć gęślą") == "Zażółć_gęślą"
    assert "/" not in export.safe_component("x/y\\z")
    assert len(export.safe_component("a" * 500)) == 80


def test_csv_nie_zaczyna_formuly():
    export = importlib.import_module("opentakserver.taklab_journal_export")
    assert export._csv_cell("=HYPERLINK(\"x\")") == "'=HYPERLINK(\"x\")"
    assert export._csv_cell("zwykły") == "zwykły"
