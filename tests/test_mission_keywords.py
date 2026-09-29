"""PUT /Marti/api/missions/<name>/keywords -- zastępuje listę i zwraca kopertę Mission."""

import importlib

import pytest


@pytest.fixture
def mission(app):
    from opentakserver.extensions import db
    from opentakserver.models.Mission import Mission

    with app.app_context():
        db.session.add(Mission(name="kw-mission", keywords=["x"], guid="guid-kw"))
        db.session.commit()
    return "kw-mission"


@pytest.fixture(autouse=True)
def mission_token(app, monkeypatch):
    mission_marti_api = importlib.import_module(
        "opentakserver.blueprints.marti_api.mission_marti_api"
    )

    # Token misji jest sprawdzany osobno; tu chodzi o samą trasę.
    monkeypatch.setattr(
        mission_marti_api,
        "verify_token",
        lambda: {"MISSION_NAME": "kw-mission", "MISSION_GUID": "guid-kw"},
    )


def stored_keywords(app, name):
    from opentakserver.extensions import db
    from opentakserver.models.Mission import Mission

    with app.app_context():
        return db.session.query(Mission).filter_by(name=name).one().keywords


def test_put_replaces_keywords_and_returns_mission(app, client, mission):
    response = client.put(f"/Marti/api/missions/{mission}/keywords", json=["a", "b"])

    assert response.status_code == 200
    assert response.mimetype == "application/json"
    assert response.json["type"] == "Mission"
    assert response.json["version"] == "3"
    assert "nodeId" in response.json
    assert response.json["data"][0]["name"] == mission
    assert response.json["data"][0]["keywords"] == ["a", "b"]
    assert stored_keywords(app, mission) == ["a", "b"]


def test_put_empty_list_clears_keywords(app, client, mission):
    response = client.put(f"/Marti/api/missions/{mission}/keywords", json=[])

    assert response.status_code == 200
    assert response.json["data"][0]["keywords"] == []
    assert stored_keywords(app, mission) == []


def test_put_unknown_mission_is_404(client, mission, monkeypatch):
    mission_marti_api = importlib.import_module(
        "opentakserver.blueprints.marti_api.mission_marti_api"
    )

    monkeypatch.setattr(
        mission_marti_api,
        "verify_token",
        lambda: {"MISSION_NAME": "nope", "MISSION_GUID": "x"},
    )
    response = client.put("/Marti/api/missions/nope/keywords", json=["a"])

    assert response.status_code == 404


def test_put_non_list_body_is_400_and_keeps_keywords(app, client, mission):
    response = client.put(f"/Marti/api/missions/{mission}/keywords", json={"a": 1})

    assert response.status_code == 400
    assert response.json["success"] is False
    assert stored_keywords(app, mission) == ["x"]
