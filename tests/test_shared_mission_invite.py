"""Misja wspólna: urządzenie dostaje zaproszenie (t-x-m-i) przy połączeniu.

ATAK z datasyncAutoAcceptInvite=true subskrybuje sam po zaproszeniu. OTS
zaprasza tylko urządzenie bez wiersza mission_roles i bez wiersza zaproszenia
(ten zostaje, dopóki subskrypcja go nie zużyje; „Block invitations” w ATAK
działa lokalnie i na serwerze nie zostawia śladu), najwyżej raz na 10 minut.
"""

import json
import logging
import os
from unittest.mock import MagicMock
from xml.etree.ElementTree import fromstring

import jwt
import pika
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from opentakserver.eud_handler import EudHandler as eud_handler_module
from opentakserver.eud_handler.EudHandler import EudHandler

SHARED = "Mapa test"
DEVICE_UID = "ANDROID-device-1"
CLOUDTAK_UID = "ANDROID-CloudTAK-abc"


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
    """Klucz serwera do podpisu tokenu misji, tak jak leży w OTS_CA_FOLDER."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    folder = tmp_path / "certs" / "opentakserver"
    folder.mkdir(parents=True)
    (folder / "opentakserver.nopass.key").write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    app.config["OTS_CA_FOLDER"] = str(tmp_path)
    return key.public_key()


@pytest.fixture(autouse=True)
def forget_invites(monkeypatch):
    monkeypatch.setattr(eud_handler_module, "_shared_invites", {})


@pytest.fixture
def shared_app(app, server_key):
    from opentakserver.extensions import db
    from opentakserver.models.EUD import EUD
    from opentakserver.models.Mission import Mission

    app.config["OTS_SHARED_MISSION"] = SHARED
    with app.app_context():
        # missions.creator_uid wskazuje na euds.uid
        admin = EUD()
        admin.uid = "ADMIN"
        admin.callsign = "ADMIN"
        db.session.add(admin)
        db.session.commit()
        mission = Mission()
        mission.name = SHARED
        mission.guid = "guid-mapa-test"
        mission.tool = "public"
        mission.group = "__ANON__"
        mission.creator_uid = "ADMIN"
        mission.default_role = "MISSION_SUBSCRIBER"
        db.session.add(mission)
        for uid in (DEVICE_UID, CLOUDTAK_UID):
            eud = EUD()
            eud.uid = uid
            eud.callsign = uid
            db.session.add(eud)
        db.session.commit()
    return app


def make_handler(app, uid=DEVICE_UID):
    handler = EudHandler.__new__(EudHandler)
    handler.app = app
    handler.uid = uid
    handler.logger = logging.getLogger("test")
    return handler


def invitations(app):
    from opentakserver.extensions import db
    from opentakserver.models.MissionInvitation import MissionInvitation

    with app.app_context():
        return [
            (row.mission_name, row.client_uid, row.creator_uid, row.type)
            for row in db.session.execute(db.select(MissionInvitation)).scalars()
        ]


def add_role(app, uid=DEVICE_UID):
    import datetime

    from opentakserver.extensions import db
    from opentakserver.models.MissionRole import MissionRole

    with app.app_context():
        role = MissionRole()
        role.clientUid = uid
        role.username = "anonymous"
        role.createTime = datetime.datetime.now(datetime.timezone.utc)
        role.role_type = "MISSION_SUBSCRIBER"
        role.mission_name = SHARED
        db.session.add(role)
        db.session.commit()


def test_invite_to_mission_without_request_context(shared_app, channel, server_key):
    from opentakserver.blueprints.marti_api.mission_marti_api import invite_to_mission

    with shared_app.app_context():
        assert invite_to_mission(SHARED, DEVICE_UID, "Mapa wspólna") is True

    assert invitations(shared_app) == [(SHARED, DEVICE_UID, "Mapa wspólna", "clientUid")]
    assert len(channel.published) == 1
    message = channel.published[0]
    assert message["exchange"] == "dms"
    assert message["routing_key"] == DEVICE_UID
    event = fromstring(message["body"]["cot"])
    assert event.get("type") == "t-x-m-i"
    mission_tag = event.find("detail/mission")
    assert mission_tag.get("name") == SHARED
    # ATAK pokazuje authorUid w oknie „based on invite from: …”
    assert mission_tag.get("authorUid") == "Mapa wspólna"
    token = jwt.decode(mission_tag.get("token"), server_key, algorithms=["RS256"])
    assert token["sub"] == DEVICE_UID
    assert token["MISSION_NAME"] == SHARED


def test_invite_to_unknown_mission_returns_false(shared_app, channel):
    from opentakserver.blueprints.marti_api.mission_marti_api import invite_to_mission

    with shared_app.app_context():
        assert invite_to_mission("Nie ma takiej", DEVICE_UID, "x") is False
    assert channel.published == []


def test_a_device_without_role_is_invited(shared_app, channel):
    assert make_handler(shared_app).maybe_invite_shared_mission() is True

    assert invitations(shared_app) == [(SHARED, DEVICE_UID, "Mapa wspólna", "clientUid")]
    assert [m["routing_key"] for m in channel.published] == [DEVICE_UID]


def test_b_device_with_role_is_not_invited(shared_app, channel):
    add_role(shared_app)

    assert make_handler(shared_app).maybe_invite_shared_mission() is False
    assert invitations(shared_app) == []
    assert channel.published == []


def test_c_second_call_within_ten_minutes_does_nothing(shared_app, channel):
    from opentakserver.extensions import db
    from opentakserver.models.MissionInvitation import MissionInvitation

    assert make_handler(shared_app).maybe_invite_shared_mission() is True
    # Nawet gdy wiersz zaproszenia zniknął (np. subskrypcja go usunęła, a potem
    # urządzenie zrezygnowało), w ciągu 10 minut nie zapraszamy ponownie
    with shared_app.app_context():
        db.session.execute(db.delete(MissionInvitation))
        db.session.commit()

    assert make_handler(shared_app).maybe_invite_shared_mission() is False
    assert len(channel.published) == 1


def test_c_invited_again_after_ten_minutes(shared_app, channel, monkeypatch):
    from opentakserver.extensions import db
    from opentakserver.models.MissionInvitation import MissionInvitation

    clock = [1000.0]
    monkeypatch.setattr(eud_handler_module.time, "monotonic", lambda: clock[0])
    assert make_handler(shared_app).maybe_invite_shared_mission() is True
    with shared_app.app_context():
        db.session.execute(db.delete(MissionInvitation))
        db.session.commit()

    clock[0] += 601
    assert make_handler(shared_app).maybe_invite_shared_mission() is True
    assert len(channel.published) == 2


def test_d_existing_invitation_row_blocks_new_invitation(shared_app, channel):
    from opentakserver.extensions import db
    from opentakserver.models.MissionInvitation import MissionInvitation

    with shared_app.app_context():
        row = MissionInvitation()
        row.mission_name = SHARED
        row.client_uid = DEVICE_UID
        row.type = "clientUid"
        db.session.add(row)
        db.session.commit()

    assert make_handler(shared_app).maybe_invite_shared_mission() is False
    assert len(invitations(shared_app)) == 1
    assert channel.published == []


def test_e_disabled_does_nothing(shared_app, channel):
    shared_app.config["OTS_SHARED_MISSION"] = ""

    assert make_handler(shared_app).maybe_invite_shared_mission() is False
    assert invitations(shared_app) == []
    assert channel.published == []


def test_cloudtak_connection_is_skipped(shared_app, channel):
    assert make_handler(shared_app, CLOUDTAK_UID).maybe_invite_shared_mission() is False
    assert invitations(shared_app) == []
    assert channel.published == []


def test_handle_cot_invites_on_the_event_after_identification(shared_app, channel):
    """Pierwsze zdarzenie identyfikuje urządzenie i wiąże jego kolejkę z dms
    (asynchronicznie); zaproszenie idzie przy następnym, raz na połączenie."""
    handler = make_handler(shared_app, uid=None)
    handler.is_ssl = False
    handler.parse_device_info = lambda event: setattr(handler, "uid", DEVICE_UID)
    handler.publish_cot = lambda event: None
    handler.pong = lambda event: False
    calls = []
    handler.maybe_invite_shared_mission = lambda: calls.append(1)
    cot = f'<event uid="{DEVICE_UID}" type="a-f-G-U-C"><detail/></event>'

    handler.handle_cot(cot)
    assert calls == []
    handler.handle_cot(cot)
    handler.handle_cot(cot)

    assert calls == [1]


def test_http_invite_route_unchanged(shared_app, client, channel, monkeypatch):
    import importlib

    mission_marti_api = importlib.import_module(
        "opentakserver.blueprints.marti_api.mission_marti_api"
    )

    monkeypatch.setattr(mission_marti_api, "verify_token", lambda: {"MISSION_NAME": SHARED})

    response = client.put(f"/Marti/api/missions/{SHARED}/invite/clientUid/{DEVICE_UID}")

    assert response.status_code == 200
    assert invitations(shared_app) == [(SHARED, DEVICE_UID, None, "clientUid")]
    assert [m["routing_key"] for m in channel.published] == [DEVICE_UID]
    event = fromstring(channel.published[0]["body"]["cot"])
    assert event.find("detail/mission").get("authorUid") == "ADMIN"


@pytest.fixture
def dispose_spy(shared_app, monkeypatch):
    """Liczy wywołania db.engine.dispose() i stan puli po nich."""
    from opentakserver.extensions import db

    with shared_app.app_context():
        engine = db.engine
    calls = []
    original = engine.dispose

    def spy(*args, **kwargs):
        original(*args, **kwargs)
        calls.append(engine.pool.checkedout())

    monkeypatch.setattr(engine, "dispose", spy)
    return calls


def test_pool_disposed_when_invited(shared_app, channel, dispose_spy):
    assert make_handler(shared_app).maybe_invite_shared_mission() is True
    assert dispose_spy == [0]


def test_pool_disposed_when_role_exists(shared_app, channel, dispose_spy):
    add_role(shared_app)
    assert make_handler(shared_app).maybe_invite_shared_mission() is False
    assert dispose_spy == [0]


def test_pool_disposed_and_no_raise_when_publish_fails(shared_app, monkeypatch, dispose_spy):
    def broken(*args, **kwargs):
        raise pika.exceptions.AMQPConnectionError("down")

    monkeypatch.setattr(pika, "BlockingConnection", broken)

    assert make_handler(shared_app).maybe_invite_shared_mission() is False
    assert dispose_spy == [0]
    # Nieudana próba nie blokuje ponownej na 10 minut
    assert eud_handler_module._shared_invites == {}


def test_federation_account_is_not_invited(shared_app, channel):
    handler = make_handler(shared_app)
    handler.user = MagicMock(username="fed_partner")

    assert handler.maybe_invite_shared_mission() is False
    assert invitations(shared_app) == []
    assert channel.published == []


def test_regular_account_is_invited(shared_app, channel):
    handler = make_handler(shared_app)
    handler.user = MagicMock(username="kaszub")

    assert handler.maybe_invite_shared_mission() is True
