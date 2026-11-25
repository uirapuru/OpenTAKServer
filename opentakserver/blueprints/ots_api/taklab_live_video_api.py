"""taklab: GET /api/taklab/video/live - live video streams with technical data.

Contract: ~/taklab docs/superpowers/specs/2026-11-25-live-video-design.md, part 1.

Who is asking, in this order (same as taklab_recordings_api):
  1. a client certificate forwarded by marti-tls in X-Ssl-Cert (CloudTAK calls
     https://<env>-ots:8443/api/taklab/video/live with the user's certificate),
  2. a Flask-Security session or Authentication-Token (OpenTAKServer-UI).

Video is readable by every account (decision in docs/wideo.md), so there is no
group filter. The list comes from video_streams.ready and is enriched with the
local MediaMTX API. A MediaMTX failure never breaks the reply: the stream gets
"tech": null. Remote addresses of publishers are never returned.

Field mapping measured on bluenviron/mediamtx:1.20.1-ffmpeg (samples in
tests/fixtures/mediamtx_1_20_1):
  /v3/paths/get/<path>: readyTime, source.type/id, readers[], bytesReceived,
      tracks2[].codec + codecProps (width, height, profile, level |
      sampleRate, channelCount). No frame rate anywhere -> fps is null.
  /v3/rtspsessions|rtspssessions/get/<id>: transport, rtpPacketsLost,
      rtpPacketsJitter (mean over all tracks in RTP clock units).
  /v3/rtmpconns|rtmpsconns/get/<id>: no transport, loss or jitter.
  /v3/srtconns/get/<id>: packetsReceivedLoss, no transport field.
"""

import re
import threading
import time
from datetime import datetime, timezone
from urllib.parse import quote

import requests
from cryptography.x509.oid import NameOID
from flask import Blueprint, Response, current_app, jsonify
from flask_security import current_user
from flask_security.decorators import _check_session, _check_token
from sqlalchemy import select

from opentakserver.blueprints.marti_api.marti_api import verify_client_cert
from opentakserver.extensions import db, logger
from opentakserver.models.VideoStream import VideoStream

taklab_live_video_api = Blueprint("taklab_live_video_api", __name__)

MSG_AUTH = "Wymagane logowanie."
MEDIAMTX_TIMEOUT = 1.5
DEFAULT_MEDIAMTX = "http://127.0.0.1:9997"
MIN_BITRATE_WINDOW = 2.0

# source.type z MediaMTX -> zasób API z licznikami sesji nadawcy
SESSION_ENDPOINTS = {
    "rtspSession": "rtspsessions",
    "rtspsSession": "rtspssessions",
    "rtmpConn": "rtmpconns",
    "rtmpsConn": "rtmpsconns",
    "srtConn": "srtconns",
}
VIDEO_CODECS = {"H264", "H265", "VP8", "VP9", "AV1", "MPEG-4 Video", "MPEG-1/2 Video", "M-JPEG"}
VIDEO_CLOCK_RATE = 90000
AUDIO_CLOCK_RATES = {"Opus": 48000, "G711": 8000, "G722": 8000, "MPEG-1/2 Audio": 90000}

# Poprzedni odczyt bytesReceived na ścieżkę: path -> (monotonic, bytes, readyTime, kbps)
_samples: dict[str, tuple[float, int, str, float | None]] = {}
_samples_lock = threading.Lock()


class MediaMTXUnavailable(Exception):
    pass


def _reply(body, status: int = 200) -> Response:
    response = jsonify(body)
    response.status_code = status
    response.headers["Cache-Control"] = "no-store"
    return response


def _viewer():
    cert = verify_client_cert()
    if cert is not None:
        username = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
        user = current_app.security.datastore.find_user(username=username) if username else None
        return user if user is not None and user.active else None
    if _check_session() or _check_token():
        user = current_user._get_current_object()
        return user if user is not None and user.active else None
    return None


