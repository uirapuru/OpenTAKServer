"""taklab: recordings registry - video, voice messages and OCR scans on the map.

Design: ~/taklab docs/superpowers/specs/2026-11-24-recordings-design.md.

Where recordings come from
    * video: MediaMTX records every publication (pathDefaults record: yes,
      fMP4, 10 minute segments) and calls the OTS webhook when a segment is
      complete. Only "account paths" are kept - the MediaMTX path is the name
      of an existing account and that account is the publisher (the TakLab
      ATAK plugin publishes path = account). Segments of other paths are
      deleted at once. One continuous publication is one recording: a segment
      that starts within JOIN_GAP of the end of the previous one extends it,
      so a plugin reconnecting within 60 s stays in the same recording. When
      the publisher has been gone for longer than JOIN_GAP the janitor joins
      the segments into one MP4 (ffmpeg concat, stream copy, no transcoding).
    * voice and scan: POST /api/taklab/recordings/ingest from the voice
      register (same namespace) and the OCR queue (taklab-system), protected
      by an environment secret.

Position: the account's device. An account may have several EUDs (phone,
CloudTAK, tablet); the device is the one with the newest own position report
(points.uid = points.device_uid) at or before the start of the recording, not
older than POSITION_MAX_AGE, preferring a device other than CloudTAK when both
reported in that window (the plugin streams from the phone). The track is the
same device's points between start and end, decimated.

Who sees a recording: the same rule as CoT group routing. The author's
enabled IN groups (or __ANON__ when they have none) against the viewer's
enabled OUT groups (or __ANON__ when they have none) - the rule of
plugins/ots-aibuddy-config/visibility.py (audience_for/is_audible), which
mirrors cot_parser.route_cot and EudHandler. "administrator" sees everything.
Rename and delete: the author or an administrator. A recording whose author
account no longer exists is visible to administrators only.

Map marker: a persistent CoT (stale in a year, <archive/>) at the start
position, routed like a CoT of the author (route_cot), with the recording id
in <__taklab_recording id kind/>. No token, password or file URL is ever put
in the CoT. Deleting a recording sends t-x-d-d with <__forcedelete/>.

Settings come only from the environment (the Helm chart), as for OCR.
"""

import json
import logging
import math
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.etree.ElementTree import Element, SubElement, tostring
from zoneinfo import ZoneInfo

from sqlalchemy import select

from opentakserver.extensions import db, logger
from opentakserver.models.TaklabRecording import (
    KINDS,
    STATE_ERROR,
    STATE_READY,
    STATE_RECORDING,
    TaklabRecording,
)

CLOUDTAK_UID_PREFIX = "ANDROID-CloudTAK-"
MARKER_PREFIX = "taklab-recording-"
MARKER_TYPES = {"video": "b-i-v", "voice": "b-i-x-i", "scan": "b-i-x-i"}
MARKER_STALE = timedelta(days=365)
TRACK_MAX_POINTS = 500
TRACK_MIN_STEP_M = 5.0
SPACE_HIGH = 0.90
SPACE_TARGET = 0.85
GIB = 1024**3

KIND_LABELS = {"video": "Wideo", "voice": "Głosówka", "scan": "Skan"}
KIND_NAMES = {"video": "Nagranie wideo", "voice": "Głosówka", "scan": "Skan dokumentu"}
MIME_EXT = {
    "video/mp4": "mp4",
    "audio/ogg": "ogg",
    "audio/wav": "wav",
    "image/jpeg": "jpg",
    "image/png": "png",
}
INGEST_MIMES = {
    "voice": {"audio/ogg": (b"OggS",), "audio/wav": (b"RIFF",)},
    "scan": {"image/jpeg": (b"\xff\xd8\xff",), "image/png": (b"\x89PNG\r\n\x1a\n",)},
}
# MediaMTX recordPath ends with %Y-%m-%d_%H-%M-%S-%f (UTC in the container)
_SEGMENT_TIME = re.compile(r"(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})-(\d{1,6})")
_AUTHOR = re.compile(r"^\s*(?P<callsign>.*?)\s*\((?P<account>[A-Za-z0-9_.]+)\)\s*$")

_janitor_lock = threading.Lock()


