"""taklab: /api/taklab/vision - video analysis on demand (administrator only).

Who is asking: the same viewer as the recordings registry (Flask-Security
session or token, or a client certificate forwarded by marti-tls). Every route
except the signed file route is for the administrator role only; other accounts
get 403, anonymous callers 401, and a server without the vision service
configured answers 404 (/status answers 200 with enabled=false so the menu can
ask without error handling).

Contract: the routes forward to the taklab-vision service (/v1/env/*, see
opentakserver/taklab_vision.py). A service error 4xx passes through with its
status and {"error": message}; an unreachable service is 503.

Signed file route: GET /api/taklab/vision/recordings/<id>/file?expires=&signature=
is called by the vision worker without a session. The HMAC signature binds one
recording and one expiry; it is the only credential. Only ready, not deleted
video recordings are served, with Range support (PyAV seeks over HTTPS).
The token, the signature and signed URLs are never logged.
"""

import time
from datetime import timezone
from urllib.parse import unquote

from flask import Blueprint, Response, request, send_file

from opentakserver import taklab_recordings as rec_core
from opentakserver import taklab_vision as vision
from opentakserver.blueprints.ots_api.taklab_recordings_api import _error, _reply, _viewer
from opentakserver.extensions import db, logger
from opentakserver.models.TaklabRecording import STATE_READY, TaklabRecording

taklab_vision_api = Blueprint("taklab_vision_api", __name__)

MSG_AUTH = "Wymagane logowanie."
MSG_ADMIN = "Analiza wideo jest dostępna tylko dla administratora."
MSG_OFF = "Analiza wideo nie jest włączona na tym serwerze."
MSG_DOWN = "Usługa analizy wideo nie odpowiada."
MSG_NO_RECORDING = "Nie ma takiego nagrania."
MSG_NO_HOST = "Serwer nie ma publicznego adresu, więc nagrań nie da się analizować."
MSG_BAD_BODY = "Wybierz transmisję albo nagranie."
MSG_SIGNATURE = "Podpis adresu jest nieprawidłowy albo wygasł."
MAX_SOURCES = 100
SERVICE_PREFIX = "/v1/env/analyses/"
OTS_PREFIX = "/api/taklab/vision/analyses/"


def _setup():
    cfg = vision.settings()
    if cfg is None:
        return None, None, _error(MSG_OFF, 404)
    viewer = _viewer()
    if viewer is None:
        return None, None, _error(MSG_AUTH, 401)
    if not rec_core.is_admin(viewer):
        return None, None, _error(MSG_ADMIN, 403)
    return cfg, viewer, None


def _service(cfg, method, path, body=None):
    """(response, None) or (None, error reply)."""
    try:
        return vision.call(cfg, method, path, json_body=body), None
    except vision.VisionUnavailable:
        return None, _error(MSG_DOWN, 503)


def _pass_json(response):
    if response.status_code >= 500:
        return _error(MSG_DOWN, 503)
    try:
        data = response.json()
    except ValueError:
        data = None
    if response.status_code >= 400:
        message = data.get("error") if isinstance(data, dict) else None
        return _error(message if isinstance(message, str) and message else MSG_DOWN, response.status_code)
    return _reply(data if data is not None else {}, response.status_code)


def _ready_video(rec_id):
    """(recording, path) of a ready, not deleted video with a file on disk, else (None, None)."""
    config = rec_core.settings()
    if config is None or not rec_id or len(rec_id) > 36:
        return None, None
    rec = db.session.get(TaklabRecording, rec_id)
    if rec is None or rec.deleted or rec.kind != "video" or rec.state != STATE_READY:
        return None, None
    path = rec_core.absolute(config, rec.file_path)
    if path is None or not path.is_file():
        return None, None
    return rec, path


@taklab_vision_api.route("/api/taklab/vision/status", methods=["GET"])
def vision_status():
    cfg = vision.settings()
    if cfg is None:
        return _reply({"enabled": False})
    viewer = _viewer()
    if viewer is None:
        return _error(MSG_AUTH, 401)
    if not rec_core.is_admin(viewer):
        return _error(MSG_ADMIN, 403)
    return _reply({"enabled": True})


@taklab_vision_api.route("/api/taklab/vision/sources", methods=["GET"])
def vision_sources():
    cfg, viewer, refusal = _setup()
    if refusal:
        return refusal
    response, failure = _service(cfg, "GET", "/v1/env/streams")
    if failure:
        return failure
    if response.status_code != 200:
        return _pass_json(response)
    try:
        live = response.json().get("streams", [])
    except (ValueError, AttributeError):
        return _error(MSG_DOWN, 503)

    recordings = []
    config = rec_core.settings()
    if config is not None:
        rows = (
            db.session.query(TaklabRecording)
            .filter(
                TaklabRecording.kind == "video",
                TaklabRecording.state == STATE_READY,
                TaklabRecording.deleted.is_(False),
            )
            .order_by(TaklabRecording.started_at.desc(), TaklabRecording.id)
            .limit(MAX_SOURCES * 3)
            .all()
        )
        for rec in rows:
            path = rec_core.absolute(config, rec.file_path)
            if path is None or not path.is_file():
                continue
            recordings.append(
                {
                    "id": rec.id,
                    "name": rec.name or rec_core.default_name(rec, config),
                    "account": rec.account,
                    "started_at": rec.started_at.replace(tzinfo=timezone.utc).isoformat(),
                    "duration_s": rec.duration_s(),
                }
            )
            if len(recordings) >= MAX_SOURCES:
                break
    return _reply({"live": live, "recordings": recordings})


