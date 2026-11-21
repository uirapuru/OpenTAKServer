"""GET misji po GUID dla misji z hasłem (tak pyta CloudTAK).

CloudTAK po subskrypcji pobiera misję z tokenem misji, bez hasła. OTS żądał
hasła mimo ważnego tokenu i zwracał 401, więc CloudTAK nie mógł nic zapisać do
feedu z hasłem (poligon26, 21 XI 2026).
"""

import importlib

import pytest
from flask_security import hash_password

MISSION = "pluton-test"
GUID = "guid-pluton-test"
CLIENT_UID = "ANDROID-CloudTAK-user"


@pytest.fixture
def protected(app, monkeypatch):
    from opentakserver.extensions import db
    from opentakserver.models.Mission import Mission

    api = importlib.import_module("opentakserver.blueprints.marti_api.mission_marti_api")
    monkeypatch.setattr(
        api, "verify_token", lambda: {"sub": CLIENT_UID, "MISSION_NAME": MISSION, "MISSION_GUID": GUID}
    )
    monkeypatch.setattr(api, "generate_token", lambda mission, uid: f"token-{mission.name}-{uid}")
    with app.app_context():
        mission = Mission()
        mission.name = MISSION
        mission.guid = GUID
        mission.tool = "public"
        mission.group = "__ANON__"
        mission.default_role = "MISSION_SUBSCRIBER"
        mission.password = hash_password("arbuz123")
        mission.password_protected = True
        db.session.add(mission)
        db.session.commit()
    return app


def test_valid_token_without_password_gets_the_mission(protected, client):
    response = client.get(f"/Marti/api/missions/guid/{GUID}", headers={"MissionAuthorization": "Bearer x"})
    assert response.status_code == 200
    assert response.get_json()["data"][0]["name"] == MISSION


def test_response_carries_a_mission_token(protected, client):
    response = client.get(f"/Marti/api/missions/guid/{GUID}?password=arbuz123")
    assert response.status_code == 200
    assert response.get_json()["data"][0]["token"] == f"token-{MISSION}-{CLIENT_UID}"


def test_wrong_password_is_still_refused(protected, client):
    response = client.get(f"/Marti/api/missions/guid/{GUID}?password=zle")
    assert response.status_code == 401
