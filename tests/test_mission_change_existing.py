"""Zmiana misji z CoT z <dest mission=...> także dla obiektu, który już jest w misji.

ATAK 5.8 publikuje edycję obiektu z misji jako CoT z <dest mission>. Bez wiersza
MissionChange i bez t-x-m-c po ok. 11 s pokazuje "Failed to publish map item".
"""

import json
from unittest.mock import MagicMock

import pytest
from bs4 import BeautifulSoup

SENDER_UID = "ANDROID-sender"
MISSION_NAME = "Mapa test"


class FakeChannel:
    """Atrapa kanału RabbitMQ: zapisuje każde basic_publish."""

    is_closing = False
    is_closed = False

    def __init__(self):
        self.published = []

    def basic_publish(self, exchange, routing_key, body, properties=None):
        self.published.append(
            {"exchange": exchange, "routing_key": routing_key, "body": json.loads(body)}
        )


def make_event(event_uid, lat="52.1", callsign="Punkt 1", start="2026-10-16T10:00:00Z"):
    xml = (
        f'<event version="2.0" uid="{event_uid}" type="b-m-p-s-m" how="h-g-i-g-o" '
        f'time="{start}" start="{start}" stale="2026-10-17T10:00:00Z">'
        f'<point lat="{lat}" lon="21.0" hae="0" ce="9999999.0" le="9999999.0"/>'
        f'<detail><contact callsign="{callsign}"/><color argb="-1"/>'
        f'<marti><dest mission="{MISSION_NAME}"/></marti></detail></event>'
    )
    return BeautifulSoup(xml, "xml").find("event")


@pytest.fixture
def mission(app):
    from opentakserver.extensions import db
    from opentakserver.models.Mission import Mission

    with app.app_context():
        row = Mission()
        row.name = MISSION_NAME
        row.guid = "5f1d1c9e-6a1e-4c51-9c3f-1a2b3c4d5e6f"
        row.tool = "public"
        row.group = "__ANON__"
        db.session.add(row)
        db.session.commit()
    return MISSION_NAME


@pytest.fixture
def controller(app, mission):
    from opentakserver.cot_parser.cot_parser import CoTController
    from opentakserver.extensions import db

    parser = CoTController(app.app_context(), MagicMock(), db, MagicMock())
    parser.rabbit_channel = FakeChannel()
    return parser


def mission_uids(app):
    from opentakserver.extensions import db
    from opentakserver.models.MissionUID import MissionUID

    with app.app_context():
        return {
            row.uid: (row.mission_name, row.callsign, row.latitude, row.creator_uid)
            for row in db.session.query(MissionUID).all()
        }


def mission_changes(app):
    from opentakserver.extensions import db
    from opentakserver.models.MissionChange import MissionChange

    with app.app_context():
        return [
            (row.change_type, row.mission_uid, row.mission_name)
            for row in db.session.query(MissionChange).order_by(MissionChange.id).all()
        ]


def assert_raw_and_change(publications, event_uid):
    raw, change = publications
    assert raw["exchange"] == "missions"
    assert raw["routing_key"] == f"missions.{MISSION_NAME}"
    assert raw["body"]["uid"] == SENDER_UID
    assert f'uid="{event_uid}"' in raw["body"]["cot"]

    assert change["exchange"] == "missions"
    assert change["routing_key"] == f"missions.{MISSION_NAME}"
    cot = BeautifulSoup(change["body"]["cot"], "xml").find("event")
    assert cot.attrs["type"] == "t-x-m-c"
    assert cot.find("mission").attrs["type"] == "ADD_CONTENT"
    assert cot.find("MissionChange").find("contentUid").text == event_uid
    assert cot.find("MissionChange").find("type").text == "ADD_CONTENT"
    # ATAK zdejmuje oczekującą publikację tylko przy creatorUid równym własnemu uid
    assert cot.find("MissionChange").find("creatorUid").text == SENDER_UID


def test_first_add_records_change_and_publishes_twice(app, controller):
    controller.generate_mission_change(SENDER_UID, make_event("point-1"))

    assert mission_uids(app) == {"point-1": (MISSION_NAME, "Punkt 1", 52.1, SENDER_UID)}
    assert mission_changes(app) == [("ADD_CONTENT", "point-1", MISSION_NAME)]
    assert len(controller.rabbit_channel.published) == 2
    assert_raw_and_change(controller.rabbit_channel.published, "point-1")


def test_edit_of_object_already_in_mission_records_another_change(app, controller):
    controller.generate_mission_change(SENDER_UID, make_event("point-1"))
    controller.rabbit_channel.published.clear()

    controller.generate_mission_change(
        SENDER_UID,
        make_event("point-1", lat="52.2", callsign="Punkt 1b", start="2026-10-16T10:05:00Z"),
    )

    assert mission_uids(app) == {"point-1": (MISSION_NAME, "Punkt 1b", 52.2, SENDER_UID)}
    assert mission_changes(app) == [
        ("ADD_CONTENT", "point-1", MISSION_NAME),
        ("ADD_CONTENT", "point-1", MISSION_NAME),
    ]
    assert len(controller.rabbit_channel.published) == 2
    assert_raw_and_change(controller.rabbit_channel.published, "point-1")


def test_edit_does_not_touch_other_mission_uid_rows(app, controller):
    controller.generate_mission_change(SENDER_UID, make_event("point-1"))
    controller.generate_mission_change(
        "ANDROID-other", make_event("point-2", lat="50.0", callsign="Inny")
    )

    controller.generate_mission_change(
        SENDER_UID,
        make_event("point-1", lat="52.2", callsign="Punkt 1b", start="2026-10-16T10:05:00Z"),
    )

    assert mission_uids(app)["point-2"] == (MISSION_NAME, "Inny", 50.0, "ANDROID-other")
