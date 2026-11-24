"""Mission reports for the ATAK TakLab plugin, and the internal routes of the report service.

/Marti/api/taklab/report...: the plugin orders, lists, downloads and deletes
reports with the phone's client certificate (marti-tls passes it in
X-Ssl-Cert), so the account comes from the certificate, as in
/Marti/api/taklab/config. OTS does not build reports: it checks the account
and forwards the call to the report service of this environment
(<env>-report), with the environment's token in X-Report-Token and the
account in X-Report-User. The service owns permissions, jobs and files;
another account's report is 404 there.

/api/taklab/report/...: routes the report service itself calls on 8081,
authorised only with the same token. They read a stored file by hash and
check a mission password (hashed by Flask-Security with a key the service
does not have). Without the token in the environment they do not exist (404).
They are meant for the cluster network only: a request that passed through a
proxy (X-Forwarded-For, e.g. the panel nginx in front of /api/) is refused.

Settings come only from the environment (the Helm chart), never from
config.yml: OTS writes config.yml from DefaultConfig on first start, and the
token must not end up in a file on the data volume.

Neither bodies, passwords nor tokens are ever written to the log.
"""

import hmac
import os
import re

import requests
from cryptography.x509.oid import NameOID
from flask import Blueprint, Response, jsonify, request, stream_with_context
from flask import current_app as app
from flask_security import hash_password, verify_password
from werkzeug.utils import secure_filename

from opentakserver.blueprints.marti_api.marti_api import verify_client_cert
from opentakserver.extensions import db, logger
from opentakserver.models.DataPackage import DataPackage
from opentakserver.models.Mission import Mission
from opentakserver.models.MissionContent import MissionContent

taklab_report_marti_api = Blueprint("taklab_report_marti_api", __name__)

# Read timeout below the 30 s proxy_read_timeout of marti-tls, so the phone
# gets our 503 with a Polish message instead of nginx's bare 504. Downloads
# stream, so the limit applies to the first byte and to gaps between chunks.
FORWARD_TIMEOUT = (5, 25)
MAX_ORDER_BYTES = 64 * 1024
MAX_FILE_BYTES = 5 * 1024 * 1024
PASSED_STATUSES = {200, 202, 400, 404, 409, 429, 503}
_REPORT_ID = re.compile(r"[A-Za-z0-9-]{1,64}")
_FILE_HASH = re.compile(r"[0-9a-f]{32,64}")
_DOWNLOAD_HEADERS = ("Content-Type", "Content-Disposition", "Content-Length")

MSG_CERT = "Wymagany certyfikat klienta."
MSG_ACCOUNT = "Konto nie istnieje albo jest wyłączone."
MSG_DISABLED = "Raporty misji nie są włączone na tym serwerze."
MSG_UNAVAILABLE = "Usługa raportów jest chwilowo niedostępna. Spróbuj za chwilę."
MSG_BAD_ID = "Nieprawidłowy identyfikator raportu."
MSG_TOO_LARGE = "Zamówienie raportu jest za duże."
MSG_BAD_HASH = "Nieprawidłowy skrót pliku."
MSG_NO_FILE = "Nie ma takiego pliku."
MSG_NOT_FOUND = "Nie znaleziono."
MSG_FILE_TOO_LARGE = "Plik jest za duży (limit 5 MB)."


def report_settings() -> dict | None:
    """URL and token of the report service, or None when reports are off."""
    url = (os.environ.get("OTS_TAKLAB_REPORT_URL") or "").strip().rstrip("/")
    token = (os.environ.get("OTS_TAKLAB_REPORT_TOKEN") or "").strip()
    if not (url and token):
        return None
    return {"url": url, "token": token}


def config_section() -> dict | None:
    """The "report" section of /Marti/api/taklab/config."""
    return None if report_settings() is None else {"enabled": True}


def _reply(body, status: int) -> Response:
    response = jsonify(body)
    response.status_code = status
    response.headers["Cache-Control"] = "no-store"
    return response


