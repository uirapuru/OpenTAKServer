"""Zapis CloudTAK do misji: paczka PUT /Marti/api/missions/<n>/contents/missionpackage
i usuwanie DELETE /Marti/api/missions/guid/<guid>/contents?uid=.

CloudTAK wysyła paczkę danych (zip z MANIFEST i plikami CoT) i potem sprawdza,
czy uid z paczki pojawiły się w misji. Wcześniej OTS odpowiadał 200 i nic nie
robił, więc CloudTAK po kilku sekundach kończył błędem 502.
"""

import io
import json
import zipfile
from unittest.mock import MagicMock

import jwt
import pika
import pytest
from bs4 import BeautifulSoup
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

MISSION = "Mapa test"
GUID = "c2a3d0f1-7b1e-4b0a-9c1d-1f2e3a4b5c6d"
CLIENT_UID = "ANDROID-CloudTAK-user"
PACKAGE_PATH = "/Marti/api/missions/Mapa%20test/contents/missionpackage"


class FakeChannel:
    def __init__(self):
        self.published = []

    def basic_publish(self, exchange, routing_key, body, properties=None):
        self.published.append(
            {"exchange": exchange, "routing_key": routing_key, "body": json.loads(body)}
        )

    def close(self):
        pass


@pytest.fixture
def channel(monkeypatch):
    fake = FakeChannel()
    connection = MagicMock()
    connection.channel.return_value = fake
    monkeypatch.setattr(pika, "BlockingConnection", lambda *a, **k: connection)
    return fake