def _iso(moment: datetime | None) -> str | None:
    if moment is None:
        return None
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_mediamtx_time(raw) -> datetime | None:
    """MediaMTX gives RFC 3339 with nanoseconds: 2026-11-25T14:58:27.437948712Z."""
    if not isinstance(raw, str):
        return None
    match = re.match(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)$", raw)
    if not match:
        return None
    base, fraction, zone = match.groups()
    text = base + ("." + fraction[:6].ljust(6, "0") if fraction else "") + ("+00:00" if zone == "Z" else zone)
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


class MediaMTXClient:
    """Short-timeout reader; after the first network failure it stops asking."""

    def __init__(self, base: str):
        self.base = base.rstrip("/")
        self.down = False

    def get(self, resource: str, name: str) -> dict | None:
        if self.down:
            raise MediaMTXUnavailable()
        url = f"{self.base}/v3/{resource}/get/{quote(name, safe='/')}"
        try:
            response = requests.get(url, timeout=MEDIAMTX_TIMEOUT)
        except requests.RequestException as error:
            self.down = True
            logger.warning(f"taklab live video: MediaMTX unreachable: {error}")
            raise MediaMTXUnavailable() from error
        if response.status_code != 200:
            return None
        try:
            body = response.json()
        except ValueError:
            return None
        return body if isinstance(body, dict) else None