def _error(message: str, status: int) -> Response:
    return _reply({"error": message}, status)


def _account() -> tuple[str | None, Response | None]:
    cert = verify_client_cert()
    if not cert:
        return None, _error(MSG_CERT, 403)
    names = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    username = names[0].value if names else None
    if not username:
        return None, _error(MSG_ACCOUNT, 403)
    user = app.security.datastore.find_user(username=username)
    if not user or not user.active:
        return None, _error(MSG_ACCOUNT, 403)
    return username, None


def _prepare(report_id: str | None = None) -> tuple[str | None, dict | None, Response | None]:
    """Account, settings, or the refusal - in this order, so an unknown
    certificate learns nothing about the server's configuration."""
    username, refusal = _account()
    if refusal:
        return None, None, refusal
    settings = report_settings()
    if settings is None:
        return None, None, _error(MSG_DISABLED, 404)
    if report_id is not None and not _REPORT_ID.fullmatch(report_id):
        return None, None, _error(MSG_BAD_ID, 400)
    return username, settings, None


def _headers(settings: dict, username: str) -> dict:
    return {"X-Report-Token": settings["token"], "X-Report-User": username}


def _call(method: str, path: str, settings: dict, **kwargs):
    try:
        return requests.request(method, settings["url"] + path, timeout=FORWARD_TIMEOUT, **kwargs)
    except requests.RequestException as e:
        logger.warning("taklab report service unreachable: {}".format(type(e).__name__))
        return None


def _json_reply(upstream) -> Response:
    if upstream is None:
        return _error(MSG_UNAVAILABLE, 503)
    if upstream.status_code not in PASSED_STATUSES:
        logger.warning("taklab report service answered {}".format(upstream.status_code))
        return _error(MSG_UNAVAILABLE, 503)
    try:
        body = upstream.json()
    except ValueError:
        return _error(MSG_UNAVAILABLE, 503)
    return _reply(body, upstream.status_code)


def _forward(method: str, path: str, settings: dict, **kwargs) -> Response:
    return _json_reply(_call(method, path, settings, **kwargs))


@taklab_report_marti_api.route("/Marti/api/taklab/report", methods=["GET", "POST"])
def report_collection():
    username, settings, refusal = _prepare()
    if refusal:
        return refusal
    headers = _headers(settings, username)
    if request.method == "GET":
        return _forward("GET", "/api/v1/reports", settings, headers=headers)

    if request.content_length is not None and request.content_length > MAX_ORDER_BYTES:
        return _error(MSG_TOO_LARGE, 413)
    # get_data, not request.stream: Flask-Security may already have parsed
    # a JSON body while looking for an auth token, and the stream is then empty.
    body = request.get_data(cache=True)
    if len(body) > MAX_ORDER_BYTES:
        return _error(MSG_TOO_LARGE, 413)
    headers["Content-Type"] = "application/json"
    return _forward("POST", "/api/v1/reports", settings, data=body, headers=headers)


@taklab_report_marti_api.route("/Marti/api/taklab/report/<report_id>", methods=["GET", "DELETE"])
def report_item(report_id):
    username, settings, refusal = _prepare(report_id)
    if refusal:
        return refusal
    return _forward(
        request.method, "/api/v1/reports/" + report_id, settings,
        headers=_headers(settings, username),
    )


def _download(report_id: str, ext: str) -> Response:
    username, settings, refusal = _prepare(report_id)
    if refusal:
        return refusal
    upstream = _call(
        "GET", "/api/v1/reports/{}.{}".format(report_id, ext), settings,
        headers=_headers(settings, username), stream=True,
    )
    if upstream is None or upstream.status_code != 200:
        try:
            return _json_reply(upstream)
        finally:
            if upstream is not None:
                upstream.close()

    def chunks():
        try:
            yield from upstream.iter_content(chunk_size=64 * 1024)
        finally:
            upstream.close()

    response = Response(stream_with_context(chunks()), status=200)
    for name in _DOWNLOAD_HEADERS:
        if upstream.headers.get(name):
            response.headers[name] = upstream.headers[name]
    response.headers["Cache-Control"] = "no-store"
    return response