@taklab_vision_api.route("/api/taklab/vision/analyses", methods=["GET"])
def list_analyses():
    cfg, viewer, refusal = _setup()
    if refusal:
        return refusal
    response, failure = _service(cfg, "GET", "/v1/env/analyses")
    return failure or _pass_json(response)


@taklab_vision_api.route("/api/taklab/vision/analyses", methods=["POST"])
def start_analysis():
    cfg, viewer, refusal = _setup()
    if refusal:
        return refusal
    body = request.get_json(silent=True)
    body = body if isinstance(body, dict) else {}
    source = body.get("source")
    if source == "live":
        path = body.get("path")
        if not isinstance(path, str) or not path.strip():
            return _error(MSG_BAD_BODY, 400)
        payload = {"source": "live", "path": path, "created_by": viewer.username}
    elif source == "recording":
        rec_id = body.get("recording_id")
        rec, _path = _ready_video(rec_id if isinstance(rec_id, str) else None)
        if rec is None:
            return _error(MSG_NO_RECORDING, 404)
        if not cfg.host:
            return _error(MSG_NO_HOST, 409)
        config = rec_core.settings()
        payload = {
            "source": "recording",
            "label": rec.name or rec_core.default_name(rec, config),
            "account": rec.account,
            "started_at": rec.started_at.replace(tzinfo=timezone.utc).timestamp(),
            "duration_s": rec.duration_s(),
            "url": vision.signed_file_url(cfg, rec.id, time.time()),
            "created_by": viewer.username,
        }
    else:
        return _error(MSG_BAD_BODY, 400)
    response, failure = _service(cfg, "POST", "/v1/env/analyses", payload)
    if failure:
        return failure
    if response.status_code < 400:
        try:
            number = response.json().get("id")
        except (ValueError, AttributeError):
            number = None
        logger.info(f"taklab vision: {viewer.username} started analysis {number}")
    return _pass_json(response)


@taklab_vision_api.route("/api/taklab/vision/analyses/<int:analysis_id>", methods=["GET"])
def one_analysis(analysis_id):
    cfg, viewer, refusal = _setup()
    if refusal:
        return refusal
    response, failure = _service(cfg, "GET", f"/v1/env/analyses/{analysis_id}")
    return failure or _pass_json(response)


@taklab_vision_api.route("/api/taklab/vision/analyses/<int:analysis_id>/stop", methods=["POST"])
def stop_analysis(analysis_id):
    cfg, viewer, refusal = _setup()
    if refusal:
        return refusal
    response, failure = _service(cfg, "POST", f"/v1/env/analyses/{analysis_id}/stop")
    if failure:
        return failure
    if response.status_code < 400:
        logger.info(f"taklab vision: {viewer.username} stopped analysis {analysis_id}")
    return _pass_json(response)


@taklab_vision_api.route("/api/taklab/vision/analyses/<int:analysis_id>/faces", methods=["GET"])
def analysis_faces(analysis_id):
    cfg, viewer, refusal = _setup()
    if refusal:
        return refusal
    response, failure = _service(cfg, "GET", f"/v1/env/analyses/{analysis_id}/faces")
    if failure:
        return failure
    if response.status_code != 200:
        return _pass_json(response)
    try:
        data = response.json()
        for item in data.get("items", []):
            url = item.get("thumb_url")
            if isinstance(url, str) and url.startswith(SERVICE_PREFIX):
                item["thumb_url"] = OTS_PREFIX + url[len(SERVICE_PREFIX):]
    except (ValueError, AttributeError):
        return _error(MSG_DOWN, 503)
    return _reply(data)


@taklab_vision_api.route(
    "/api/taklab/vision/analyses/<int:analysis_id>/persons/<int:person_id>/thumb.jpg", methods=["GET"]
)
def person_thumb(analysis_id, person_id):
    cfg, viewer, refusal = _setup()
    if refusal:
        return refusal
    response, failure = _service(
        cfg, "GET", f"/v1/env/analyses/{analysis_id}/persons/{person_id}/thumb.jpg"
    )
    if failure:
        return failure
    if response.status_code != 200:
        return _pass_json(response)
    result = Response(response.content, mimetype="image/jpeg")
    result.headers["Cache-Control"] = "private, no-store"
    return result


@taklab_vision_api.route("/api/taklab/vision/recordings/<rec_id>/file", methods=["GET"])
def signed_recording_file(rec_id):
    cfg = vision.settings()
    if cfg is None:
        return _error(MSG_OFF, 404)
    rec_id = unquote(rec_id)
    if not vision.signature_valid(
        cfg, rec_id, request.args.get("expires"), request.args.get("signature"), time.time()
    ):
        return _error(MSG_SIGNATURE, 403)
    rec, path = _ready_video(rec_id)
    if rec is None:
        return _error(MSG_NO_RECORDING, 404)
    response = send_file(
        path, mimetype=rec.mime or "video/mp4", conditional=True, max_age=0
    )
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Accel-Buffering"] = "no"
    return response
