"""Misja wspólna: obiekty rozgłoszone (bez <dest>) trafiają do misji OTS_SHARED_MISSION,
t-x-d-d z <__forcedelete/> zdejmuje obiekt z misji, w której jest.
"""

import json
import logging
from unittest.mock import MagicMock

import pytest
from bs4 import BeautifulSoup

SENDER_UID = "ANDROID-sender"
SHARED = "Mapa test"
OTHER = "Inna misja"


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

    def basic_ack(self, delivery_tag=None):
        pass

    def basic_nack(self, delivery_tag=None, requeue=False):
        raise AssertionError("on_message odrzucił wiadomość")


def make_xml(event_uid, event_type, detail=""):
    return (
        f'<event version="2.0" uid="{event_uid}" type="{event_type}" how="h-g-i-g-o" '
        'time="2026-10-16T10:00:00Z" start="2026-10-16T10:00:00Z" '
        'stale="2026-10-17T10:00:00Z">'
        '<point lat="52.1" lon="21.0" hae="0" ce="9999999.0" le="9999999.0"/>'
        f'<detail><contact callsign="{event_uid}"/>{detail}</detail></event>'
    )


def parse(xml):
    return BeautifulSoup(xml, "xml").find("event")


def add_mission(app, name):
    from opentakserver.extensions import db
    from opentakserver.models.Mission import Mission

    with app.app_context():
        row = Mission()
        row.name = name
        row.guid = f"guid-{name}"
        row.tool = "public"
        row.group = "__ANON__"
        db.session.add(row)
        db.session.commit()


@pytest.fixture
def shared_app(app):
    from opentakserver.extensions import db
    from opentakserver.models.EUD import EUD

    app.config["OTS_SHARED_MISSION"] = SHARED
    add_mission(app, SHARED)
    add_mission(app, OTHER)
    with app.app_context():
        eud = EUD()
        eud.uid = SENDER_UID
        eud.callsign = "sender"
        db.session.add(eud)
        db.session.commit()
    return app


@pytest.fixture
def controller(shared_app):
    from opentakserver.cot_parser.cot_parser import CoTController
    from opentakserver.extensions import db

    parser = CoTController(shared_app.app_context(), MagicMock(), db, MagicMock())
    parser.rabbit_channel = FakeChannel()
    return parser


def deliver(controller, xml, sender_uid=SENDER_UID):
    body = json.dumps({"uid": sender_uid, "cot": xml, "user_id": None}).encode()
    controller.on_message(controller.rabbit_channel, MagicMock(), None, body)


def mission_uids(app):
    from opentakserver.extensions import db
    from opentakserver.models.MissionUID import MissionUID

    with app.app_context():
        return {row.uid: row.mission_name for row in db.session.query(MissionUID).all()}


def mission_changes(app):
    from opentakserver.extensions import db
    from opentakserver.models.MissionChange import MissionChange

    with app.app_context():
        return [
            (row.change_type, row.mission_uid, row.mission_name)
            for row in db.session.query(MissionChange).order_by(MissionChange.id).all()
        ]


def mission_publications(controller):
    return [p for p in controller.rabbit_channel.published if p["exchange"] == "missions"]


def change_cots(controller):
    cots = []
    for publication in mission_publications(controller):
        cot = BeautifulSoup(publication["body"]["cot"], "xml").find("event")
        if cot.attrs["type"] == "t-x-m-c":
            cots.append((publication["routing_key"], cot))
    return cots


# --- is_shareable (funkcja czysta) ---


@pytest.mark.parametrize(
    "event_uid,event_type,detail,expected",
    [
        ("shape-1", "u-d-f", "", True),
        ("route-1", "b-m-r", "", True),
        ("point-1", "b-m-p-s-m", "", True),
        ("enemy-1", "a-h-G", "", True),
        (SENDER_UID, "a-f-G-U-C", "", False),
        ("shape-1", "u-d-f", '<marti><dest uid="X"/></marti>', False),
        ("shape-1", "u-d-f", '<marti><dest callsign="X"/></marti>', False),
        ("friend-1", "a-f-G-U-C", '<__group name="Cyan" role="Team Member"/>', False),
        ("msg-1", "b-t-f", '<__chat id="All Chat Rooms" chatroom="All Chat Rooms"/>', False),
        ("enemy-1", "a-h-G", '<__chat id="All Chat Rooms"/>', False),
        ("ping-1", "t-x-c-t", "", False),
        ("snap-1", "y-taklab-map", "", False),
        ("alert-1", "b-a-o-tbl", "", False),
        ("other-1", "b-i-v", "", False),
    ],
)
def test_is_shareable(event_uid, event_type, detail, expected):
    from opentakserver.cot_parser.shared_mission import is_shareable

    assert is_shareable(parse(make_xml(event_uid, event_type, detail)), SENDER_UID) is expected


