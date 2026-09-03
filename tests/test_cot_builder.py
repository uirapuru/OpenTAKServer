import subprocess
import sys
from datetime import datetime, timezone
from xml.etree import ElementTree as ET

import pytest

from opentakserver.cot_builder import SUPPORTED_TYPES, build_event, is_marker_type

MOMENT = datetime(2026, 9, 3, 12, 0, 0, tzinfo=timezone.utc)


def build(cot_type="a-h-G", **kwargs):
    arguments = {
        "cot_type": cot_type,
        "uid": "8f14e45f-ceea-467a-9575-9b0e34f4f0ad",
        "callsign": "SZTAB",
        "latitude": 52.1,
        "longitude": 21.0,
        "timestamp": MOMENT,
    }
    arguments.update(kwargs)
    return build_event(**arguments)


def test_supported_types_cover_the_whitelist():
    assert SUPPORTED_TYPES["a-h-G"].kind == "marker"
    assert SUPPORTED_TYPES["b-a-o-tbl"].kind == "alert"
    assert SUPPORTED_TYPES["b-t-f"].kind == "chat"


def test_only_atom_types_go_to_the_marker_table():
    assert is_marker_type("a-h-G") is True
    assert is_marker_type("b-m-p-w") is False
    assert is_marker_type("b-a-o-tbl") is False


def test_event_carries_type_uid_and_point():
    event = build()
    assert event.get("type") == "a-h-G"
    assert event.get("uid") == "8f14e45f-ceea-467a-9575-9b0e34f4f0ad"
    point = event.find("point")
    assert point.get("lat") == "52.1"
    assert point.get("lon") == "21.0"


def test_stale_follows_the_requested_lifetime():
    event = build(stale_seconds=60)
    assert event.get("time") == "2026-09-03T12:00:00.0000Z"
    assert event.get("stale") == "2026-09-03T12:01:00.0000Z"


def test_callsign_lands_in_the_contact_element():
    event = build()
    assert event.find("detail/contact").get("callsign") == "SZTAB"


def test_remarks_are_added_only_when_given():
    assert build().find("detail/remarks") is None
    assert build(remarks="patrol").find("detail/remarks").text == "patrol"


def test_chat_detail_becomes_a_geochat_element():
    event = build("b-t-f", detail={"message": "zbiorka"})
    chat = event.find("detail/__chat")
    assert chat.get("senderCallsign") == "SZTAB"
    assert event.find("detail/remarks").text == "zbiorka"


def test_unknown_type_is_rejected():
    with pytest.raises(ValueError):
        build("a-x-Q")


def test_event_serialises_to_wellformed_xml():
    ET.fromstring(ET.tostring(build()))


def test_cot_builder_is_pure_no_heavy_imports():
    """Verify that importing cot_builder does not pull in flask or pika.

    The whole point of the pure layer is that tests can run without a full
    OpenTAKServer installation. This test fails if someone reintroduces a
    heavy import at module scope.
    """
    # Import in a subprocess with a clean sys.modules to ensure no hidden
    # import chains from the current process affect the test.
    code = """
import sys
try:
    import opentakserver.cot_builder
    has_flask = 'flask' in sys.modules
    has_pika = 'pika' in sys.modules
    print(f"flask={has_flask};pika={has_pika}")
except Exception as e:
    print(f"error={e}")
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"Import failed: {result.stderr}"
    output = result.stdout.strip()
    assert output == "flask=False;pika=False", f"Heavy imports detected: {output}"


ALERT_LABELS = {
    "b-a-o-tbl": "911 Alert",
    "b-a-o-opn": "In Contact",
    "b-a-o-pan": "Ring The Bell",
    "b-a-g": "Geo-fence Breached",
}


@pytest.mark.parametrize("cot_type,label", sorted(ALERT_LABELS.items()))
def test_alert_carries_the_emergency_element(cot_type, label):
    """cot_parser.parse_alert only creates an Alert row when <emergency type=...> is present."""
    event = build(cot_type)
    emergency = event.find("detail/emergency")
    assert emergency is not None
    assert emergency.get("type") == label
    assert emergency.text == "SZTAB"


def test_cancel_alert_carries_the_cancel_attribute():
    """b-a-o-can takes cot_parser.parse_alert's cancel branch, not the type branch."""
    event = build("b-a-o-can")
    emergency = event.find("detail/emergency")
    assert emergency is not None
    assert emergency.get("cancel") == "true"
    assert emergency.get("type") is None


def test_alert_labels_are_exposed_for_every_alert_type():
    for entry in SUPPORTED_TYPES.values():
        if entry.kind == "alert":
            assert entry.alert_label


def test_chat_carries_chatgrp_and_timed_remarks():
    """cot_parser.parse_geochat reads chatgrp["uid0"] and remarks["time"]."""
    event = build("b-t-f", detail={"message": "zbiorka"})
    chat_group = event.find("detail/chatgrp")
    assert chat_group is not None
    assert chat_group.get("uid0") == "8f14e45f-ceea-467a-9575-9b0e34f4f0ad"
    assert chat_group.get("uid1") == "All Chat Rooms"
    assert chat_group.get("id") == "All Chat Rooms"

    remarks = event.find("detail/remarks")
    assert remarks.get("time") == "2026-09-03T12:00:00.0000Z"
    assert remarks.get("source") == "8f14e45f-ceea-467a-9575-9b0e34f4f0ad"
    assert remarks.get("to") == "All Chat Rooms"


def test_empty_chat_message_is_rejected():
    """An empty message would emit no <remarks>, which parse_geochat drops silently."""
    with pytest.raises(ValueError):
        build("b-t-f", detail={"message": "   "})
    with pytest.raises(ValueError):
        build("b-t-f", detail={})


def _as_parser_sees_it(event):
    """Reproduce how cot_parser turns a published message back into an event.

    cot_parser.on_message does ``BeautifulSoup(body["cot"], "xml").find("event")``
    and hands that object to parse_alert/parse_geochat. This mirrors that shape so
    the test proves the fields those methods read are reachable. The parser class
    itself is deliberately not imported: it drags in the database and the broker.
    """
    from bs4 import BeautifulSoup

    return BeautifulSoup(ET.tostring(event).decode("utf-8"), "xml").find("event")


@pytest.mark.parametrize("cot_type,label", sorted(ALERT_LABELS.items()))
def test_parser_can_reach_the_alert_fields(cot_type, label):
    event = _as_parser_sees_it(build(cot_type))
    assert event.find("emergency").attrs["type"] == label
    assert event.attrs["start"]


def test_parser_can_reach_the_cancel_field():
    event = _as_parser_sees_it(build("b-a-o-can"))
    assert "cancel" in event.find("emergency").attrs


def test_parser_can_reach_the_geochat_fields():
    event = _as_parser_sees_it(build("b-t-f", detail={"message": "zbiorka"}))
    chat = event.find("__chat")
    assert chat.attrs["chatroom"] and chat.attrs["id"]
    assert event.find("chatgrp").attrs["uid0"]
    assert event.find("remarks").attrs["time"]
    assert event.find("remarks").text == "zbiorka"
