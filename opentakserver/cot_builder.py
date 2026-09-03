"""Pure helpers that turn a message description into a CoT event.

No Flask, no database, no broker. Everything here is unit testable on its own.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from xml.etree import ElementTree as ET

from opentakserver.config_helpers import iso8601_string_from_datetime

DEFAULT_STALE_SECONDS = 86400
UNKNOWN_ERROR_VALUE = 9999999.0

# The chat room every message from the operator panel goes to. ATAK broadcasts
# to this room by name, and cot_parser stores it as the Chatroom row.
ALL_CHAT_ROOMS = "All Chat Rooms"


@dataclass(frozen=True)
class CotTypeSpec:
    """How one supported CoT type is built.

    ``kind`` drives the shape of the <detail> block. ``alert_label`` is the
    human readable emergency name ATAK puts in <emergency type="...">; it is
    only set for alert types. The labels below were read out of the decompiled
    ATAK 5.7 client, not out of published CoT documentation, which disagrees
    about which label belongs to b-a-o-tbl and which to b-a-o-opn.
    """

    kind: str
    alert_label: str | None = None


SUPPORTED_TYPES = {
    "a-h-G": CotTypeSpec("marker"),
    "a-f-G": CotTypeSpec("marker"),
    "a-u-G": CotTypeSpec("marker"),
    "a-n-G": CotTypeSpec("marker"),
    "a-h-G-E-V": CotTypeSpec("marker"),
    "a-h-A": CotTypeSpec("marker"),
    "a-u-A-M-F-Q-r": CotTypeSpec("marker"),
    "a-h-G-I": CotTypeSpec("marker"),
    "b-m-p-s-p-i": CotTypeSpec("marker"),
    "b-m-p-w": CotTypeSpec("marker"),
    "b-a-o-tbl": CotTypeSpec("alert", "911 Alert"),
    "b-a-o-opn": CotTypeSpec("alert", "In Contact"),
    "b-a-o-pan": CotTypeSpec("alert", "Ring The Bell"),
    "b-a-o-can": CotTypeSpec("alert", "Cancel Alert"),
    "b-a-g": CotTypeSpec("alert", "Geo-fence Breached"),
    "b-t-f": CotTypeSpec("chat"),
}

# b-a-o-can retracts the sender's last open alert. cot_parser.parse_alert takes
# that branch on the presence of a "cancel" attribute, never on a type label.
CANCEL_ALERT_TYPE = "b-a-o-can"


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

    spec = SUPPORTED_TYPES[cot_type]
    text = remarks
    remarks_attributes = {}

    if spec.kind == "alert":
        # cot_parser.parse_alert ignores an event without <emergency>, so every
        # alert type has to carry one. The callsign is the element's text
        # because that is where ATAK puts the name of whoever raised the alert.
        emergency = ET.SubElement(detail_element, "emergency")
        if cot_type == CANCEL_ALERT_TYPE:
            emergency.set("cancel", "true")
        else:
            emergency.set("type", spec.alert_label)
        emergency.text = callsign

    if spec.kind == "chat":
        message = str((detail or {}).get("message", "") or "").strip()
        if not message:
            # An empty message would emit no <remarks>, and cot_parser.parse_geochat
            # returns early without one, so the message would be dropped in silence
            # while the caller was told it was sent.
            raise ValueError("A chat event needs a non-empty detail.message")

        chat = ET.SubElement(detail_element, "__chat")
        chat.set("senderCallsign", callsign)
        chat.set("id", uid)
        chat.set("chatroom", ALL_CHAT_ROOMS)

        # cot_parser.parse_geochat reads chatgrp["uid0"] as the sender uid and
        # raises AttributeError without a <chatgrp>, which abandons the whole
        # message. The panel has no separate device uid for itself, so the
        # event's own uid is the sender uid.
        chat_group = ET.SubElement(detail_element, "chatgrp")
        chat_group.set("uid0", uid)
        chat_group.set("uid1", ALL_CHAT_ROOMS)
        chat_group.set("id", ALL_CHAT_ROOMS)

        text = message
        # parse_geochat reads remarks["time"] for the GeoChat timestamp.
        remarks_attributes = {
            "time": iso8601_string_from_datetime(timestamp),
            "source": uid,
            "to": ALL_CHAT_ROOMS,
        }

    if text:
        remarks_element = ET.SubElement(detail_element, "remarks", remarks_attributes)
        remarks_element.text = text

    return event
