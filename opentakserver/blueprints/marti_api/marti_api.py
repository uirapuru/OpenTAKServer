import datetime
import hmac
import os
import traceback
from urllib.parse import unquote, urlparse

from cryptography.exceptions import InvalidSignature
from flask import Blueprint
from flask import current_app as app
from flask import jsonify, request, send_from_directory
from flask_babel import gettext
from cryptography import x509
from cryptography.x509 import UnsupportedGeneralNameType
from simplekml import Document, GxMultiTrack, GxTrack, Icon, IconStyle, Kml, Style

import opentakserver
from opentakserver import __version__ as version
from opentakserver.extensions import db, logger
from opentakserver.functions import datetime_from_iso8601_string, iso8601_string_from_datetime
from opentakserver.models.EUD import EUD
from opentakserver.models.Point import Point

marti_api = Blueprint("marti_api", __name__)


# Taklab: the certificate in X-Ssl-Cert is public data. Anyone who knows the PEM
# of a CA-signed certificate and reaches port 8081 past nginx (another pod in the
# namespace, the api NodePort, the tailnet) used to act as its owner. The header
# now counts only together with a secret that only the TLS-terminating proxy
# (marti-tls) and this process know. The secret lives in a file named by
# OTS_SSL_CERT_PROXY_TOKEN_FILE and is read from the environment on purpose, not
# from app.config, so it never lands in config.yml.
PROXY_TOKEN_HEADER = "X-Taklab-Proxy-Token"


def _proxy_token() -> str | None:
    """None = no proxy secret configured (upstream behaviour); "" = configured but
    unusable, which refuses every forwarded certificate."""
    path = os.environ.get("OTS_SSL_CERT_PROXY_TOKEN_FILE")
    if not path:
        return None
    try:
        with open(path, encoding="utf8") as f:
            return f.read().strip()
    except OSError as e:
        logger.error(f"Cannot read the proxy token file {path}: {e}")
        return ""


def _proxy_token_ok() -> bool:
    expected = _proxy_token()
    if expected is None:
        return True
    given = request.headers.get(PROXY_TOKEN_HEADER, "")
    if not expected or not hmac.compare_digest(given.encode(), expected.encode()):
        logger.warning(
            f"Ignoring {app.config.get('OTS_SSL_CERT_HEADER')} from {request.remote_addr}: "
            "missing or wrong proxy token"
        )
        return False
    return True


# Verifies the client cert forwarded by nginx in the X-Ssl-Cert header
# Returns the parsed cert if valid, otherwise returns None
def verify_client_cert() -> x509.Certificate | None:
    cert_header = app.config.get("OTS_SSL_CERT_HEADER")
    if cert_header not in request.headers:
        return None

    cert = request.headers.get(cert_header)
    if not cert:
        return None

    if not _proxy_token_ok():
        return None

    try:
        cert = x509.load_pem_x509_certificate(unquote(cert).encode("utf8"))
        with open(os.path.join(app.config.get("OTS_CA_FOLDER"), "ca.pem"), "rb") as f:
            ca_cert = x509.load_pem_x509_certificate(f.read())

        # Checks the issuer name and the signature for any key type. The old code
        # assumed RSA with SHA-256 and let InvalidSignature escape as a 500.
        cert.verify_directly_issued_by(ca_cert)

        now = datetime.datetime.now(datetime.timezone.utc)
        if not cert.not_valid_before_utc <= now <= cert.not_valid_after_utc:
            logger.error(f"Certificate outside its validity period: {cert.subject.rfc4514_string()}")
            return None

        """
            Note to future self, get the common name (which is the username) like this
            cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
        """

        return cert
    except (ValueError, TypeError, InvalidSignature, UnsupportedGeneralNameType) as e:
        logger.error(f"Failed to verify certificate: {e}")
        return None


