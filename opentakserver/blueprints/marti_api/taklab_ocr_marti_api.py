"""/Marti/api/taklab/ocr: document scan -> text for the ATAK TakLab plugin.

The plugin uploads a photo with the phone's client certificate (marti-tls
passes it in X-Ssl-Cert), so the account comes from the certificate, as in
/Marti/api/taklab/config. OTS does not run the model: it checks the account,
size and type, and forwards the job to the central OCR queue in the cluster
(taklab-ocr in taklab-system), which owns jobs, positions and the GPU.

The queue authenticates this server with a per-environment token and keeps
the account of every job; another account (or another environment) gets 404.
It also enforces the per-account limit of active jobs (429).

Settings come only from the environment (the Helm chart), never from
config.yml: OTS writes config.yml from DefaultConfig on first start, and the
token must not end up in a file on the data volume.

Neither the image nor the recognised text is ever written to the log.
"""

import os
import re
import uuid

import requests
from cryptography.x509.oid import NameOID
from flask import Blueprint, Response, jsonify, request
from flask import current_app as app

from opentakserver.blueprints.marti_api.marti_api import verify_client_cert
from opentakserver.extensions import logger

taklab_ocr_marti_api = Blueprint("taklab_ocr_marti_api", __name__)

DEFAULT_MAX_BYTES = 15 * 1024 * 1024
ALLOWED_TYPES = {"image/jpeg": (b"\xff\xd8\xff",), "image/png": (b"\x89PNG\r\n\x1a\n",)}
FORWARD_TIMEOUT = (5, 60)
_FILENAME_BAD = re.compile(r"[\x00-\x1f\x7f]")
PASSED_STATUSES = {200, 202, 204, 400, 404, 409, 413, 415, 429, 503}

MSG_CERT = "Wymagany certyfikat klienta."
MSG_ACCOUNT = "Konto nie istnieje albo jest wyłączone."
MSG_DISABLED = "Rozpoznawanie tekstu nie jest włączone na tym serwerze."
MSG_TOO_LARGE = "Zdjęcie jest za duże (limit {} MB)."
MSG_TYPE = "Nieobsługiwany typ pliku. Wyślij zdjęcie JPEG albo PNG."
MSG_UNAVAILABLE = "Usługa rozpoznawania tekstu jest chwilowo niedostępna. Spróbuj za chwilę."
MSG_NOT_FOUND = "Nie ma takiego zadania."


def ocr_settings() -> dict | None:
    """URL, token, environment name and size limit, or None when OCR is off."""
    url = (os.environ.get("OTS_TAKLAB_OCR_URL") or "").strip().rstrip("/")
    token = (os.environ.get("OTS_TAKLAB_OCR_TOKEN") or "").strip()
    client = (os.environ.get("OTS_TAKLAB_OCR_CLIENT") or "").strip()
    if not (url and token and client):
        return None
    try:
        max_bytes = int(os.environ.get("OTS_TAKLAB_OCR_MAX_BYTES") or DEFAULT_MAX_BYTES)
    except ValueError:
        max_bytes = DEFAULT_MAX_BYTES
    if max_bytes <= 0:
        max_bytes = DEFAULT_MAX_BYTES
    return {"url": url, "token": token, "client": client, "max_bytes": max_bytes}


def config_section() -> dict | None:
    """The "ocr" section of /Marti/api/taklab/config."""
    settings = ocr_settings()
    return None if settings is None else {"maxBytes": settings["max_bytes"]}


def _reply(body, status: int) -> Response:
    response = jsonify(body) if body is not None else Response(status=status)
    response.status_code = status
    response.headers["Cache-Control"] = "no-store"
    return response


def _error(message: str, status: int) -> Response:
    return _reply({"error": message}, status)


def _account() -> tuple[str | None, Response | None]:
    cert = verify_client_cert()
    if not cert:
        return None, _error(MSG_CERT, 401)
    username = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
    user = app.security.datastore.find_user(username=username)
    if not user or not user.active:
        return None, _error(MSG_ACCOUNT, 403)
    return username, None


def _headers(settings: dict, username: str) -> dict:
    return {
        "Authorization": "Bearer " + settings["token"],
        "X-Taklab-Ocr-Client": settings["client"],
        "X-Taklab-Ocr-Account": username,
    }


def _forward(method: str, path: str, settings: dict, **kwargs) -> Response:
    try:
        upstream = requests.request(method, settings["url"] + path, timeout=FORWARD_TIMEOUT, **kwargs)
    except requests.RequestException as e:
        logger.warning("taklab OCR queue unreachable: {}".format(type(e).__name__))
        return _error(MSG_UNAVAILABLE, 503)
    if upstream.status_code not in PASSED_STATUSES:
        logger.warning("taklab OCR queue answered {}".format(upstream.status_code))
        return _error(MSG_UNAVAILABLE, 503)
    if upstream.status_code == 204 or not upstream.content:
        return _reply(None, upstream.status_code)
    try:
        body = upstream.json()
    except ValueError:
        return _error(MSG_UNAVAILABLE, 503)
    return _reply(body, upstream.status_code)


def _job_id(raw: str) -> str | None:
    try:
        return str(uuid.UUID(raw))
    except (ValueError, TypeError):
        return None


def _filename() -> str | None:
    raw = request.headers.get("X-Taklab-Filename")
    if not raw:
        return None
    return _FILENAME_BAD.sub("", raw)[:200] or None


@taklab_ocr_marti_api.route("/Marti/api/taklab/ocr", methods=["POST"])
def ocr_submit():
    username, refusal = _account()
    if refusal:
        return refusal
    settings = ocr_settings()
    if settings is None:
        return _error(MSG_DISABLED, 404)

    max_bytes = settings["max_bytes"]
    too_large = _error(MSG_TOO_LARGE.format(round(max_bytes / 1024 / 1024)), 413)
    if request.content_length is not None and request.content_length > max_bytes:
        return too_large
    mimetype = (request.mimetype or "").lower()
    if mimetype not in ALLOWED_TYPES:
        return _error(MSG_TYPE, 415)
    body = request.stream.read(max_bytes + 1)
    if len(body) > max_bytes:
        return too_large
    if not any(body.startswith(magic) for magic in ALLOWED_TYPES[mimetype]):
        return _error(MSG_TYPE, 415)

    headers = _headers(settings, username)
    headers["Content-Type"] = mimetype
    filename = _filename()
    if filename:
        headers["X-Taklab-Filename"] = filename
    return _forward("POST", "/v1/jobs", settings, data=body, headers=headers)


@taklab_ocr_marti_api.route("/Marti/api/taklab/ocr/<job_id>", methods=["GET", "DELETE"])
def ocr_job(job_id):
    username, refusal = _account()
    if refusal:
        return refusal
    settings = ocr_settings()
    if settings is None:
        return _error(MSG_DISABLED, 404)
    job = _job_id(job_id)
    if job is None:
        return _error(MSG_NOT_FOUND, 404)
    return _forward(request.method, "/v1/jobs/" + job, settings, headers=_headers(settings, username))