def test_is_shareable_without_sender_is_false():
    from opentakserver.cot_parser.shared_mission import is_shareable

    assert is_shareable(parse(make_xml("shape-1", "u-d-f")), None) is False


# --- on_message: dopisywanie ---


def test_broadcast_drawing_lands_in_shared_mission(shared_app, controller):
    deliver(controller, make_xml("shape-1", "u-d-f"))

    assert mission_uids(shared_app) == {"shape-1": SHARED}
    assert mission_changes(shared_app) == [("ADD_CONTENT", "shape-1", SHARED)]
    [(routing_key, cot)] = change_cots(controller)
    assert routing_key == f"missions.{SHARED}"
    assert cot.find("MissionChange").find("type").text == "ADD_CONTENT"
    assert cot.find("MissionChange").find("contentUid").text == "shape-1"
    # ATAK zdejmuje oczekującą publikację tylko przy creatorUid równym własnemu uid
    assert cot.find("MissionChange").find("creatorUid").text == SENDER_UID
    assert cot.find("mission").attrs["authorUid"] == SENDER_UID
    # Obiekt nadal idzie do grup nadawcy jak dotąd
    assert [p["exchange"] for p in controller.rabbit_channel.published].count("groups") == 1


def test_private_drawing_stays_out(shared_app, controller):
    deliver(controller, make_xml("shape-1", "u-d-f", '<marti><dest uid="X"/></marti>'))

    assert mission_uids(shared_app) == {}
    assert mission_changes(shared_app) == []
    assert mission_publications(controller) == []


def test_own_position_stays_out_but_other_unit_goes_in(shared_app, controller):
    deliver(controller, make_xml(SENDER_UID, "a-f-G-U-C"))
    assert mission_uids(shared_app) == {}

    deliver(controller, make_xml("enemy-1", "a-h-G"))
    assert mission_uids(shared_app) == {"enemy-1": SHARED}


@pytest.mark.parametrize(
    "event_uid,event_type,detail",
    [
        ("msg-1", "b-t-f", '<__chat id="All Chat Rooms" chatroom="All Chat Rooms"/>'),
        ("enemy-1", "a-h-G", '<__chat id="All Chat Rooms" chatroom="All Chat Rooms"/>'),
        ("ping-1", "t-x-c-t", ""),
        ("snap-1", "y-taklab-map", ""),
    ],
)
def test_chat_and_technical_events_stay_out(shared_app, controller, event_uid, event_type, detail):
    deliver(controller, make_xml(event_uid, event_type, detail))

    assert mission_uids(shared_app) == {}
    assert mission_changes(shared_app) == []


def test_disabled_feature_changes_nothing(shared_app, controller):
    shared_app.config["OTS_SHARED_MISSION"] = ""

    deliver(controller, make_xml("shape-1", "u-d-f"))

    assert mission_uids(shared_app) == {}
    assert mission_publications(controller) == []


def test_missing_mission_is_logged_without_exception(shared_app, controller):
    shared_app.config["OTS_SHARED_MISSION"] = "Nie ma takiej"

    deliver(controller, make_xml("shape-1", "u-d-f"))

    assert mission_uids(shared_app) == {}
    controller.logger.error.assert_called()
    assert "Nie ma takiej" in str(controller.logger.error.call_args)
    # Zdarzenie i tak poszło do grup
    assert [p["exchange"] for p in controller.rabbit_channel.published] == ["groups"]


def test_default_config_is_disabled():
    from opentakserver.defaultconfig import DefaultConfig

    assert DefaultConfig.OTS_SHARED_MISSION == ""


def published_change_details(controller, event_uid):
    [(_, cot)] = change_cots(controller)
    assert cot.find("MissionChange").find("contentUid").text == event_uid
    return cot.find("MissionChange").find("details")


