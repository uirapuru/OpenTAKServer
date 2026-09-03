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
    assert SUPPORTED_TYPES["a-h-G"] == "marker"
    assert SUPPORTED_TYPES["b-a-o-tbl"] == "alert"
    assert SUPPORTED_TYPES["b-t-f"] == "chat"


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
        cwd="/home/uirapuru/OpenTAKServer-fork/.claude/worktrees/komunikaty-cot",
    )
    assert result.returncode == 0, f"Import failed: {result.stderr}"
    output = result.stdout.strip()
    assert output == "flask=False;pika=False", f"Heavy imports detected: {output}"
