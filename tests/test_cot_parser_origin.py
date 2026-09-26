"""route_cot stamps direct messages with the sender's account name.

The stamp is ``<_taklab_origin user="..."/>`` inside ``<detail>``. A client
cannot forge it: every ``_taklab_origin`` the client wrote is removed before
the event goes out, on every exchange.
"""

import json
from unittest.mock import MagicMock

import pytest
from bs4 import BeautifulSoup
from flask_security import hash_password

SENDER_UID = "ANDROID-sender"


def make_event(detail="", event_type="b-t-f", event_uid="event-1"):
    xml = (
        f'<event version="2.0" uid="{event_uid}" type="{event_type}" how="h-g-i-g-o" '
        'time="2026-09-26T16:40:00Z" start="2026-09-26T16:40:00Z" '
        'stale="2026-09-26T16:45:00Z">'
        '<point lat="9999999.0" lon="9999999.0" hae="0" ce="9999999.0" le="9999999.0"/>'
        f"<detail>{detail}</detail></event>"
    )
    return xml


def parse(xml):
    return BeautifulSoup(xml, "xml").find("event")


@pytest.fixture
def controller(app):
    from opentakserver.cot_parser.cot_parser import CoTController
    from opentakserver.extensions import db

    parser = CoTController(app.app_context(), MagicMock(), db, MagicMock())
    parser.rabbit_channel = MagicMock()
    return parser


@pytest.fixture
def user_id(app):
    from opentakserver.extensions import db

    with app.app_context():
        user = app.security.datastore.create_user(
            username="kaszub", password=hash_password("secret")
        )
        db.session.commit()
        return user.id


def published(parser, exchange):
    bodies = []
    for call in parser.rabbit_channel.basic_publish.call_args_list:
        if call.kwargs["exchange"] == exchange:
            bodies.append((call.kwargs["routing_key"], json.loads(call.kwargs["body"])))
    return bodies


def origins(cot):
    return BeautifulSoup(cot, "xml").find_all("_taklab_origin")


def test_dm_by_uid_is_stamped_with_the_sender_account(controller, user_id):
    event = parse(make_event('<marti><dest uid="bot-uid"/></marti>'))

    controller.route_cot(event, SENDER_UID, user_id)

    [(routing_key, body)] = published(controller, "dms")
    assert routing_key == "bot-uid"
    [origin] = origins(body["cot"])
    assert origin.attrs == {"user": "kaszub"}
    assert origin.parent.name == "detail"


def test_dm_by_callsign_is_stamped_with_the_sender_account(controller, user_id):
    event = parse(make_event('<marti><dest callsign="Hal9000"/></marti>'))

    controller.route_cot(event, SENDER_UID, user_id)

    [(routing_key, body)] = published(controller, "dms")
    assert routing_key == "Hal9000"
    [origin] = origins(body["cot"])
    assert origin.attrs == {"user": "kaszub"}


def test_forged_origin_is_replaced(controller, user_id):
    event = parse(
        make_event(
            '<_taklab_origin user="victim"/><_taklab_origin user="other"/>'
            '<marti><dest uid="bot-uid"/></marti>'
        )
    )

    controller.route_cot(event, SENDER_UID, user_id)

    [(_, body)] = published(controller, "dms")
    [origin] = origins(body["cot"])
    assert origin.attrs == {"user": "kaszub"}


def test_nested_forged_origin_is_removed(controller, user_id):
    event = parse(
        make_event(
            '<taklab_map v="1"><_taklab_origin user="victim"/></taklab_map>'
            '<marti><dest uid="bot-uid"/></marti>'
        )
    )

    controller.route_cot(event, SENDER_UID, user_id)

    [(_, body)] = published(controller, "dms")
    [origin] = origins(body["cot"])
    assert origin.attrs == {"user": "kaszub"}
    assert origin.parent.name == "detail"


def test_unknown_user_is_stamped_empty(controller):
    event = parse(make_event('<_taklab_origin user="victim"/><marti><dest uid="bot-uid"/></marti>'))

    controller.route_cot(event, SENDER_UID, None)

    [(_, body)] = published(controller, "dms")
    [origin] = origins(body["cot"])
    assert origin.attrs == {"user": ""}


