"""Pure helpers that turn a message description into a CoT event.

No Flask, no database, no broker. Everything here is unit testable on its own.
"""

from datetime import datetime, timedelta
from xml.etree import ElementTree as ET

from opentakserver.config_helpers import iso8601_string_from_datetime

DEFAULT_STALE_SECONDS = 86400
UNKNOWN_ERROR_VALUE = 9999999.0

SUPPORTED_TYPES = {
    "a-h-G": "marker",
    "a-f-G": "marker",
    "a-u-G": "marker",
    "a-n-G": "marker",
    "a-h-G-E-V": "marker",
    "a-h-A": "marker",
    "a-u-A-M-F-Q-r": "marker",
    "a-h-G-I": "marker",
    "b-m-p-s-p-i": "marker",
    "b-m-p-w": "marker",
    "b-a-o-tbl": "alert",
    "b-a-o-opn": "alert",
    "b-a-o-pan": "alert",
    "b-a-o-can": "alert",
    "b-a-g": "alert",
    "b-t-f": "chat",
}


def is_marker_type(cot_type):
    """Only atom types (a-*) are stored in the Marker table."""
    return cot_type.startswith("a-")


def build_event(
    cot_type,
    uid,
    callsign,
    latitude,
    longitude,
    timestamp,
    stale_seconds=DEFAULT_STALE_SECONDS,
    hae=UNKNOWN_ERROR_VALUE,
    ce=UNKNOWN_ERROR_VALUE,
    le=UNKNOWN_ERROR_VALUE,
    remarks=None,
    detail=None,
):
    if cot_type not in SUPPORTED_TYPES:
        raise ValueError(f"Unsupported CoT type: {cot_type}")

    stale = timestamp + timedelta(seconds=stale_seconds)

    event = ET.Element("event")
    event.set("version", "2.0")
    event.set("type", cot_type)
    event.set("uid", uid)
    event.set("how", "m-g")
    event.set("time", iso8601_string_from_datetime(timestamp))
    event.set("start", iso8601_string_from_datetime(timestamp))
    event.set("stale", iso8601_string_from_datetime(stale))

    point = ET.SubElement(event, "point")
    point.set("lat", str(latitude))
    point.set("lon", str(longitude))
    point.set("hae", str(hae))
    point.set("ce", str(ce))
    point.set("le", str(le))

    detail_element = ET.SubElement(event, "detail")
    detail_element.set("uid", uid)

    contact = ET.SubElement(detail_element, "contact")
    contact.set("callsign", callsign)

    text = remarks
    if SUPPORTED_TYPES[cot_type] == "chat":
        message = (detail or {}).get("message", "")
        chat = ET.SubElement(detail_element, "__chat")
        chat.set("senderCallsign", callsign)
        chat.set("id", uid)
        chat.set("chatroom", "All Chat Rooms")
        text = message

    if text:
        ET.SubElement(detail_element, "remarks").text = text

    return event