def _int(value) -> int | None:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _num(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _tracks(path_info: dict) -> list[dict]:
    tracks = path_info.get("tracks2")
    if isinstance(tracks, list) and tracks:
        return [t for t in tracks if isinstance(t, dict)]
    # Starsze API: same nazwy kodeków
    return [{"codec": name} for name in path_info.get("tracks") or [] if isinstance(name, str)]


def _is_video(track: dict) -> bool:
    props = track.get("codecProps") or {}
    return track.get("codec") in VIDEO_CODECS or "width" in props


def _is_audio(track: dict) -> bool:
    props = track.get("codecProps") or {}
    return not _is_video(track) and (bool(track.get("codec")) or "sampleRate" in props)


def _video(tracks: list[dict]) -> dict | None:
    track = next((t for t in tracks if _is_video(t)), None)
    if track is None:
        return None
    props = track.get("codecProps") or {}
    level = props.get("level")
    return {
        "codec": track.get("codec"),
        "width": _int(props.get("width")),
        "height": _int(props.get("height")),
        "profile": props.get("profile") if isinstance(props.get("profile"), str) else None,
        "level": str(level) if isinstance(level, (str, int, float)) and not isinstance(level, bool) else None,
        # MediaMTX 1.20.1 nie podaje liczby klatek na sekundę
        "fps": _num(props.get("fps")),
    }


def _audio(tracks: list[dict]) -> dict | None:
    track = next((t for t in tracks if _is_audio(t)), None)
    if track is None:
        return None
    props = track.get("codecProps") or {}
    return {
        "codec": track.get("codec"),
        "sample_rate": _int(props.get("sampleRate")),
        "channels": _int(props.get("channelCount")),
    }


def _clock_rate(track: dict) -> int | None:
    if _is_video(track):
        return VIDEO_CLOCK_RATE
    codec = track.get("codec")
    if codec == "MPEG-4 Audio":
        return _int((track.get("codecProps") or {}).get("sampleRate"))
    return AUDIO_CLOCK_RATES.get(codec)


def _jitter_ms(raw, tracks: list[dict]) -> float | None:
    """rtpPacketsJitter is a mean over tracks in their own RTP clock units.

    It converts to milliseconds only with a single track; with video + audio
    the units are mixed and the value cannot be undone, so it stays null.
    """
    value = _num(raw)
    if value is None or len(tracks) != 1:
        return None
    rate = _clock_rate(tracks[0])
    if not rate:
        return None
    return round(value / rate * 1000, 2)


def _bitrate_kbps(path: str, total: int | None, ready_raw, ready_at: datetime | None) -> float | None:
    if total is None:
        return None
    now = time.monotonic()
    ready_key = ready_raw if isinstance(ready_raw, str) else ""
    with _samples_lock:
        previous = _samples.get(path)
        if previous is not None and previous[2] == ready_key and total >= previous[1]:
            elapsed = now - previous[0]
            if elapsed < MIN_BITRATE_WINDOW:
                # Za krótkie okno: zostaje poprzedni pomiar, próbka bazowa bez zmian
                if previous[3] is not None:
                    return previous[3]
                return _average_since_ready(total, ready_at)
            kbps = round((total - previous[1]) * 8 / elapsed / 1000, 1)
            _samples[path] = (now, total, ready_key, kbps)
            return kbps
        kbps = _average_since_ready(total, ready_at)
        _samples[path] = (now, total, ready_key, None)
        return kbps


def _average_since_ready(total: int, ready_at: datetime | None) -> float | None:
    if ready_at is None:
        return None
    seconds = (datetime.now(timezone.utc) - ready_at).total_seconds()
    if seconds <= 0:
        return None
    return round(total * 8 / seconds / 1000, 1)


def _forget_other_paths(live_paths: set[str]) -> None:
    with _samples_lock:
        for path in list(_samples):
            if path not in live_paths:
                del _samples[path]


def _stream_tech(client: MediaMTXClient, path: str) -> tuple[dict | None, datetime | None]:
    try:
        info = client.get("paths", path)
    except MediaMTXUnavailable:
        return None, None
    if info is None:
        return None, None

    ready_at = parse_mediamtx_time(info.get("readyTime"))
    source = info.get("source") if isinstance(info.get("source"), dict) else {}
    source_type = source.get("type") if isinstance(source.get("type"), str) else None
    tracks = _tracks(info)
    readers = info.get("readers")
    total = _int(info.get("bytesReceived"))
    if total is None:
        total = _int(info.get("inboundBytes"))

    session = None
    endpoint = SESSION_ENDPOINTS.get(source_type)
    if endpoint and isinstance(source.get("id"), str):
        try:
            session = client.get(endpoint, source["id"])
        except MediaMTXUnavailable:
            session = None
    session = session or {}

    if source_type in ("rtspSession", "rtspsSession"):
        lost = _int(session.get("rtpPacketsLost", session.get("inboundRTPPacketsLost")))
        jitter = _jitter_ms(session.get("rtpPacketsJitter", session.get("inboundRTPPacketsJitter")), tracks)
    elif source_type == "srtConn":
        lost = _int(session.get("packetsReceivedLoss"))
        jitter = None
    else:
        lost = None
        jitter = None

    tech = {
        "source": source_type,
        "transport": session.get("transport") if isinstance(session.get("transport"), str) else None,
        "readers": len(readers) if isinstance(readers, list) else None,
        "bytes_received": total,
        "bitrate_kbps": _bitrate_kbps(path, total, info.get("readyTime"), ready_at),
        "packets_lost": lost,
        "jitter_ms": jitter,
        "video": _video(tracks),
        "audio": _audio(tracks),
    }
    return tech, ready_at


@taklab_live_video_api.route("/api/taklab/video/live", methods=["GET"])
def live_streams():
    if _viewer() is None:
        return _reply({"error": MSG_AUTH}, 401)

    rows = db.session.execute(
        select(VideoStream).where(VideoStream.ready.is_(True)).order_by(VideoStream.path)
    ).scalars().all()

    client = MediaMTXClient(current_app.config.get("OTS_MEDIAMTX_API_ADDRESS") or DEFAULT_MEDIAMTX)
    if not current_app.config.get("OTS_MEDIAMTX_ENABLE", True):
        client.down = True

    streams = []
    for row in rows:
        tech, ready_at = _stream_tech(client, row.path)
        streams.append(
            {
                "path": row.path,
                "username": row.username,
                "started_at": _iso(ready_at),
                "tech": tech,
            }
        )
    _forget_other_paths({row.path for row in rows})

    return _reply({"generated_at": _iso(datetime.now(timezone.utc)), "streams": streams})