def test_user_id_without_account_is_stamped_empty(controller):
    event = parse(make_event('<marti><dest uid="bot-uid"/></marti>'))

    controller.route_cot(event, SENDER_UID, 424242)

    [(_, body)] = published(controller, "dms")
    [origin] = origins(body["cot"])
    assert origin.attrs == {"user": ""}


def test_every_destination_gets_a_single_stamp(controller, user_id):
    event = parse(make_event('<marti><dest uid="bot-uid"/><dest callsign="Hal9000"/></marti>'))

    controller.route_cot(event, SENDER_UID, user_id)

    bodies = published(controller, "dms")
    assert [key for key, _ in bodies] == ["bot-uid", "Hal9000"]
    for _, body in bodies:
        assert len(origins(body["cot"])) == 1


def test_group_broadcast_strips_forged_origin_without_stamping(controller, user_id):
    event = parse(make_event('<contact callsign="A"/><_taklab_origin user="victim"/>'))

    controller.route_cot(event, SENDER_UID, user_id)

    [(routing_key, body)] = published(controller, "groups")
    assert routing_key == "__ANON__.OUT"
    assert origins(body["cot"]) == []
    assert BeautifulSoup(body["cot"], "xml").find("contact").attrs == {"callsign": "A"}


def test_anonymous_broadcast_strips_forged_origin(controller):
    event = parse(make_event('<a><_taklab_origin user="victim"/></a>'))

    controller.route_cot(event, SENDER_UID, None)

    [(_, body)] = published(controller, "groups")
    assert origins(body["cot"]) == []


def test_ordinary_event_is_unchanged(controller, user_id):
    xml = make_event('<contact callsign="A"/><remarks>hello</remarks>')
    event = parse(xml)

    controller.route_cot(event, SENDER_UID, user_id)

    [(_, body)] = published(controller, "groups")
    assert body["cot"] == str(parse(xml))


def deliver(controller, xml, user_id):
    body = json.dumps({"uid": SENDER_UID, "cot": xml, "user_id": user_id}).encode()
    controller.on_message(controller.rabbit_channel, MagicMock(), None, body)


@pytest.fixture
def sender_eud(app):
    from opentakserver.extensions import db
    from opentakserver.models.EUD import EUD

    with app.app_context():
        eud = EUD()
        eud.uid = SENDER_UID
        eud.callsign = "sender"
        db.session.add(eud)
        db.session.commit()


def stored_types(app):
    from opentakserver.extensions import db
    from opentakserver.models.CoT import CoT

    with app.app_context():
        return [row.type for row in db.session.execute(db.select(CoT)).scalars()]


def test_map_snapshot_is_routed_but_not_stored(app, controller, user_id, sender_eud):
    xml = make_event(
        '<taklab_map v="1">{"v":1}</taklab_map><marti><dest uid="bot-uid"/></marti>',
        event_type="y-taklab-map",
        event_uid=f"{SENDER_UID}.taklab-map.1",
    )

    deliver(controller, xml, user_id)

    assert stored_types(app) == []
    [(routing_key, body)] = published(controller, "dms")
    assert routing_key == "bot-uid"
    assert "taklab_map" in body["cot"]
    assert origins(body["cot"])[0].attrs == {"user": "kaszub"}


def test_ordinary_dm_is_still_stored(app, controller, user_id, sender_eud):
    xml = make_event('<marti><dest uid="bot-uid"/></marti>')

    deliver(controller, xml, user_id)

    assert stored_types(app) == ["b-t-f"]
    assert len(published(controller, "dms")) == 1


def test_forged_origin_is_stripped_before_storing(app, controller, user_id, sender_eud):
    from opentakserver.extensions import db
    from opentakserver.models.CoT import CoT

    xml = make_event('<_taklab_origin user="victim"/><marti><dest uid="bot-uid"/></marti>')

    deliver(controller, xml, user_id)

    with app.app_context():
        [stored] = db.session.execute(db.select(CoT)).scalars()
        assert origins(stored.xml) == []