@marti_api.route("/Marti/api/clientEndPoints", methods=["GET"])
def client_end_points():
    # TODO: Add group support ?group=__ANON__
    euds = db.session.execute(db.select(EUD)).scalars()
    return_value = {
        "version": 3,
        "type": "com.bbn.marti.remote.ClientEndpoint",
        "data": [],
        "nodeId": app.config.get("OTS_NODE_ID"),
    }
    for eud in euds:
        if not eud.callsign or not eud.last_event_time:
            continue

        return_value["data"].append(
            {
                "callsign": eud.callsign,
                "uid": eud.uid,
                "username": eud.user.username if eud.user else "anonymous",
                "lastEventTime": iso8601_string_from_datetime(eud.last_event_time),
                "lastStatus": eud.last_status,
            }
        )

    return return_value, 200, {"Content-Type": "application/json"}


@marti_api.route("/Marti/api/version")
def marti_version():
    return f"OpenTAKServer {opentakserver.__version__}"


@marti_api.route("/Marti/api/version/config", methods=["GET"])
def marti_config():
    url = urlparse(request.url_root)

    return (
        {
            "version": "3",
            "type": "ServerConfig",
            "data": {"version": version, "api": "3", "hostname": url.hostname},
            "nodeId": app.config.get("OTS_NODE_ID"),
        },
        200,
        {"Content-Type": "application/json"},
    )


@marti_api.route("/Marti/ExportMissionKML")
def atak_track_history():
    try:
        start_time = request.args.get("startTime")
        end_time = request.args.get("endTime")
        uid = request.args.get("uid")
        file_format = request.args.get("format")
        # Not sure what these three are supposed to do
        multitrack_threshold = request.args.get("multiTrackThreshold")
        extended_data = request.args.get("extendedData")
        optimize_export = request.args.get("optimizeExport")

        kml = Kml()
        doc: Document = kml.newdocument(name=uid)

        eud = db.session.execute(db.session.query(EUD).filter_by(uid=uid)).first()
        if not eud:
            return (
                jsonify({"success": False, "error": gettext("No such UID: %(uid)s", uid=uid)}),
                400,
            )
        eud: EUD = eud[0]

        icon = Icon(href=f"files/team_{eud.team.name.lower().replace(' ', '')}.png")
        icon_style = IconStyle(scale=1.0, heading=0.0, icon=icon)
        style = Style(iconstyle=icon_style)
        doc.styles.append(style)
        kml.addfile(
            os.path.join(
                os.path.dirname(os.path.realpath(__file__)),
                "icons",
                f"team_{eud.team.name.lower().replace(' ', '')}.png",
            )
        )

        query = db.session.query(Point)

        if uid:
            query = query.filter(Point.device_uid == uid)

        if start_time:
            query = query.filter(Point.timestamp >= datetime_from_iso8601_string(start_time))

        if end_time:
            query = query.filter(Point.timestamp <= datetime_from_iso8601_string(end_time))

        points = db.session.execute(query)
        timestamps = []
        coords = []
        for point in points:
            point = point[0]
            timestamps.append(iso8601_string_from_datetime(point.timestamp))
            coords.append((point.longitude, point.latitude))

        multitrack: GxMultiTrack = doc.newgxmultitrack(gxinterpolate=0)
        multitrack.style = style
        multitrack.name = eud.callsign

        track: GxTrack = multitrack.newgxtrack(name=uid, gxaltitudemode="clampToGround")
        track.newwhen(timestamps)
        track.newgxcoord(coords)

        if file_format == "kmz":
            kml.savekmz(os.path.join(app.config.get("UPLOAD_FOLDER"), f"{uid}.kmz"))
            return send_from_directory(
                app.config.get("UPLOAD_FOLDER"),
                f"{uid}.kmz",
                as_attachment=True,
                download_name=f"{uid}.kmz",
            )
        else:
            kml.save(os.path.join(app.config.get("UPLOAD_FOLDER"), f"{uid}.kml"))
            return send_from_directory(
                app.config.get("UPLOAD_FOLDER"),
                f"{uid}.kml",
                as_attachment=True,
                download_name=f"{uid}.kml",
            )

    except BaseException as e:
        logger.error(f"Failed to generate KML: {e}")
        logger.debug(traceback.format_exc())
        return jsonify({"success": False, "error": str(e)}), 500
