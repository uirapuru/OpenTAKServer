"""Subskrypcja CloudTAK bez wcześniejszej pozycji własnej.

OTS zakłada wiersz euds dopiero po pozycji własnej urządzenia. CloudTAK w
przeglądarce bez lokalizacji jej nie wysyła, więc subskrypcja kończyła się
400 "Invalid UID" i użytkownik nigdy nie dołączał do misji wspólnej. Wiersz
zakładamy tylko dla uid ANDROID-CloudTAK-<użytkownik z certyfikatu>.
"""

import importlib
from unittest.mock import MagicMock

import pika
import pytest

MISSION = "Mapa test"
GUID = "guid-mapa-test"


class FakeChannel:
    def __init__(self):
        self.bindings = []

    def queue_declare(self, queue):
        pass

    def queue_bind(self, queue, exchange, routing_key):
        self.bindings.append((queue, exchange, routing_key))

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
def cert_user(monkeypatch):
    module = importlib.import_module("opentakserver.blueprints.marti_api.mission_marti_api")
    current = {"username": "alice"}

    def fake_cert():
        if current["username"] is None:
            return False
        cert = MagicMock()
        cert.get_subject.return_value.commonName = current["username"]
        return cert

    monkeypatch.setattr(module, "verify_client_cert", fake_cert)
    # Klucz serwera do podpisu tokenu nie istnieje w testach
    monkeypatch.setattr(module, "generate_token", lambda mission, uid: "token")
    return current


@pytest.fixture
def mission_app(app, cert_user):
    from opentakserver.extensions import db
    from opentakserver.models.Mission import Mission

    with app.app_context():
        for name in ("alice", "bob"):
            app.security.datastore.create_user(username=name, password="x")
        db.session.commit()
        mission = Mission()
        mission.name = MISSION
        mission.guid = GUID
        mission.tool = "public"
        mission.group = "__ANON__"
        mission.default_role = "MISSION_SUBSCRIBER"
        db.session.add(mission)
        db.session.commit()
    return app


def euds(app):
    from opentakserver.extensions import db
    from opentakserver.models.EUD import EUD

    with app.app_context():
        return {
            row.uid: (row.user.username if row.user else None, row.callsign)
            for row in db.session.execute(db.select(EUD)).scalars()
        }


def roles(app):
    from opentakserver.extensions import db
    from opentakserver.models.MissionRole import MissionRole

    with app.app_context():
        return [
            (row.clientUid, row.mission_name, row.username)
            for row in db.session.execute(db.select(MissionRole)).scalars()
        ]


def subscribe(app, uid):
    return app.test_client().put(f"/Marti/api/missions/guid/{GUID}/subscription?uid={uid}")


def test_a_cloudtak_uid_of_cert_user_creates_eud_and_subscribes(mission_app, channel):
    response = subscribe(mission_app, "ANDROID-CloudTAK-alice")

    assert response.status_code == 201
    assert euds(mission_app) == {"ANDROID-CloudTAK-alice": ("alice", "alice")}
    assert roles(mission_app) == [("ANDROID-CloudTAK-alice", MISSION, "alice")]
    assert ("ANDROID-CloudTAK-alice", "missions", f"missions.{MISSION}") in channel.bindings


def test_a_taken_callsign_does_not_block_the_subscription(mission_app, channel):
    from opentakserver.extensions import db
    from opentakserver.models.EUD import EUD

    with mission_app.app_context():
        phone = EUD()
        phone.uid = "ANDROID-phone"
        phone.callsign = "alice"
        db.session.add(phone)
        db.session.commit()

    response = subscribe(mission_app, "ANDROID-CloudTAK-alice")

    assert response.status_code == 201
    assert euds(mission_app)["ANDROID-CloudTAK-alice"] == ("alice", None)


def test_b_cloudtak_uid_of_another_user_is_rejected(mission_app, channel):
    response = subscribe(mission_app, "ANDROID-CloudTAK-bob")

    assert response.status_code == 400
    assert euds(mission_app) == {}
    assert roles(mission_app) == []


def test_c_no_client_cert_is_rejected(mission_app, channel, cert_user):
    cert_user["username"] = None

    response = subscribe(mission_app, "ANDROID-CloudTAK-alice")

    assert response.status_code == 400
    assert euds(mission_app) == {}
    assert roles(mission_app) == []


def test_d_arbitrary_uid_is_rejected(mission_app, channel):
    response = subscribe(mission_app, "ANDROID-123")

    assert response.status_code == 400
    assert euds(mission_app) == {}
    assert roles(mission_app) == []


def test_e_existing_eud_path_unchanged(mission_app, channel):
    from opentakserver.extensions import db
    from opentakserver.models.EUD import EUD
    from opentakserver.models.user import User

    with mission_app.app_context():
        bob = db.session.execute(db.select(User).filter_by(username="bob")).scalar()
        eud = EUD()
        eud.uid = "ANDROID-123"
        eud.callsign = "Telefon"
        eud.user_id = bob.id
        db.session.add(eud)
        db.session.commit()

    response = subscribe(mission_app, "ANDROID-123")

    assert response.status_code == 201
    assert euds(mission_app) == {"ANDROID-123": ("bob", "Telefon")}
    assert roles(mission_app) == [("ANDROID-123", MISSION, "bob")]