@dataclass(frozen=True)
class Settings:
    root: Path
    retention_days: float
    limit_bytes: int
    min_free_bytes: int
    ingest_token: str
    ocr_token: str
    timezone: str
    link: str
    join_gap_s: float
    position_max_age_s: float
    janitor_s: float
    playback_url: str
    max_ingest_bytes: int


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or "").strip() or default


def _number(name: str, default: float) -> float:
    try:
        value = float(_env(name, str(default)))
    except ValueError:
        return default
    return value if value >= 0 else default


def settings() -> Settings | None:
    """None when the registry is off (no recordings directory configured)."""
    root = _env("OTS_TAKLAB_RECORDINGS_DIR")
    if not root:
        return None
    return Settings(
        root=Path(root),
        retention_days=_number("OTS_TAKLAB_RECORDINGS_RETENTION_DAYS", 30),
        limit_bytes=int(_number("OTS_TAKLAB_RECORDINGS_LIMIT_BYTES", 50 * GIB)),
        min_free_bytes=int(_number("OTS_TAKLAB_RECORDINGS_MIN_FREE_BYTES", 5 * GIB)),
        ingest_token=_env("OTS_TAKLAB_RECORDINGS_INGEST_TOKEN"),
        ocr_token=_env("OTS_TAKLAB_OCR_TOKEN"),
        timezone=_env("OTS_TAKLAB_RECORDINGS_TIMEZONE", "Europe/Warsaw"),
        link=_env("OTS_TAKLAB_RECORDINGS_LINK"),
        join_gap_s=_number("OTS_TAKLAB_RECORDINGS_JOIN_GAP_S", 60),
        position_max_age_s=_number("OTS_TAKLAB_RECORDINGS_POSITION_MAX_AGE_S", 3600),
        janitor_s=_number("OTS_TAKLAB_RECORDINGS_JANITOR_S", 60),
        playback_url=_env("OTS_TAKLAB_RECORDINGS_PLAYBACK_URL", "http://127.0.0.1:9996"),
        max_ingest_bytes=int(_number("OTS_TAKLAB_RECORDINGS_INGEST_MAX_BYTES", 50 * 1024 * 1024)),
    )


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def naive_utc(moment: datetime) -> datetime:
    if moment.tzinfo is not None:
        moment = moment.astimezone(timezone.utc).replace(tzinfo=None)
    return moment


def parse_time(value) -> datetime | None:
    """ISO 8601 (with Z or offset) or unix seconds -> naive UTC; None if unreadable."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc).replace(tzinfo=None)
    text = str(value).strip()
    try:
        return datetime.fromtimestamp(float(text), tz=timezone.utc).replace(tzinfo=None)
    except ValueError:
        pass
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        return moment
    return naive_utc(moment)


# --- identity and visibility -------------------------------------------------


def find_user(username: str | None):
    if not username:
        return None
    from flask import current_app as app

    return app.security.datastore.find_user(username=username)


def is_admin(user) -> bool:
    return bool(user) and user.has_role("administrator")


def can_see(viewer, rec: TaklabRecording) -> bool:
    # User decision 24 XI 2026: recordings are for administrators only (list,
    # playback, download, rename, delete). Map markers still go to the author's groups.
    return not rec.deleted and is_admin(viewer)


def can_manage(viewer, rec: TaklabRecording) -> bool:
    return is_admin(viewer)


# --- positions ---------------------------------------------------------------


def parse_author(author: str | None) -> tuple[str | None, str | None]:
    """'znak (konto)' -> (callsign, account); anything else -> (author, None)."""
    if not author:
        return None, None
    match = _AUTHOR.match(author)
    if match:
        return (match.group("callsign") or None), match.group("account")
    return author.strip() or None, None


def resolve_author(account: str | None, callsign: str | None):
    """The account of a recording: given explicitly, or found by an EUD callsign."""
    from opentakserver.models.EUD import EUD
    from opentakserver.models.user import User

    user = find_user(account)
    if user is None and callsign:
        eud = db.session.execute(
            select(EUD).where(EUD.callsign == callsign, EUD.user_id.is_not(None))
        ).scalars().first()
        if eud is not None:
            user = db.session.get(User, eud.user_id)
    return user


def device_position(user, at: datetime, max_age_s: float):
    """(device_uid, callsign, lat, lon) of the account's device at a moment, or None."""
    from opentakserver.models.EUD import EUD
    from opentakserver.models.Point import Point

    if user is None:
        return None
    euds = db.session.execute(select(EUD.uid, EUD.callsign).where(EUD.user_id == user.id)).all()
    if not euds:
        return None
    callsigns = {uid: callsign for uid, callsign in euds}
    window_start = at - timedelta(seconds=max_age_s)
    # No report before the start: the first fix within a minute after it counts.
    window_end = at + timedelta(seconds=60)
    base = (Point.latitude.is_not(None), Point.longitude.is_not(None))
    before, after = [], []
    for uid in callsigns:
        own = (Point.device_uid == uid, Point.uid == uid, *base)
        point = db.session.execute(
            select(Point.latitude, Point.longitude, Point.timestamp)
            .where(*own, Point.timestamp <= at, Point.timestamp >= window_start)
            .order_by(Point.timestamp.desc(), Point.id.desc())
            .limit(1)
        ).first()
        if point is not None:
            before.append((uid, point))
            continue
        point = db.session.execute(
            select(Point.latitude, Point.longitude, Point.timestamp)
            .where(*own, Point.timestamp > at, Point.timestamp <= window_end)
            .order_by(Point.timestamp, Point.id)
            .limit(1)
        ).first()
        if point is not None:
            after.append((uid, point))
    candidates = before or after
    if not candidates:
        return None
    phones = [c for c in candidates if not c[0].startswith(CLOUDTAK_UID_PREFIX)]
    pick = max if before else min
    uid, point = pick(phones or candidates, key=lambda c: c[1][2])
    return uid, callsigns.get(uid), point[0], point[1]