@pytest.fixture
def server_key(app, tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    folder = tmp_path / "certs" / "opentakserver"
    folder.mkdir(parents=True)
    (folder / "opentakserver.pub").write_bytes(
        key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    app.config["OTS_CA_FOLDER"] = str(tmp_path)
    return key


USERNAME = "cloudtak-user"


def create_user(app, username):
    from opentakserver.extensions import db

    with app.app_context():
        user = app.security.datastore.create_user(username=username, password="x")
        db.session.commit()
        return user.id


def create_eud(app, uid, user_id):
    from opentakserver.extensions import db
    from opentakserver.models.EUD import EUD

    with app.app_context():
        eud = EUD()
        eud.uid = uid
        eud.callsign = uid
        eud.user_id = user_id
        db.session.add(eud)
        db.session.commit()


@pytest.fixture
def cert_user(monkeypatch):
    """Nazwa użytkownika z certyfikatu klienta (nginx przekazuje go w nagłówku)."""
    import importlib

    module = importlib.import_module("opentakserver.blueprints.marti_api.mission_marti_api")
    current = {"username": USERNAME}

    def fake_cert():
        if current["username"] is None:
            return False
        cert = MagicMock()
        cert.get_subject.return_value.commonName = current["username"]
        return cert

    monkeypatch.setattr(module, "verify_client_cert", fake_cert)
    return current


@pytest.fixture
def mission_app(app, server_key, cert_user):
    from opentakserver.extensions import db
    from opentakserver.models.Mission import Mission

    create_eud(app, CLIENT_UID, create_user(app, USERNAME))
    with app.app_context():
        mission = Mission()
        mission.name = MISSION
        mission.guid = GUID
        mission.tool = "public"
        mission.group = "__ANON__"
        db.session.add(mission)
        db.session.commit()
    return app


def token(server_key, mission_name=MISSION, guid=GUID, sub=CLIENT_UID):
    return jwt.encode(
        {"sub": sub, "MISSION_NAME": mission_name, "MISSION_GUID": guid},
        server_key,
        algorithm="RS256",
    )


def cot_xml(uid, callsign, detail=""):
    return (
        f'<event version="2.0" uid="{uid}" type="b-m-p-s-m" how="h-g-i-g-o" '
        'time="2026-10-16T10:00:00Z" start="2026-10-16T10:00:00Z" '
        'stale="2026-10-17T10:00:00Z">'
        '<point lat="52.1" lon="21.0" hae="0" ce="1" le="1"/>'
        f'<detail><contact callsign="{callsign}"/>{detail}</detail></event>'
    )


def make_package(files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "MANIFEST/manifest.xml",
            '<MissionPackageManifest version="2"><Configuration/><Contents/>'
            "</MissionPackageManifest>",
        )
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def parser_messages(channel):
    return [p for p in channel.published if p["exchange"] == "cot_parser"]


def upload(client, body, headers):
    return client.put(
        f"{PACKAGE_PATH}?creatorUid={CLIENT_UID}",
        data=body,
        headers=headers,
        content_type="application/x-zip-compressed",
    )


def test_package_cots_go_to_the_parser_with_mission_destination(
    mission_app, channel, server_key
):
    body = make_package(
        {
            "a1/point-1.cot": cot_xml("point-1", "Pierwszy"),
            "a2/point-2.cot": cot_xml(
                "point-2", "Drugi", f'<marti><dest mission="{MISSION}"/></marti>'
            ),
            "a3/image.png": b"\x89PNG",
        }
    )

    response = upload(
        mission_app.test_client(),
        body,
        {"MissionAuthorization": f"Bearer {token(server_key)}"},
    )

    assert response.status_code == 200
    messages = parser_messages(channel)
    assert [m["routing_key"] for m in messages] == ["cot_parser", "cot_parser"]
    uids = []
    for message in messages:
        assert message["body"]["uid"] == CLIENT_UID
        event = BeautifulSoup(message["body"]["cot"], "xml").find("event")
        uids.append(event.attrs["uid"])
        # Dokładnie jeden <dest mission>, nawet gdy CoT już go miał
        assert [d.attrs for d in event.find_all("dest")] == [{"mission": MISSION}]
    assert uids == ["point-1", "point-2"]


def test_package_end_to_end_lands_in_the_mission(mission_app, channel, server_key):
    from opentakserver.cot_parser.cot_parser import CoTController
    from opentakserver.extensions import db
    from opentakserver.models.MissionUID import MissionUID

    body = make_package({"a1/point-1.cot": cot_xml("point-1", "Pierwszy")})
    response = upload(
        mission_app.test_client(), body, {"Authorization": f"Bearer {token(server_key)}"}
    )
    assert response.status_code == 200

    parser = CoTController(mission_app.app_context(), MagicMock(), db, MagicMock())
    parser.rabbit_channel = MagicMock()
    for message in parser_messages(channel):
        parser.on_message(parser.rabbit_channel, MagicMock(), None, json.dumps(message["body"]))

    with mission_app.app_context():
        row = db.session.get(MissionUID, "point-1")
        assert row.mission_name == MISSION
        assert row.creator_uid == CLIENT_UID


def test_package_without_token_is_rejected(mission_app, channel):
    body = make_package({"a1/point-1.cot": cot_xml("point-1", "Pierwszy")})

    response = upload(mission_app.test_client(), body, {})

    assert response.status_code == 401
    assert channel.published == []


def test_package_with_token_for_another_mission_is_rejected(mission_app, channel, server_key):
    body = make_package({"a1/point-1.cot": cot_xml("point-1", "Pierwszy")})

    response = upload(
        mission_app.test_client(),
        body,
        {"MissionAuthorization": f"Bearer {token(server_key, mission_name='Inna')}"},
    )

    assert response.status_code == 401
    assert channel.published == []


def test_bad_zip_is_rejected(mission_app, channel, server_key):
    response = upload(
        mission_app.test_client(),
        b"to nie jest zip",
        {"MissionAuthorization": f"Bearer {token(server_key)}"},
    )

    assert response.status_code == 400
    assert channel.published == []


def test_package_over_the_size_limit_is_rejected(mission_app, channel, server_key, monkeypatch):
    import importlib

    module = importlib.import_module("opentakserver.blueprints.marti_api.mission_marti_api")
    monkeypatch.setattr(module, "MISSION_PACKAGE_MAX_BYTES", 5000)
    # Paczka mieści się w limicie, rozpakowana treść już nie (bomba zip)
    body = make_package({"a1/point-1.cot": cot_xml("point-1", "x" * 50000)})
    assert len(body) < 5000

    response = upload(
        mission_app.test_client(),
        body,
        {"MissionAuthorization": f"Bearer {token(server_key)}"},
    )

    assert response.status_code == 400
    assert channel.published == []


def test_package_with_unknown_mission_is_404(mission_app, channel, server_key):
    body = make_package({"a1/point-1.cot": cot_xml("point-1", "Pierwszy")})

    response = mission_app.test_client().put(
        "/Marti/api/missions/Nie%20ma/contents/missionpackage",
        data=body,
        headers={"MissionAuthorization": f"Bearer {token(server_key, mission_name='Nie ma')}"},
    )

    assert response.status_code == 404
    assert channel.published == []


def add_member(app, uid, mission_name=MISSION):
    from opentakserver.extensions import db
    from opentakserver.models.MissionUID import MissionUID

    with app.app_context():
        row = MissionUID()
        row.uid = uid
        row.mission_name = mission_name
        row.creator_uid = CLIENT_UID
        db.session.add(row)
        db.session.commit()


@pytest.mark.parametrize(
    "path",
    [
        f"/Marti/api/missions/guid/{GUID}/contents",
        f"/Marti/api/missions/gui/{GUID}/contents",
    ],
)
def test_delete_by_guid_removes_uid(mission_app, channel, server_key, path):
    from opentakserver.extensions import db
    from opentakserver.models.MissionChange import MissionChange
    from opentakserver.models.MissionUID import MissionUID

    add_member(mission_app, "point-1")

    response = mission_app.test_client().delete(
        f"{path}?uid=point-1",
        headers={"MissionAuthorization": f"Bearer {token(server_key)}"},
    )

    assert response.status_code == 200
    with mission_app.app_context():
        assert db.session.get(MissionUID, "point-1").mission_name is None
        [change] = db.session.query(MissionChange).all()
        assert (change.change_type, change.mission_name) == ("REMOVE_CONTENT", MISSION)
    [message] = channel.published
    assert message["routing_key"] == f"missions.{MISSION}"


def test_delete_by_unknown_guid_is_404(mission_app, channel, server_key):
    response = mission_app.test_client().delete(
        "/Marti/api/missions/guid/nie-ma/contents?uid=point-1",
        headers={"MissionAuthorization": f"Bearer {token(server_key)}"},
    )

    assert response.status_code == 404


def test_delete_by_guid_without_token_is_401(mission_app, channel):
    add_member(mission_app, "point-1")

    response = mission_app.test_client().delete(
        f"/Marti/api/missions/guid/{GUID}/contents?uid=point-1"
    )

    assert response.status_code == 401


def auth_headers(server_key, **kwargs):
    return {"MissionAuthorization": f"Bearer {token(server_key, **kwargs)}"}


def test_package_cot_cannot_address_people_directly(mission_app, channel, server_key):
    from opentakserver.cot_parser.cot_parser import CoTController
    from opentakserver.extensions import db

    detail = '<marti><dest callsign="X"/><dest uid="Y"/><dest mission="Inna"/></marti>'
    body = make_package({"a1/point-1.cot": cot_xml("point-1", "Pierwszy", detail)})

    response = upload(mission_app.test_client(), body, auth_headers(server_key))

    assert response.status_code == 200
    [message] = parser_messages(channel)
    event = BeautifulSoup(message["body"]["cot"], "xml").find("event")
    assert [d.attrs for d in event.find_all("dest")] == [{"mission": MISSION}]
    assert len(event.find_all("marti")) == 1

    parser = CoTController(mission_app.app_context(), MagicMock(), db, MagicMock())
    parser.rabbit_channel = MagicMock()
    parser.on_message(parser.rabbit_channel, MagicMock(), None, json.dumps(message["body"]))
    calls = parser.rabbit_channel.basic_publish.call_args_list
    exchanges = [c.kwargs.get("exchange") for c in calls]
    assert "dms" not in exchanges


def test_package_without_client_cert_is_rejected(mission_app, channel, server_key, cert_user):
    cert_user["username"] = None
    body = make_package({"a1/point-1.cot": cot_xml("point-1", "Pierwszy")})

    response = upload(mission_app.test_client(), body, auth_headers(server_key))

    assert response.status_code == 401
    assert channel.published == []


def test_package_with_cert_of_unknown_user_is_rejected(
    mission_app, channel, server_key, cert_user
):
    cert_user["username"] = "nie-ma-takiego"
    body = make_package({"a1/point-1.cot": cot_xml("point-1", "Pierwszy")})

    response = upload(mission_app.test_client(), body, auth_headers(server_key))

    assert response.status_code == 401
    assert channel.published == []


def test_token_of_another_users_device_is_rejected(mission_app, channel, server_key):
    create_eud(mission_app, "ANDROID-other", create_user(mission_app, "inny"))
    body = make_package({"a1/point-1.cot": cot_xml("point-1", "Pierwszy")})

    response = upload(
        mission_app.test_client(), body, auth_headers(server_key, sub="ANDROID-other")
    )

    assert response.status_code == 403
    assert channel.published == []


def test_token_of_unknown_device_is_rejected(mission_app, channel, server_key):
    body = make_package({"a1/point-1.cot": cot_xml("point-1", "Pierwszy")})

    response = upload(
        mission_app.test_client(), body, auth_headers(server_key, sub="ANDROID-nobody")
    )

    assert response.status_code == 403
    assert channel.published == []


def test_package_publishes_cert_user_id(mission_app, channel, server_key):
    from opentakserver.extensions import db
    from opentakserver.models.EUD import EUD

    body = make_package({"a1/point-1.cot": cot_xml("point-1", "Pierwszy")})

    upload(mission_app.test_client(), body, auth_headers(server_key))

    with mission_app.app_context():
        user_id = db.session.query(EUD).filter_by(uid=CLIENT_UID).one().user_id
    [message] = parser_messages(channel)
    assert message["body"]["user_id"] == user_id


def test_package_from_federation_account_is_rejected(
    mission_app, channel, server_key, cert_user
):
    create_eud(mission_app, "ANDROID-fed", create_user(mission_app, "fed_partner"))
    cert_user["username"] = "fed_partner"
    body = make_package({"a1/point-1.cot": cot_xml("point-1", "Pierwszy")})

    response = upload(mission_app.test_client(), body, auth_headers(server_key, sub="ANDROID-fed"))

    assert response.status_code == 403
    assert channel.published == []


def test_package_with_too_many_events_is_rejected(mission_app, channel, server_key, monkeypatch):
    import importlib

    module = importlib.import_module("opentakserver.blueprints.marti_api.mission_marti_api")
    monkeypatch.setattr(module, "MISSION_PACKAGE_MAX_EVENTS", 2)
    body = make_package({f"a{i}/point-{i}.cot": cot_xml(f"point-{i}", "x") for i in range(3)})

    response = upload(mission_app.test_client(), body, auth_headers(server_key))

    assert response.status_code == 400
    assert channel.published == []


def test_max_events_default_is_500():
    import importlib

    module = importlib.import_module("opentakserver.blueprints.marti_api.mission_marti_api")
    assert module.MISSION_PACKAGE_MAX_EVENTS == 500


def test_declared_body_over_limit_is_413(mission_app, channel, server_key, monkeypatch):
    import importlib

    module = importlib.import_module("opentakserver.blueprints.marti_api.mission_marti_api")
    monkeypatch.setattr(module, "MISSION_PACKAGE_MAX_BYTES", 100)
    body = make_package({"a1/point-1.cot": cot_xml("point-1", "Pierwszy")})

    response = upload(mission_app.test_client(), body, auth_headers(server_key))

    assert response.status_code == 413
    assert channel.published == []
