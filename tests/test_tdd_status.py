"""is_self_delete tells a self logout t-x-d-d apart from a map object delete.

ATAK sends the same event type, t-x-d-d, both when the user's own EUD logs
out and when the user deletes a map object (marker, route, ...). Only the
first case should mark the sender's EUD as Disconnected; the object-delete
case must leave it alone. Pure bs4, no Flask app / database needed.
"""

from bs4 import BeautifulSoup

from opentakserver.cot_parser.tdd import is_self_delete

SENDER_UID = "ANDROID-sender"


def make_event(detail=""):
    xml = (
        '<event version="2.0" uid="event-1" type="t-x-d-d" how="h-g-i-g-o" '
        'time="2026-09-26T16:40:00Z" start="2026-09-26T16:40:00Z" '
        'stale="2026-09-26T16:45:00Z">'
        f"<detail>{detail}</detail></event>"
    )
    return BeautifulSoup(xml, "xml").find("event")


def test_link_to_sender_uid_is_a_self_delete():
    event = make_event(f'<link uid="{SENDER_UID}" type="a-f-G-U-C"/>')

    assert is_self_delete(event, SENDER_UID) is True


def test_link_to_another_object_is_not_a_self_delete():
    event = make_event('<link uid="other-object-uid" type="b-m-p"/>')

    assert is_self_delete(event, SENDER_UID) is False


def test_no_link_is_not_a_self_delete():
    event = make_event("<remarks>no link here</remarks>")

    assert is_self_delete(event, SENDER_UID) is False


def test_no_detail_is_not_a_self_delete():
    xml = (
        '<event version="2.0" uid="event-1" type="t-x-d-d" how="h-g-i-g-o" '
        'time="2026-09-26T16:40:00Z" start="2026-09-26T16:40:00Z" '
        'stale="2026-09-26T16:45:00Z"/>'
    )
    event = BeautifulSoup(xml, "xml").find("event")

    assert is_self_delete(event, SENDER_UID) is False


def test_link_without_uid_is_not_a_self_delete():
    event = make_event('<link type="b-m-p"/>')

    assert is_self_delete(event, SENDER_UID) is False


def test_only_the_first_link_is_checked():
    event = make_event(f'<link uid="other-object-uid"/><link uid="{SENDER_UID}"/>')

    assert is_self_delete(event, SENDER_UID) is False