def _distance_m(a, b) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(
        (lon2 - lon1) / 2
    ) ** 2
    return 2 * 6371000 * math.asin(min(1.0, math.sqrt(h)))


def decimate(points: list, max_points: int = TRACK_MAX_POINTS, min_step_m: float = TRACK_MIN_STEP_M):
    """Drop points closer than min_step_m to the last kept one, then thin evenly.

    First and last points always stay.
    """
    if len(points) <= 2:
        return list(points)
    kept = [points[0]]
    for point in points[1:-1]:
        if _distance_m(kept[-1], point) >= min_step_m:
            kept.append(point)
    kept.append(points[-1])
    if len(kept) > max_points:
        step = (len(kept) - 1) / (max_points - 1)
        kept = [kept[round(i * step)] for i in range(max_points)]
    return kept


def build_track(device_uid: str | None, start: datetime, end: datetime | None) -> list:
    from opentakserver.models.Point import Point
    from opentakserver.models.TaklabRecording import iso

    if not device_uid:
        return []
    end = end or start
    rows = db.session.execute(
        select(Point.latitude, Point.longitude, Point.timestamp)
        .where(
            Point.device_uid == device_uid,
            Point.uid == device_uid,
            Point.timestamp >= start,
            Point.timestamp <= end,
            Point.latitude.is_not(None),
            Point.longitude.is_not(None),
        )
        .order_by(Point.timestamp, Point.id)
    ).all()
    return [[lat, lon, iso(ts)] for lat, lon, ts in decimate([tuple(r) for r in rows])]


def locate(rec: TaklabRecording, user, config: Settings) -> None:
    """Fill device, callsign and start position from the account's device."""
    found = device_position(user, rec.started_at, config.position_max_age_s)
    if found is None:
        return
    rec.device_uid, device_callsign, rec.latitude, rec.longitude = found
    if not rec.callsign:
        rec.callsign = device_callsign


# --- map markers -------------------------------------------------------------


def _local(moment: datetime, config: Settings) -> datetime:
    try:
        zone = ZoneInfo(config.timezone)
    except Exception:  # noqa: BLE001 - a bad zone name must not stop the marker
        zone = timezone.utc
    return moment.replace(tzinfo=timezone.utc).astimezone(zone)


def _duration_text(seconds: float | None) -> str:
    if seconds is None:
        return "trwa"
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds} s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} min {seconds} s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min"


