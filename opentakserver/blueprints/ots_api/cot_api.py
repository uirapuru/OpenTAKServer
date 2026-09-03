"""POST /api/cot - send an arbitrary supported CoT event to the connected clients."""

import json
import traceback
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4
from xml.etree import ElementTree as ET

import bleach
import pika
from flask import Blueprint
from flask import current_app as app
from flask import jsonify, request
from flask_security import auth_required, current_user
from sqlalchemy import insert, update
from sqlalchemy.exc import IntegrityError

from opentakserver.blueprints.ots_api.api import route_cot
from opentakserver.cot_builder import (
    DEFAULT_STALE_SECONDS,
    SUPPORTED_TYPES,
    build_event,
    is_marker_type,
)
from opentakserver.extensions import db, logger
from opentakserver.functions import cot_type_to_2525c, get_affiliation, get_battle_dimension
from opentakserver.models.CoT import CoT
from opentakserver.models.Marker import Marker
from opentakserver.models.Point import Point

cot_send_api_blueprint = Blueprint("cot_send_api", __name__)

UNKNOWN_ERROR_VALUE = 9999999.0

# Thirty days. Anything longer is a mistake rather than a lifetime anyone wants,
# and a large enough value overflows timedelta instead of building an event.
MAX_STALE_SECONDS = 2592000


def _error(message):
    return jsonify({"success": False, "error": message}), 400


def _read_float(body, key, default=None):
    if key not in body:
        return default
    return float(body[key])


@cot_send_api_blueprint.route("/api/cot", methods=["POST"])
@auth_required()
def send_cot():
    """Send an arbitrary supported CoT event to the connected clients.

    :param type: The CoT type to send, i.e. ``a-h-G``. Must be one of ``SUPPORTED_TYPES``
    :param uid: The UID of the event, must be a UUID4 string
    :param latitude: The latitude of the event, between -90 and 90
    :param longitude: The longitude of the event, between -180 and 180
    :param callsign: Optional callsign, defaults to the logged in user's username
    :param remarks: Optional free-text remarks
    :param detail: Optional dict of extra detail fields, i.e. ``{"message": "..."}`` for chat events
    :param stale_seconds: Optional number of seconds until the event goes stale, defaults to
        ``DEFAULT_STALE_SECONDS``
    :param hae, ce, le: Optional height/circular/linear error values
    """
    body = request.json or {}

    cot_type = body.get("type")
    if cot_type not in SUPPORTED_TYPES:
        return _error(f"Unsupported CoT type: {cot_type}")

    if "detail" in body and body["detail"] is not None and not isinstance(body["detail"], dict):
        return _error("detail must be an object")

    try:
        UUID(str(body.get("uid")), version=4)
    except (ValueError, TypeError):
        return _error("Invalid UID. UIDs need to be in UUID4 format")

    try:
        latitude = float(body["latitude"])
        longitude = float(body["longitude"])
    except (KeyError, TypeError, ValueError):
        return _error("Please provide a numeric latitude and longitude")

    if not -90 <= latitude <= 90:
        return _error(f"Invalid latitude: {latitude}")
    if not -180 <= longitude <= 180:
        return _error(f"Invalid longitude: {longitude}")

    callsign = bleach.clean(str(body.get("callsign") or current_user.username))
    remarks = bleach.clean(str(body["remarks"])) if body.get("remarks") else None

    timestamp = datetime.now(timezone.utc)

    # The numeric coercions belong inside the guard: a careless value has to come
    # back as the documented 400, not as an uncaught exception and a 500 page.
    # OverflowError is here because a huge stale_seconds overflows timedelta.
    try:
        stale_seconds = int(body.get("stale_seconds", DEFAULT_STALE_SECONDS))
        ce = _read_float(body, "ce", UNKNOWN_ERROR_VALUE)
        hae = _read_float(body, "hae", UNKNOWN_ERROR_VALUE)
        le = _read_float(body, "le", UNKNOWN_ERROR_VALUE)

        if not 0 < stale_seconds <= MAX_STALE_SECONDS:
            return _error(
                f"stale_seconds must be between 1 and {MAX_STALE_SECONDS}: {stale_seconds}"
            )

        event = build_event(
            cot_type=cot_type,
            uid=body["uid"],
            callsign=callsign,
            latitude=latitude,
            longitude=longitude,
            timestamp=timestamp,
            stale_seconds=stale_seconds,
            hae=hae,
            ce=ce,
            le=le,
            remarks=remarks,
            detail=body.get("detail"),
        )
    except (ValueError, TypeError, OverflowError) as error:
        logger.error(f"Failed to build CoT: {error}")
        logger.error(traceback.format_exc())
        return _error(f"Failed to build CoT: {error}")

    stale = timestamp + timedelta(seconds=stale_seconds)

    _persist_event(
        event, cot_type, callsign, timestamp, stale, latitude, longitude, ce, hae, le, body["uid"]
    )
    _publish_event(event)

    return jsonify({"success": True, "uid": event.get("uid")}), 201


