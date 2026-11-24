"""GET /Marti/api/taklab/config: ustawienia wtyczki ATAK TakLab dla konta.

Konto ustalamy z certyfikatu klienta (marti-tls przekazuje go w X-Ssl-Cert).
Sekcja "voice" albo "video" jest null, gdy środowisko nie ma tej funkcji
(brak hosta albo portu), a {"error": ...}, gdy token konta nie nadaje się
na hasło. Sekcja "ocr" to {"maxBytes": N} albo null, gdy środowisko nie
ma rozpoznawania tekstu. Sekcja "report" to {"enabled": true} albo null,
gdy środowisko nie ma usługi raportów misji. Odpowiedzi z tokenem nie wolno buforować ani zapisywać w dzienniku.
"""

import hashlib
import json
import sqlite3

from cryptography.x509.oid import NameOID
from flask import Blueprint
from flask import current_app as app
from flask import jsonify

from opentakserver.blueprints.marti_api.marti_api import verify_client_cert
from opentakserver.blueprints.marti_api.taklab_ocr_marti_api import config_section as ocr_config_section
from opentakserver.blueprints.marti_api.taklab_report_marti_api import config_section as report_config_section
from opentakserver.extensions import logger
from opentakserver.taklab_enrollment_token import enrollment_token

taklab_config_marti_api = Blueprint("taklab_config_marti_api", __name__)


def _port(key: str) -> int:
    try:
        return int(app.config.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def _channels_from_mumble_db() -> list[dict] | None:
    """Kanały wprost z bazy Murmura we wspólnym wolumenie poda. Numery nadaje
    kontener startowy jako MAX+1, więc lista z chartu mogłaby się nie zgadzać."""
    path = app.config.get("OTS_TAKLAB_MUMBLE_DB")
    if not path:
        return None
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
        try:
            rows = con.execute(
                "SELECT channel_id, name FROM channels "
                "WHERE server_id = 1 AND channel_id > 0 ORDER BY channel_id"
            ).fetchall()
        finally:
            con.close()
    except sqlite3.Error as e:
        logger.warning("Cannot read Mumble channels from {}: {}".format(path, e))
        return None
    return [{"id": int(cid), "name": str(name)} for cid, name in rows]


def _channels() -> list[dict]:
    from_db = _channels_from_mumble_db()
    if from_db is not None:
        return from_db
    try:
        channels = json.loads(app.config.get("OTS_TAKLAB_MUMBLE_CHANNELS") or "[]")
    except ValueError:
        logger.error("OTS_TAKLAB_MUMBLE_CHANNELS is not valid JSON")
        return []
    return [
        {"id": int(c["id"]), "name": str(c["name"])}
        for c in channels
        if isinstance(c, dict) and "id" in c and "name" in c
    ]


@taklab_config_marti_api.route("/Marti/api/taklab/config")
def taklab_config():
    cert = verify_client_cert()
    if not cert:
        return jsonify({"success": False, "error": "Client certificate required"}), 401
    username = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value

    user = app.security.datastore.find_user(username=username)
    if not user or not user.active:
        return jsonify({"success": False, "error": "Account not found or inactive"}), 403

    host = (app.config.get("OTS_TAKLAB_HOST") or "").strip()
    mumble_port = _port("OTS_TAKLAB_MUMBLE_PORT")
    rtsp_port = _port("OTS_TAKLAB_RTSP_PORT")
    want_voice = bool(host and mumble_port)
    want_video = bool(host and rtsp_port)

    token, error = (None, None)
    if want_voice or want_video:
        token, error = enrollment_token(username)

    voice = None
    if want_voice:
        voice = {"error": error} if error else {
            "host": host, "port": mumble_port, "username": username,
            "token": token, "channels": _channels(),
        }
    video = None
    if want_video:
        video = {"error": error} if error else {
            "host": host, "port": rtsp_port, "path": username,
            "username": username, "token": token,
        }

    body = {"server": host, "account": username, "voice": voice, "video": video,
            "ocr": ocr_config_section(), "report": report_config_section()}
    body["version"] = hashlib.sha256(
        json.dumps(body, sort_keys=True).encode()
    ).hexdigest()[:16]

    response = jsonify(body)
    response.headers["Cache-Control"] = "no-store"
    return response
