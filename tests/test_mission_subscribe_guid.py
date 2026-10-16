"""Subskrypcja misji po GUID (tak subskrybuje CloudTAK).

Trasa po GUID nie znała nazwy misji: wiersz MissionRole dostawał
mission_name=None (każda subskrypcja dopisywała nowy), a kolejka była wiązana
z kluczem missions.None, więc subskrybent nie dostawał zmian misji na żywo.
"""

import importlib
from unittest.mock import MagicMock

import pika
import pytest

MISSION = "Mapa test"
GUID = "guid-mapa-test"
CLIENT_UID = "ANDROID-CloudTAK-user"
USERNAME = "cloudtak-user"


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
def subscriber(app, monkeypatch):
    from opentakserver.extensions import db
    from opentakserver.models.EUD import EUD
    from opentakserver.models.Mission import Mission

    mission_marti_api = importlib.import_module(
        "opentakserver.blueprints.marti_api.mission_marti_api"
    )
    cert = MagicMock()
    cert.get_subject.return_value.commonName = USERNAME
    monkeypatch.setattr(mission_marti_api, "verify_client_cert", lambda: cert)
    # Klucz serwera do podpisu tokenu nie istnieje w testach
    monkeypatch.setattr(mission_marti_api, "generate_token", lambda mission, uid: "token")

    with app.app_context():
        user = app.security.datastore.create_user(username=USERNAME, password="x")
        db.session.commit()
        mission = Mission()
        mission.name = MISSION
        mission.guid = GUID
        mission.tool = "public"
        mission.group = "__ANON__"
        mission.default_role = "MISSION_SUBSCRIBER"
        db.session.add(mission)
        eud = EUD()
        eud.uid = CLIENT_UID
        eud.callsign = "cloudtak"
        eud.user_id = user.id
        db.session.add(eud)
        db.session.commit()
    return app


def roles(app):
    from opentakserver.extensions import db
    from opentakserver.models.MissionRole import MissionRole

    with app.app_context():
        return [
            (row.clientUid, row.mission_name)
            for row in db.session.execute(db.select(MissionRole)).scalars()
        ]


def test_subscribe_by_guid_twice_keeps_one_role_with_mission_name(subscriber, client, channel):
    for _ in range(2):
        response = client.put(f"/Marti/api/missions/guid/{GUID}/subscription?uid={CLIENT_UID}")
        assert response.status_code == 201

    assert roles(subscriber) == [(CLIENT_UID, MISSION)]
    assert (CLIENT_UID, "missions", f"missions.{MISSION}") in channel.bindings
    assert all(key != "missions.None" for _, _, key in channel.bindings)


def test_subscribe_by_unknown_guid_is_404(subscriber, client, channel):
    response = client.put(f"/Marti/api/missions/guid/no-such-guid/subscription?uid={CLIENT_UID}")
    assert response.status_code == 404
    assert roles(subscriber) == []


def test_subscribe_by_name_unchanged(subscriber, client, channel):
    response = client.put(f"/Marti/api/missions/{MISSION}/subscription?uid={CLIENT_UID}")
    assert response.status_code == 201
    assert roles(subscriber) == [(CLIENT_UID, MISSION)]
    assert (CLIENT_UID, "missions", f"missions.{MISSION}") in channel.bindings