def marker_remarks(rec: TaklabRecording, config: Settings) -> str:
    start = _local(rec.started_at, config)
    when = start.strftime("%d.%m.%Y %H:%M")
    if rec.ended_at is not None:
        when += "–" + _local(rec.ended_at, config).strftime("%H:%M")
    who = rec.callsign or rec.account or "nieznany"
    if rec.account and rec.callsign and rec.callsign != rec.account:
        who = f"{rec.callsign} ({rec.account})"
    duration = "" if rec.kind == "scan" else f", {_duration_text(rec.duration_s())}"
    parts = [f"{KIND_LABELS.get(rec.kind, rec.kind)}: {rec.name}", f"{who}, {when}{duration}"]
    if rec.state == STATE_RECORDING:
        parts.append("Nagrywanie trwa.")
    if config.link:
        parts.append("Odtwarzanie: " + config.link.replace("{id}", rec.id))
    return "\n".join(parts)


def marker_event(rec: TaklabRecording, config: Settings, now: datetime | None = None) -> str:
    now = now or utcnow()
    stamp = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"
    stale = now + MARKER_STALE
    event = Element(
        "event",
        {
            "version": "2.0",
            "uid": rec.marker_uid or MARKER_PREFIX + rec.id,
            "type": MARKER_TYPES.get(rec.kind, "b-i-x-i"),
            "how": "h-g-i-g-o",
            "time": stamp,
            "start": stamp,
            "stale": stale.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        },
    )
    SubElement(
        event,
        "point",
        {
            "lat": f"{rec.latitude:.7f}",
            "lon": f"{rec.longitude:.7f}",
            "hae": "9999999.0",
            "ce": "9999999.0",
            "le": "9999999.0",
        },
    )
    detail = SubElement(event, "detail")
    title = rec.name or KIND_NAMES.get(rec.kind, "Nagranie")
    SubElement(detail, "contact", {"callsign": title[:120]})
    SubElement(detail, "remarks").text = marker_remarks(rec, config)
    SubElement(detail, "archive")
    # Numer nagrania: <link> czyta node-cot (CloudTAK, properties.links),
    # element wlasny niesie tez rodzaj. Zaden z nich nie ma adresu pliku.
    SubElement(detail, "link", {"uid": f"rec:{rec.id}", "relation": "r-u", "type": "taklab-recording"})
    SubElement(detail, "__taklab_recording", {"id": rec.id, "kind": rec.kind})
    return tostring(event).decode("utf-8")


