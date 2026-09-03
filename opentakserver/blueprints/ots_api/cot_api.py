"""POST /api/cot - send an arbitrary supported CoT event to the connected clients."""

import traceback
from datetime import datetime, timezone
from uuid import UUID

import bleach
from flask import Blueprint, jsonify, request
from flask_security import auth_required, current_user

from opentakserver.cot_builder import DEFAULT_STALE_SECONDS, SUPPORTED_TYPES, build_event
from opentakserver.extensions import logger

cot_send_api_blueprint = Blueprint("cot_send_api", __name__)

UNKNOWN_ERROR_VALUE = 9999999.0


def _error(message):
    return jsonify({"success": False, "error": message}), 400


def _read_float(body, key, default=None):
    if key not in body:
        return default
    return float(body[key])


@cot_send_api_blueprint.route("/api/cot", methods=["POST"])
@auth_required()
def send_cot():
    body = request.json or {}

    cot_type = body.get("type")
    if cot_type not in SUPPORTED_TYPES:
        return _error(f"Unsupported CoT type: {cot_type}")

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

    try:
        event = build_event(
            cot_type=cot_type,
            uid=body["uid"],
            callsign=callsign,
            latitude=latitude,
            longitude=longitude,
            timestamp=datetime.now(timezone.utc),
            stale_seconds=int(body.get("stale_seconds", DEFAULT_STALE_SECONDS)),
            hae=_read_float(body, "hae", UNKNOWN_ERROR_VALUE),
            ce=_read_float(body, "ce", UNKNOWN_ERROR_VALUE),
            le=_read_float(body, "le", UNKNOWN_ERROR_VALUE),
            remarks=remarks,
            detail=body.get("detail"),
        )
    except (ValueError, TypeError) as error:
        logger.error(f"Failed to build CoT: {error}")
        logger.error(traceback.format_exc())
        return _error(f"Failed to build CoT: {error}")

    return jsonify({"success": True, "uid": event.get("uid")}), 201