def raw_xml(event_uid, event_type, inner):
    return (
        f'<event version="2.0" uid="{event_uid}" type="{event_type}" how="h-g-i-g-o" '
        'time="2026-10-16T10:00:00Z" start="2026-10-16T10:00:00Z" '
        f'stale="2026-10-17T10:00:00Z">{inner}</event>'
    )


def test_contact_without_callsign_still_publishes_change(shared_app, controller):
    deliver(
        controller,
        raw_xml(
            "shape-1",
            "u-d-f",
            '<point lat="52.1" lon="21.0" hae="0" ce="1" le="1"/><detail><contact/></detail>',
        ),
    )

    details = published_change_details(controller, "shape-1")
    assert "callsign" not in details.attrs
    assert details.find("location").attrs == {"lat": "52.1", "lon": "21.0"}


def test_usericon_with_camel_case_attribute_still_publishes_change(shared_app, controller):
    deliver(
        controller,
        raw_xml(
            "point-1",
            "b-m-p-s-m",
            '<point lat="52.1" lon="21.0" hae="0" ce="1" le="1"/>'
            '<detail><contact callsign="P"/><usericon iconsetPath="set/icon.png"/></detail>',
        ),
    )

    details = published_change_details(controller, "point-1")
    assert details.attrs["iconsetPath"] == "set/icon.png"


def test_event_without_point_still_publishes_change(shared_app, controller):
    deliver(controller, raw_xml("shape-1", "u-d-f", '<detail><contact callsign="S"/></detail>'))

    details = published_change_details(controller, "shape-1")
    assert details.attrs["callsign"] == "S"
    assert details.find("location") is None


# --- on_message: usuwanie (t-x-d-d) ---


def delete_xml(target_uid, forcedelete=True):
    detail = f'<link uid="{target_uid}" relation="none" type="u-d-f"/>'
    if forcedelete:
        detail += "<__forcedelete/>"
    return make_xml("delete-event-1", "t-x-d-d", detail)


def test_forced_delete_removes_object_from_shared_mission(shared_app, controller):
    deliver(controller, make_xml("shape-1", "u-d-f"))
    deliver(controller, make_xml("shape-2", "u-d-f"))
    controller.rabbit_channel.published.clear()

    deliver(controller, delete_xml("shape-1"))

    assert mission_uids(shared_app) == {"shape-1": None, "shape-2": SHARED}
    assert mission_changes(shared_app)[-1] == ("REMOVE_CONTENT", "shape-1", SHARED)
    [(routing_key, cot)] = change_cots(controller)
    assert routing_key == f"missions.{SHARED}"
    assert cot.find("mission").attrs["type"] == "REMOVE_CONTENT"
    assert cot.find("MissionChange").find("contentUid").text == "shape-1"
    assert cot.find("MissionChange").find("creatorUid").text == SENDER_UID


def test_forced_delete_removes_object_from_another_mission(shared_app, controller):
    xml = make_xml("point-9", "b-m-p-s-m", f'<marti><dest mission="{OTHER}"/></marti>')
    deliver(controller, xml)
    assert mission_uids(shared_app) == {"point-9": OTHER}
    controller.rabbit_channel.published.clear()

    deliver(controller, delete_xml("point-9"))

    assert mission_uids(shared_app) == {"point-9": None}
    assert mission_changes(shared_app)[-1] == ("REMOVE_CONTENT", "point-9", OTHER)
    [(routing_key, _)] = change_cots(controller)
    assert routing_key == f"missions.{OTHER}"


def test_delete_without_forcedelete_does_nothing(shared_app, controller):
    deliver(controller, make_xml("shape-1", "u-d-f"))
    controller.rabbit_channel.published.clear()

    deliver(controller, delete_xml("shape-1", forcedelete=False))

    assert mission_uids(shared_app) == {"shape-1": SHARED}
    assert [c[0] for c in mission_changes(shared_app)] == ["ADD_CONTENT"]
    assert change_cots(controller) == []


def test_forced_delete_of_unknown_uid_does_nothing(shared_app, controller):
    deliver(controller, delete_xml("nobody"))

    assert mission_changes(shared_app) == []
    assert change_cots(controller) == []


def test_forced_delete_when_feature_disabled_does_nothing(shared_app, controller):
    deliver(controller, make_xml("shape-1", "u-d-f"))
    shared_app.config["OTS_SHARED_MISSION"] = ""
    controller.rabbit_channel.published.clear()

    deliver(controller, delete_xml("shape-1"))

    assert mission_uids(shared_app) == {"shape-1": SHARED}