@taklab_report_marti_api.route("/Marti/api/taklab/report/<report_id>.pdf", methods=["GET"])
def report_pdf(report_id):
    return _download(report_id, "pdf")


@taklab_report_marti_api.route("/Marti/api/taklab/report/<report_id>.txt", methods=["GET"])
def report_txt(report_id):
    return _download(report_id, "txt")


# --- Internal routes for the report service (8081, cluster network only) ---


def _internal_refusal() -> Response | None:
    """None when the caller is the report service, otherwise a 404 that looks
    the same whatever was wrong: no token configured, a proxied request, or a
    wrong token."""
    settings = report_settings()
    given = request.headers.get("X-Report-Token") or ""
    if (
        settings is None
        or request.headers.get("X-Forwarded-For")
        or not hmac.compare_digest(given.encode(), settings["token"].encode())
    ):
        return _error(MSG_NOT_FOUND, 404)
    return None


def _stored_path(row) -> str | None:
    """Where OTS keeps the file of a data package or mission content row -
    the same two places /Marti/sync/content serves from."""
    name = secure_filename(row.filename or "")
    _, extension = os.path.splitext(name)
    upload = os.path.join(app.config.get("UPLOAD_FOLDER"), f"{row.hash}{extension}")
    if os.path.isfile(upload):
        return upload
    if name:
        mission_file = os.path.join(app.config.get("OTS_DATA_FOLDER"), "missions", name)
        if os.path.isfile(mission_file):
            return mission_file
    return None


@taklab_report_marti_api.route("/api/taklab/report/file/<file_hash>", methods=["GET"])
def internal_file(file_hash):
    refusal = _internal_refusal()
    if refusal:
        return refusal
    if not _FILE_HASH.fullmatch(file_hash):
        return _error(MSG_BAD_HASH, 400)
    row = db.session.execute(db.select(DataPackage).filter_by(hash=file_hash)).scalar()
    if row is None:
        row = db.session.execute(db.select(MissionContent).filter_by(hash=file_hash)).scalar()
    path = _stored_path(row) if row is not None else None
    if path is None:
        return _error(MSG_NO_FILE, 404)
    if os.path.getsize(path) > MAX_FILE_BYTES:
        return _error(MSG_FILE_TOO_LARGE, 413)
    with open(path, "rb") as f:
        data = f.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        return _error(MSG_FILE_TOO_LARGE, 413)
    response = Response(data, status=200, mimetype=row.mime_type or "application/octet-stream")
    response.headers["Cache-Control"] = "no-store"
    return response


_DUMMY_HASH: list[str] = []


def _dummy_hash() -> str:
    """A fixed hash made with this server's settings (computed once, needs the app)."""
    if not _DUMMY_HASH:
        _DUMMY_HASH.append(hash_password("taklab-report-dummy-password"))
    return _DUMMY_HASH[0]


@taklab_report_marti_api.route("/api/taklab/report/mission-password", methods=["POST"])
def internal_mission_password():
    refusal = _internal_refusal()
    if refusal:
        return refusal
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return _reply({"ok": False}, 200)
    name, password = body.get("name"), body.get("password")
    if not (isinstance(name, str) and isinstance(password, str) and name and password):
        return _reply({"ok": False}, 200)
    mission = db.session.execute(db.select(Mission).filter_by(name=name)).scalar()
    if mission is None or not mission.password:
        # Same work as a real check, so the answer time does not reveal
        # whether a password-protected mission of that name exists.
        verify_password(password, _dummy_hash())
        return _reply({"ok": False}, 200)
    ok = bool(verify_password(password, mission.password))
    return _reply({"ok": ok}, 200)
