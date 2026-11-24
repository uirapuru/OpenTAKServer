"""Dołączanie treści do misji trasą po GUID: PUT /Marti/api/missions/guid/<guid>/contents.

CloudTAK (załącznik do obiektu w misji) woła tę trasę po GUID. Funkcja szukała
misji po GUID, ale dalej zapisywała wszystko z `mission_name=None`, więc
`mission_content_mission.mission_name` łamało NOT NULL i serwer oddawał 500
(public, 24 XI 2026).
"""

from opentakserver.extensions import db
from tests.test_mission_package_upload import (  # noqa: F401  (fixtures)
    CLIENT_UID,
    GUID,
    MISSION,
    auth_headers,
    cert_user,
    channel,
    mission_app,
    server_key,
)

HASH = "a" * 64


def add_file(app):
    from opentakserver.models.MissionContent import MissionContent

    with app.app_context():
        content = MissionContent()
        content.hash = HASH
        content.uid = "file-uid-1"
        content.filename = "zdjecie.png"
        content.creator_uid = "administrator"
        db.session.add(content)
        db.session.commit()


def test_file_attached_by_guid_is_linked_to_the_mission(mission_app, channel, server_key):
    from opentakserver.models.MissionChange import MissionChange
    from opentakserver.models.MissionContentMission import MissionContentMission

    add_file(mission_app)
    response = mission_app.test_client().put(
        f"/Marti/api/missions/guid/{GUID}/contents",
        json={"hashes": [HASH]},
        headers=auth_headers(server_key),
    )

    assert response.status_code == 200, response.get_data(as_text=True)
    with mission_app.app_context():
        links = db.session.query(MissionContentMission).all()
        assert [link.mission_name for link in links] == [MISSION]
        changes = db.session.query(MissionChange).filter_by(content_uid="file-uid-1").all()
        assert [change.mission_name for change in changes] == [MISSION]
    assert [m["routing_key"] for m in channel.published] == [f"missions.{MISSION}"]


def test_uid_added_by_guid_lands_in_the_mission(mission_app, channel, server_key):
    from opentakserver.models.MissionUID import MissionUID

    response = mission_app.test_client().put(
        f"/Marti/api/missions/guid/{GUID}/contents?creatorUid={CLIENT_UID}",
        json={"uids": ["point-1"]},
        headers=auth_headers(server_key),
    )

    assert response.status_code == 200, response.get_data(as_text=True)
    with mission_app.app_context():
        rows = db.session.query(MissionUID).filter_by(uid="point-1").all()
        assert [row.mission_name for row in rows] == [MISSION]
    assert [m["routing_key"] for m in channel.published] == [f"missions.{MISSION}"]