def delete_event(rec: TaklabRecording, now: datetime | None = None) -> str:
    now = now or utcnow()
    stamp = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"
    stale = (now + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    event = Element(
        "event",
        {
            "version": "2.0",
            "uid": str(uuid.uuid4()),
            "type": "t-x-d-d",
            "how": "h-g-i-g-o",
            "time": stamp,
            "start": stamp,
            "stale": stale,
        },
    )
    SubElement(event, "point", {"lat": "0", "lon": "0", "hae": "0", "ce": "9999999", "le": "9999999"})
    detail = SubElement(event, "detail")
    SubElement(
        detail,
        "link",
        {
            "uid": rec.marker_uid or MARKER_PREFIX + rec.id,
            "relation": "none",
            "type": MARKER_TYPES.get(rec.kind, "b-i-x-i"),
        },
    )
    SubElement(detail, "__forcedelete")
    return tostring(event).decode("utf-8")


def _route(xml: str, user) -> None:
    """Send a server CoT to the author's groups; failures are logged, never raised."""
    from opentakserver.blueprints.ots_api.api import route_cot

    try:
        route_cot(xml, user)
    except Exception as e:  # noqa: BLE001 - RabbitMQ down must not lose the recording
        logger.warning(f"taklab recordings: cannot publish CoT ({type(e).__name__})")


def publish_marker(rec: TaklabRecording, config: Settings) -> bool:
    """Publish (or refresh, same uid) the marker; only with a position and a known author."""
    user = find_user(rec.account)
    if user is None or rec.latitude is None or rec.longitude is None:
        return False
    rec.marker_uid = rec.marker_uid or MARKER_PREFIX + rec.id
    _route(marker_event(rec, config), user)
    return True


def remove_marker(rec: TaklabRecording) -> None:
    if not rec.marker_uid:
        return
    user = find_user(rec.account)
    if user is None:
        return
    _route(delete_event(rec), user)


# --- files and space ---------------------------------------------------------


def _ensure_dirs(config: Settings) -> None:
    for sub in ("video", "voice", "scan"):
        (config.root / sub).mkdir(parents=True, exist_ok=True)


def absolute(config: Settings, relative: str | None) -> Path | None:
    """Path inside the recordings directory, or None for anything outside it."""
    if not relative:
        return None
    root = config.root.resolve()
    path = (root / relative).resolve()
    if path != root and root in path.parents:
        return path
    return None


def relative_to_root(config: Settings, path: str | Path) -> str | None:
    root = config.root.resolve()
    resolved = Path(path).resolve()
    if root in resolved.parents:
        return str(resolved.relative_to(root))
    return None


def write_file(config: Settings, kind: str, rec_id: str, data: bytes, mime: str) -> str:
    _ensure_dirs(config)
    day = utcnow().strftime("%Y/%m/%d")
    relative = f"{kind}/{day}/{rec_id}.{MIME_EXT.get(mime, 'bin')}"
    path = config.root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_bytes(data)
    tmp.replace(path)
    return relative


def delete_files(rec: TaklabRecording, config: Settings) -> None:
    for relative in [rec.file_path, *rec.segment_list()]:
        path = absolute(config, relative)
        if path is not None:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError as e:
                logger.warning(f"taklab recordings: cannot delete {relative}: {e}")


def used_bytes() -> int:
    total = db.session.execute(
        select(db.func.coalesce(db.func.sum(TaklabRecording.size_bytes), 0)).where(
            TaklabRecording.deleted.is_(False)
        )
    ).scalar()
    return int(total or 0)


def disk_free(config: Settings) -> tuple[int | None, int | None]:
    try:
        usage = shutil.disk_usage(config.root)
    except OSError:
        return None, None
    return usage.free, usage.total


def usage(config: Settings) -> dict:
    used = used_bytes()
    free, total = disk_free(config)
    count = db.session.execute(
        select(db.func.count()).select_from(TaklabRecording).where(TaklabRecording.deleted.is_(False))
    ).scalar()
    return {
        "used_bytes": used,
        "limit_bytes": config.limit_bytes,
        "percent": round(100.0 * used / config.limit_bytes, 1) if config.limit_bytes else None,
        "disk_free_bytes": free,
        "disk_total_bytes": total,
        "min_free_bytes": config.min_free_bytes,
        "retention_days": config.retention_days,
        "count": int(count or 0),
    }


def delete_recording(rec: TaklabRecording, config: Settings, reason: str) -> None:
    """Soft delete: files gone at once, marker withdrawn, row kept as deleted."""
    delete_files(rec, config)
    remove_marker(rec)
    rec.deleted = True
    rec.deleted_at = utcnow()
    rec.file_path = None
    rec.segments = None
    rec.size_bytes = 0
    rec.text = None
    rec.track = None
    db.session.add(rec)
    db.session.commit()
    logger.info(f"taklab recordings: deleted {rec.kind} {rec.id} ({reason})")


def expire(config: Settings, now: datetime | None = None) -> int:
    now = now or utcnow()
    cutoff = now - timedelta(days=config.retention_days)
    expired = db.session.execute(
        select(TaklabRecording).where(
            TaklabRecording.deleted.is_(False),
            db.func.coalesce(TaklabRecording.ended_at, TaklabRecording.started_at) < cutoff,
            TaklabRecording.state != STATE_RECORDING,
        )
    ).scalars().all()
    for rec in expired:
        delete_recording(rec, config, "retention")
    # Deleted rows are kept for one more retention period, then dropped.
    db.session.execute(
        db.delete(TaklabRecording).where(
            TaklabRecording.deleted.is_(True), TaklabRecording.deleted_at < cutoff
        )
    )
    db.session.commit()
    return len(expired)


def _space_low(config: Settings, used: int) -> bool:
    if config.limit_bytes and used > SPACE_HIGH * config.limit_bytes:
        return True
    free, _total = disk_free(config)
    return free is not None and free < config.min_free_bytes


def enforce_space(config: Settings) -> int:
    """Over 90 % of the limit (or the disk nearly full): oldest go first, down to 85 %."""
    used = used_bytes()
    if not _space_low(config, used):
        return 0
    removed = 0
    target = SPACE_TARGET * config.limit_bytes if config.limit_bytes else 0
    oldest = db.session.execute(
        select(TaklabRecording)
        .where(TaklabRecording.deleted.is_(False), TaklabRecording.state != STATE_RECORDING)
        .order_by(TaklabRecording.started_at)
    ).scalars().all()
    for rec in oldest:
        free, _ = disk_free(config)
        disk_ok = free is None or free >= config.min_free_bytes
        if used <= target and disk_ok:
            break
        size = rec.size_bytes or 0
        delete_recording(rec, config, "space")
        used -= size
        removed += 1
    logger.warning(
        f"taklab recordings: space low, deleted {removed} oldest recording(s), "
        f"{used} of {config.limit_bytes} bytes in use"
    )
    return removed


# --- ingest (voice, scan) ----------------------------------------------------


class IngestError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def token_kinds(config: Settings, presented: str | None) -> set[str]:
    """Kinds the presented secret may ingest: the environment secret all, the OCR token scans."""
    if not presented:
        return set()
    kinds = set()
    if config.ingest_token and secrets.compare_digest(
        config.ingest_token.encode(), presented.encode("utf-8", "surrogateescape")
    ):
        kinds |= {"voice", "scan"}
    if config.ocr_token and secrets.compare_digest(
        config.ocr_token.encode(), presented.encode("utf-8", "surrogateescape")
    ):
        kinds |= {"scan"}
    return kinds


def ingest(config: Settings, *, kind: str, data: bytes, mime: str, fields: dict) -> tuple[TaklabRecording, bool]:
    """Create a voice or scan recording; (recording, created). Same source_ref = same row."""
    if kind not in ("voice", "scan"):
        raise IngestError("kind must be voice or scan")
    allowed = INGEST_MIMES[kind]
    if mime not in allowed:
        raise IngestError(f"unsupported type {mime} for {kind}", 415)
    if not data or not any(data.startswith(magic) for magic in allowed[mime]):
        raise IngestError("file content does not match its type", 415)

    source_ref = (fields.get("source_ref") or "").strip()[:255] or None
    if source_ref:
        existing = db.session.execute(
            select(TaklabRecording).where(TaklabRecording.source_ref == source_ref)
        ).scalars().first()
        if existing is not None:
            return existing, False

    callsign = (fields.get("callsign") or "").strip() or None
    account = (fields.get("account") or "").strip() or None
    if fields.get("author") and not account:
        parsed_callsign, account = parse_author(fields.get("author"))
        callsign = callsign or parsed_callsign
    user = resolve_author(account, callsign)

    started = parse_time(fields.get("started_at")) or utcnow()
    ended = parse_time(fields.get("ended_at"))
    if ended is None and fields.get("duration_s") not in (None, ""):
        try:
            ended = started + timedelta(seconds=max(0.0, float(fields["duration_s"])))
        except ValueError:
            raise IngestError("duration_s must be a number")
    if ended is None:
        ended = started
    if ended < started:
        raise IngestError("ended_at before started_at")

    rec = TaklabRecording(
        id=str(uuid.uuid4()),
        kind=kind,
        account=user.username if user is not None else account,
        callsign=callsign,
        started_at=started,
        ended_at=ended,
        created_at=utcnow(),
        state=STATE_READY,
        mime=mime,
        text=(fields.get("text") or None),
        source_ref=source_ref,
        deleted=False,
    )
    name = (fields.get("name") or "").strip()
    rec.name = name[:200] if name else default_name(rec, config)
    locate(rec, user, config)
    rec.track = json.dumps(build_track(rec.device_uid, rec.started_at, rec.ended_at))
    rec.file_path = write_file(config, kind, rec.id, data, mime)
    rec.size_bytes = len(data)
    db.session.add(rec)
    db.session.commit()
    publish_marker(rec, config)
    db.session.add(rec)
    db.session.commit()
    logger.info(f"taklab recordings: {kind} {rec.id} from {rec.account or '?'} ({len(data)} B)")
    enforce_space(config)
    return rec, True


def default_name(rec: TaklabRecording, config: Settings) -> str:
    when = _local(rec.started_at, config).strftime("%d.%m %H:%M")
    who = rec.callsign or rec.account or ""
    return f"{KIND_NAMES.get(rec.kind, 'Nagranie')} {when}" + (f" {who}" if who else "")


# --- video from MediaMTX -----------------------------------------------------


def account_path(path: str | None):
    """The account of a MediaMTX path, when the path is an account and that account published it."""
    from opentakserver.models.VideoStream import VideoStream

    if not path or "/" in path:
        return None
    user = find_user(path)
    if user is None or not user.active:
        return None
    stream = db.session.execute(select(VideoStream).where(VideoStream.path == path)).scalars().first()
    if stream is not None and stream.username and stream.username != user.username:
        return None
    return user


def segment_start(segment_path: str, duration_s: float | None) -> datetime:
    match = _SEGMENT_TIME.search(Path(segment_path).name)
    if match:
        base = datetime.strptime(match.group(1), "%Y-%m-%d_%H-%M-%S")
        return base + timedelta(microseconds=int(match.group(2).ljust(6, "0")[:6]))
    return utcnow() - timedelta(seconds=duration_s or 0)


def _probe_duration(path: Path) -> float | None:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        return float(out.stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def open_video(path: str) -> TaklabRecording | None:
    return db.session.execute(
        select(TaklabRecording)
        .where(
            TaklabRecording.kind == "video",
            TaklabRecording.source_path == path,
            TaklabRecording.state == STATE_RECORDING,
            TaklabRecording.deleted.is_(False),
        )
        .order_by(TaklabRecording.started_at.desc())
    ).scalars().first()


def video_segment_complete(config: Settings, path: str, segment_path: str, duration: str | None) -> str:
    """A finished MediaMTX segment: keep it in a recording or delete it. Returns what happened."""
    segment = Path(segment_path)
    relative = relative_to_root(config, segment)
    if relative is None:
        logger.warning("taklab recordings: segment outside the recordings directory ignored")
        return "outside"
    user = account_path(path)
    if user is None:
        segment.unlink(missing_ok=True)
        return "not-an-account"
    try:
        seconds = float(duration) if duration not in (None, "") else None
    except ValueError:
        seconds = None
    if seconds is None:
        seconds = _probe_duration(segment) or 0.0
    start = segment_start(segment_path, seconds)
    end = start + timedelta(seconds=seconds)
    size = segment.stat().st_size if segment.exists() else 0

    rec = open_video(path)
    if rec is not None and rec.ended_at is not None and (start - rec.ended_at).total_seconds() > config.join_gap_s:
        finalize_video(rec, config)
        rec = None
    created = rec is None
    if created:
        rec = TaklabRecording(
            id=str(uuid.uuid4()),
            kind="video",
            account=user.username,
            started_at=start,
            created_at=utcnow(),
            state=STATE_RECORDING,
            mime="video/mp4",
            source_path=path,
            segments="[]",
            size_bytes=0,
            deleted=False,
        )
        locate(rec, user, config)
        rec.name = default_name(rec, config)
    segments = rec.segment_list()
    if relative not in segments:
        segments.append(relative)
        rec.size_bytes = (rec.size_bytes or 0) + size
    rec.segments = json.dumps(segments)
    rec.ended_at = max(end, rec.ended_at or end)
    rec.last_activity_at = utcnow()
    rec.track = json.dumps(build_track(rec.device_uid, rec.started_at, rec.ended_at))
    db.session.add(rec)
    db.session.commit()
    publish_marker(rec, config)
    db.session.add(rec)
    db.session.commit()
    enforce_space(config)
    return "created" if created else "extended"


def video_activity(path: str) -> None:
    """ready / notready: the publication is alive or just ended - the grace period starts now."""
    rec = open_video(path)
    if rec is not None:
        rec.last_activity_at = utcnow()
        db.session.add(rec)
        db.session.commit()


def _stream_ready(path: str) -> bool:
    from opentakserver.models.VideoStream import VideoStream

    stream = db.session.execute(select(VideoStream).where(VideoStream.path == path)).scalars().first()
    return bool(stream and stream.ready)


def finalize_video(rec: TaklabRecording, config: Settings) -> None:
    """Join the segments into one MP4 (stream copy) and mark the recording ready."""
    segments = [absolute(config, s) for s in rec.segment_list()]
    segments = [s for s in segments if s is not None and s.exists()]
    if not segments:
        rec.state = STATE_ERROR
        db.session.add(rec)
        db.session.commit()
        return
    day = rec.started_at.strftime("%Y/%m/%d")
    relative = f"video/{day}/{rec.id}.mp4"
    target = config.root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".part.mp4")
    ok = False
    if len(segments) == 1:
        try:
            shutil.move(str(segments[0]), tmp)
            ok = True
        except OSError as e:
            logger.warning(f"taklab recordings: cannot move segment of {rec.id}: {e}")
    else:
        listing = target.with_suffix(".txt")
        listing.write_text("".join(f"file '{s}'\n" for s in segments))
        try:
            result = subprocess.run(
                ["ffmpeg", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(listing),
                 "-c", "copy", "-movflags", "+faststart", str(tmp)],
                capture_output=True,
                timeout=1800,
            )
            ok = result.returncode == 0
            if not ok:
                logger.warning(f"taklab recordings: ffmpeg could not join {rec.id}: {result.stderr[-300:]!r}")
        except (OSError, subprocess.SubprocessError) as e:
            logger.warning(f"taklab recordings: ffmpeg could not join {rec.id}: {e}")
        finally:
            listing.unlink(missing_ok=True)
    if not ok:
        tmp.unlink(missing_ok=True)
        # Segments stay; the file route plays them through MediaMTX playback.
        rec.state = STATE_ERROR
        db.session.add(rec)
        db.session.commit()
        return
    tmp.replace(target)
    for segment in segments:
        segment.unlink(missing_ok=True)
    rec.file_path = relative
    rec.size_bytes = target.stat().st_size
    rec.segments = None
    rec.state = STATE_READY
    rec.track = json.dumps(build_track(rec.device_uid, rec.started_at, rec.ended_at))
    db.session.add(rec)
    db.session.commit()
    publish_marker(rec, config)
    db.session.add(rec)
    db.session.commit()
    logger.info(f"taklab recordings: video {rec.id} ready, {len(segments)} segment(s)")


def finalize_idle_videos(config: Settings, now: datetime | None = None) -> int:
    now = now or utcnow()
    done = 0
    for rec in db.session.execute(
        select(TaklabRecording).where(
            TaklabRecording.kind == "video",
            TaklabRecording.state == STATE_RECORDING,
            TaklabRecording.deleted.is_(False),
        )
    ).scalars().all():
        last = rec.last_activity_at or rec.ended_at or rec.started_at
        if (now - last).total_seconds() <= config.join_gap_s:
            continue
        if _stream_ready(rec.source_path) and (now - last).total_seconds() < 3 * 3600:
            # Still publishing: the next segment (at most 10 min) extends it.
            continue
        finalize_video(rec, config)
        done += 1
    return done


# --- janitor -----------------------------------------------------------------


def janitor_step(config: Settings) -> None:
    with _janitor_lock:
        finalize_idle_videos(config)
        expire(config)
        enforce_space(config)


def start_janitor(app) -> threading.Thread | None:
    config = settings()
    if config is None:
        logger.info("taklab recordings: disabled (no OTS_TAKLAB_RECORDINGS_DIR)")
        return None

    def loop():
        while True:
            time.sleep(max(10.0, config.janitor_s))
            try:
                with app.app_context():
                    janitor_step(config)
            except Exception:  # noqa: BLE001 - the loop must not die
                logger.exception("taklab recordings: janitor step failed")
                try:
                    with app.app_context():
                        db.session.rollback()
                except Exception:  # noqa: BLE001
                    pass

    thread = threading.Thread(target=loop, name="taklab-recordings-janitor", daemon=True)
    thread.start()
    logger.info(f"taklab recordings: janitor every {config.janitor_s:.0f} s, {config.root}")
    return thread


__all__ = ["KINDS", "settings", "Settings"]