def _publish_event(event):
    """Publish a built CoT event to the connected clients.

    Publishes onto the ``cot_parser`` exchange (feeds the record-building
    process) and the ``firehose`` exchange (general subscription stream),
    then hands the event to ``route_cot`` so it reaches the sender's groups.
    """
    xml = ET.tostring(event).decode("utf-8")
    payload = json.dumps({"cot": xml, "uid": app.config["OTS_NODE_ID"]})
    properties = pika.BasicProperties(expiration=app.config.get("OTS_RABBITMQ_TTL"))

    credentials = pika.PlainCredentials(
        app.config.get("OTS_RABBITMQ_USERNAME"), app.config.get("OTS_RABBITMQ_PASSWORD")
    )
    connection = pika.BlockingConnection(
        pika.ConnectionParameters(
            host=app.config.get("OTS_RABBITMQ_SERVER_ADDRESS"), credentials=credentials
        )
    )
    channel = connection.channel()
    channel.basic_publish(
        exchange="cot_parser", routing_key="cot_parser", body=payload, properties=properties
    )
    channel.basic_publish(exchange="firehose", routing_key="", body=payload, properties=properties)
    channel.close()
    connection.close()

    route_cot(xml, current_user)


def _persist_event(
    event, cot_type, callsign, timestamp, stale, latitude, longitude, ce, hae, le, uid
):
    """Write the CoT/Point rows, and a Marker row for atom types, for a built event.

    A repeated ``uid`` for an atom type moves the existing Marker to the new
    point/cot rather than being rejected, matching how /api/markers already
    treats a re-sent marker uid.
    """
    cot_row = db.session.execute(
        insert(CoT).values(
            how="m-g",
            type=cot_type,
            timestamp=timestamp,
            xml=ET.tostring(event),
            start=timestamp,
            stale=stale,
            sender_callsign=current_user.username,
        )
    )
    db.session.commit()
    cot_id = cot_row.inserted_primary_key[0]

    point_row = db.session.execute(
        insert(Point).values(
            uid=str(uuid4()),
            device_uid=None,
            latitude=latitude,
            longitude=longitude,
            ce=ce,
            hae=hae,
            le=le,
            timestamp=timestamp,
            location_source="",
            course=0,
            speed=0,
            cot_id=cot_id,
        )
    )
    db.session.commit()

    if is_marker_type(cot_type):
        marker = Marker()
        marker.uid = uid
        marker.callsign = callsign
        marker.affiliation = get_affiliation(cot_type)
        marker.battle_dimension = get_battle_dimension(cot_type)
        marker.mil_std_2525c = cot_type_to_2525c(cot_type)
        marker.cot_id = cot_id
        marker.point_id = point_row.inserted_primary_key[0]
        try:
            db.session.add(marker)
            db.session.commit()
        except IntegrityError:
            # A marker with this uid already exists - resending the same uid
            # moves it to the new point/cot rather than being rejected, matching
            # how /api/markers already treats a re-sent marker uid.
            db.session.rollback()
            db.session.execute(
                update(Marker)
                .where(Marker.uid == marker.uid)
                .values(point_id=marker.point_id, cot_id=marker.cot_id, **marker.serialize())
            )
            db.session.commit()
