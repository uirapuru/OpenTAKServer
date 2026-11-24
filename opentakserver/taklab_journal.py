"""taklab: environment journal - who added what, who went where, who shared
video, who wrote what to whom, which files and recordings appeared.

Design: ~/taklab docs/superpowers/specs/2026-11-24-journal-design.md.

Writers (each one is a single line in the code it hooks into):
    * record_cot        - cot_parser.on_message, after route_cot
    * record_disconnect - cot_parser.on_message, "disconnected" body
    * record_file_upload - /Marti/sync/missionupload and /Marti/sync/upload
    * recordings        - SQLAlchemy listeners on TaklabRecording below
                          (insert -> recording_*, deleted False->True -> recording_delete)
    * record_export     - the journal API, every PDF/ZIP export

A writer never raises: every error is logged and swallowed, so the journal can
never stop CoT processing or an upload. Writers use their own short database
connection (engine.begin()), never the caller's ORM session.

Classification of a CoT (classify): see the table in the design. State that
turns a CoT into "add", "change", a new position or nothing lives in memory
per process (_State), with a look-up of the newest journal row on a miss, so a
restart does not repeat additions. With several cot_parser processes each
keeps its own state; positions may then be logged a bit more often than the
50 m / 5 min rule says, never less.

No secret is stored: the raw XML is not kept, video addresses lose the
user:password@ part and the ?query, file entries keep the hash and the name.
"""

import hashlib
import json
import math
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from sqlalchemy import event as sa_event
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import insert, select

from opentakserver.extensions import db, logger
from opentakserver.models.TaklabJournal import KINDS, TaklabJournal

POSITION_MIN_DISTANCE_M = 50.0
POSITION_MIN_INTERVAL_S = 300.0
DISCONNECT_DEDUP_S = 30.0
MAX_TEXT = 20000
MAX_TITLE = 500
MAX_RECIPIENTS = 50
CACHE_LIMIT = 50000
NAME_TTL_S = 600.0
ALL_CHAT_ROOMS = "All Chat Rooms"
RECORDING_MARKER_PREFIX = "taklab-recording-"

OBJECT_PREFIXES = ("a-", "u-d-", "b-m-p-", "u-r-b-")
OBJECT_TYPES = ("b-m-r", "u-rb-a")
KIND_LABELS = {
    "object_add": "Dodanie obiektu",
    "object_change": "Zmiana obiektu",
    "object_delete": "Usunięcie obiektu",
    "position": "Pozycja",
    "disconnect": "Rozłączenie",
    "chat": "Wiadomość",
    "video_share": "Udostępnienie wideo",
    "file_upload": "Plik na serwerze",
    "file_send": "Wysłanie pliku",
    "alert": "Alarm",
    "casevac": "CASEVAC",
    "recording_video": "Nagranie wideo",
    "recording_voice": "Głosówka",
    "recording_scan": "Skan dokumentu",
    "recording_delete": "Usunięcie nagrania",
    "journal_export": "Eksport dziennika",
}
ALERT_NAMES = {
    "b-a-o-tbl": "911",
    "b-a-o-pan": "Ring the Bell",
    "b-a-o-opn": "Troops in Contact",
    "b-a-g": "Geofence",
    "b-a-o-c": "Alarm",
    "b-a-o-can": "Odwołanie alarmu",
}
RECORDING_KINDS = {"video": "recording_video", "voice": "recording_voice", "scan": "recording_scan"}
RECORDING_NAMES = {"video": "Nagranie wideo", "voice": "Głosówka", "scan": "Skan dokumentu"}

_lock = threading.Lock()


# --- settings and time -------------------------------------------------------


@dataclass(frozen=True)
class Settings:
    timezone: str
    font_dir: str
    pdf_limit: int


def settings() -> Settings:
    def env(name: str, default: str = "") -> str:
        return (os.environ.get(name) or "").strip() or default

    try:
        pdf_limit = int(env("OTS_TAKLAB_JOURNAL_PDF_LIMIT", "20000"))
    except ValueError:
        pdf_limit = 20000
    return Settings(
        timezone=env(
            "OTS_TAKLAB_JOURNAL_TIMEZONE", env("OTS_TAKLAB_RECORDINGS_TIMEZONE", "Europe/Warsaw")
        ),
        font_dir=env("OTS_TAKLAB_JOURNAL_FONT_DIR"),
        pdf_limit=max(1, pdf_limit),
    )


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _now() -> datetime:
    """Server time of reception (naive UTC); tests replace it."""
    return utcnow()


def local(moment: datetime, zone_name: str) -> datetime:
    try:
        zone = ZoneInfo(zone_name)
    except Exception:  # noqa: BLE001 - a bad zone name falls back to UTC
        zone = timezone.utc
    return moment.replace(tzinfo=timezone.utc).astimezone(zone)


# --- small helpers -------------------------------------------------------------


def distance_m(lat1, lon1, lat2, lon2) -> float:
    radius = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius * math.asin(min(1.0, math.sqrt(a)))


def clean_url(raw: str | None) -> str | None:
    """Address of a stream without user:password@, ?query and #fragment."""
    if not raw:
        return None
    text = str(raw).strip()
    try:
        parts = urlsplit(text)
    except ValueError:
        return None
    if "://" not in text:
        # No scheme: cut credentials and the query by hand.
        text = text.split("?", 1)[0].split("#", 1)[0]
        return text.rsplit("@", 1)[-1] or None
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    try:
        port = parts.port
    except ValueError:
        port = None
    netloc = f"{host}:{port}" if port else host
    return urlunsplit((parts.scheme, netloc, parts.path, "", "")) or None


def _attr(tag, name, default=None):
    if tag is None:
        return default
    value = tag.attrs.get(name)
    return value if value not in (None, "") else default


def _float(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _point(event) -> tuple[float | None, float | None]:
    point = event.find("point")
    lat, lon = _float(_attr(point, "lat")), _float(_attr(point, "lon"))
    if lat is None or lon is None or abs(lat) > 90 or abs(lon) > 180 or (lat == 0 and lon == 0):
        return None, None
    return lat, lon


def _text(tag) -> str | None:
    if tag is None:
        return None
    value = tag.get_text()
    value = value.strip() if value else ""
    return value[:MAX_TEXT] or None


def _title(value: str) -> str:
    value = " ".join(str(value).split())
    return value[:MAX_TITLE]


def _fingerprint(*parts) -> str:
    raw = "\x1f".join("" if p is None else str(p) for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _round(value: float | None) -> str:
    # 4 decimal places ~ 11 m in latitude
    return "" if value is None else f"{value:.4f}"


# --- classification (no database) ---------------------------------------------


def is_object_type(cot_type: str) -> bool:
    return cot_type in OBJECT_TYPES or cot_type.startswith(OBJECT_PREFIXES)


def _destinations(event) -> list[dict]:
    found = []
    for dest in event.find_all("dest"):
        callsign, uid = _attr(dest, "callsign"), _attr(dest, "uid")
        if callsign or uid:
            found.append({"callsign": callsign, "uid": uid})
    return found


def _mission(event) -> str | None:
    for dest in event.find_all("dest"):
        if _attr(dest, "mission"):
            return dest.attrs["mission"][:255]
    return None


def _object_fingerprint(event, cot_type, name, lat, lon) -> str:
    vertices = []
    for link in event.find_all("link"):
        point = _attr(link, "point")
        if point:
            bits = point.split(",")
            vertices.append(
                ",".join(_round(_float(b)) for b in bits[:2])
            )
    return _fingerprint(cot_type, name, _round(lat), _round(lon), ";".join(vertices))


def classify(event, sender_uid: str | None) -> dict | None:
    """What a CoT means for the journal: a draft entry or None (noise).

    The draft "kind" is final except for "object" (resolved to object_add,
    object_change or nothing by the state) and the repeat checks.
    """
    if event is None:
        return None
    cot_type = event.attrs.get("type") or ""
    uid = event.attrs.get("uid") or ""
    if event.find("__taklab_recording") is not None:
        return None
    lat, lon = _point(event)
    contact = event.find("contact")
    callsign = _attr(contact, "callsign")
    mission = _mission(event)

    if cot_type == "t-x-d-d":
        link = event.find("link")
        target = _attr(link, "uid")
        if not target or target.startswith(RECORDING_MARKER_PREFIX):
            return None
        if sender_uid and target == sender_uid:
            return {"kind": "disconnect", "device_uid": sender_uid}
        return {
            "kind": "object_delete",
            "object_uid": target[:255],
            "object_type": _attr(link, "type"),
            "mission": mission,
        }

    if cot_type.startswith("t-") or cot_type.startswith("y-"):
        # pings (t-x-c-t), TAK protocol (t-x-takp*), mission changes, snapshots
        return None

    if cot_type == "b-t-f":
        return _classify_chat(event, uid, mission)

    if cot_type.startswith("b-t-f"):
        return None  # delivery and read receipts

    if cot_type == "b-f-t-r":
        share = event.find("fileshare")
        filename = _attr(share, "filename") or _attr(share, "name") or "plik"
        file_hash = (_attr(share, "sha256") or "").lower()
        size = _float(_attr(share, "sizeInBytes"))
        return {
            "kind": "file_send",
            "object_uid": uid[:255],
            "object_type": cot_type,
            "title_name": filename,
            "recipients_raw": _destinations(event),
            "lat": lat,
            "lon": lon,
            "mission": mission,
            "file": {
                "source": "data_package",
                "ref": file_hash if re.fullmatch(r"[0-9a-f]{64}", file_hash) else None,
                "filename": filename[:255],
                "size": int(size) if size is not None else None,
                "mime": "application/zip",
            },
            "repeat_key": uid,
        }

    video = event.find("__video")
    if cot_type == "b-i-v" or (video is not None and not is_self(uid, sender_uid)):
        entry = event.find("ConnectionEntry")
        if cot_type != "b-i-v" and entry is None and not _attr(video, "url"):
            video = None
        else:
            return _classify_video(event, uid, cot_type, callsign, video, entry, lat, lon, mission)

    if cot_type.startswith("b-a-"):
        emergency = event.find("emergency")
        label = ALERT_NAMES.get(cot_type) or _attr(emergency, "type") or "Alarm"
        text = _text(emergency) or _text(event.find("remarks"))
        return {
            "kind": "alert",
            "object_uid": uid[:255],
            "object_type": cot_type,
            "title_name": label,
            "object_name": callsign or _text(emergency),
            "text": text,
            "lat": lat,
            "lon": lon,
            "mission": mission,
            "fingerprint": _fingerprint(cot_type, label, text),
        }

    if cot_type == "b-r-f-h-c":
        text = _text(event.find("remarks"))
        return {
            "kind": "casevac",
            "object_uid": uid[:255],
            "object_type": cot_type,
            "object_name": callsign or uid,
            "text": text,
            "lat": lat,
            "lon": lon,
            "mission": mission,
            "fingerprint": _fingerprint(cot_type, callsign, _round(lat), _round(lon), text),
        }

    if cot_type.startswith("a-") and (
        is_self(uid, sender_uid) or event.find("takv") is not None or event.find("__group") is not None
    ):
        # Own position report; also a device of the federation partner (it has takv/__group).
        if lat is None:
            return None
        return {
            "kind": "position",
            "device_uid": uid[:255],
            "device_callsign": callsign,
            "object_type": cot_type,
            "lat": lat,
            "lon": lon,
        }

    if is_object_type(cot_type) and not is_self(uid, sender_uid):
        return {
            "kind": "object",
            "object_uid": uid[:255],
            "object_type": cot_type,
            "object_name": callsign or uid,
            "text": _text(event.find("remarks")),
            "lat": lat,
            "lon": lon,
            "mission": mission,
            "fingerprint": _object_fingerprint(event, cot_type, callsign, lat, lon),
        }
    return None


def is_self(uid: str | None, sender_uid: str | None) -> bool:
    return bool(uid) and bool(sender_uid) and uid == sender_uid


def _classify_chat(event, uid, mission) -> dict | None:
    chat = event.find("__chat")
    remarks = event.find("remarks")
    if chat is None:
        return None
    chatroom = _attr(chat, "chatroom") or _attr(chat, "id")
    recipients = _destinations(event)
    group = chat.find("chatgrp") if chat is not None else None
    group = group or event.find("chatgrp")
    room_id = _attr(chat, "id")
    if group is not None:
        for key in sorted(group.attrs):
            if not re.fullmatch(r"uid[1-9][0-9]*", key):
                continue
            value = group.attrs[key]
            if not value or value == ALL_CHAT_ROOMS:
                continue
            if not any(r.get("uid") == value for r in recipients):
                recipients.append({"callsign": None, "uid": value})
    public = chatroom == ALL_CHAT_ROOMS or room_id == ALL_CHAT_ROOMS
    return {
        "kind": "chat",
        "object_uid": uid[:255],
        "object_type": "b-t-f",
        "sender_callsign": _attr(chat, "senderCallsign"),
        "chatroom": (chatroom or "")[:255] or None,
        "text": _text(remarks),
        "recipients_raw": [] if public else recipients,
        "private": bool(recipients) and not public,
        "mission": mission,
        "lat": None,
        "lon": None,
        "repeat_key": uid,
    }


def _classify_video(event, uid, cot_type, callsign, video, entry, lat, lon, mission) -> dict:
    address = None
    alias = None
    if entry is not None:
        protocol = _attr(entry, "protocol", "rtsp")
        host = _attr(entry, "address", "")
        port = _attr(entry, "port")
        path = _attr(entry, "path", "")
        alias = _attr(entry, "alias")
        if host:
            if not path.startswith("/") and path:
                path = "/" + path
            netloc = f"{host}:{port}" if port and str(port) not in ("-1", "0") else host
            address = f"{protocol}://{netloc}{path}"
    if address is None:
        address = _attr(video, "url")
    address = clean_url(address)
    name = alias or callsign or uid
    return {
        "kind": "video_share",
        "object_uid": uid[:255],
        "object_type": cot_type,
        "title_name": name,
        "text": address,
        "recipients_raw": _destinations(event),
        "lat": lat,
        "lon": lon,
        "mission": mission,
        "fingerprint": _fingerprint(name, address),
    }


# --- per-process state ---------------------------------------------------------


class _State:
    def __init__(self):
        self.objects: dict[str, str] = {}  # object uid -> fingerprint, "" when deleted
        self.positions: dict[str, tuple[float, float, datetime]] = {}
        self.disconnects: dict[str, datetime] = {}
        self.repeats: dict[tuple[str, str], str] = {}  # (kind, uid) -> fingerprint
        self.users: dict[int, tuple[str | None, float]] = {}
        self.devices: dict[str, tuple[tuple | None, float]] = {}
        self.callsigns: dict[str, tuple[tuple | None, float]] = {}

    @staticmethod
    def put(store: dict, key, value) -> None:
        if len(store) >= CACHE_LIMIT and key not in store:
            store.clear()
        store[key] = value


_state = _State()


def reset_state() -> None:
    """Forget the in-memory state (tests, and nothing else)."""
    global _state
    with _lock:
        _state = _State()


# --- identity look-ups ----------------------------------------------------------


def _user_name(conn, user_id) -> str | None:
    if not user_id:
        return None
    try:
        user_id = int(user_id)
    except (TypeError, ValueError):
        return None
    cached = _state.users.get(user_id)
    if cached and time.monotonic() - cached[1] < NAME_TTL_S:
        return cached[0]
    from opentakserver.models.user import User

    name = conn.execute(select(User.__table__.c.username).where(User.__table__.c.id == user_id)).scalar()
    _State.put(_state.users, user_id, (name, time.monotonic()))
    return name


def _device(conn, uid: str | None) -> tuple | None:
    """(callsign, account) of an EUD by uid."""
    if not uid:
        return None
    cached = _state.devices.get(uid)
    if cached and time.monotonic() - cached[1] < NAME_TTL_S:
        return cached[0]
    from opentakserver.models.EUD import EUD

    row = conn.execute(select(EUD.__table__.c.callsign, EUD.__table__.c.user_id).where(EUD.__table__.c.uid == uid)).first()
    value = (row[0], _user_name(conn, row[1])) if row else None
    _State.put(_state.devices, uid, (value, time.monotonic()))
    return value


def _device_by_callsign(conn, callsign: str | None) -> tuple | None:
    """(uid, account) of an EUD by callsign."""
    if not callsign:
        return None
    cached = _state.callsigns.get(callsign)
    if cached and time.monotonic() - cached[1] < NAME_TTL_S:
        return cached[0]
    from opentakserver.models.EUD import EUD

    table = EUD.__table__
    row = conn.execute(select(table.c.uid, table.c.user_id).where(table.c.callsign == callsign)).first()
    value = (row[0], _user_name(conn, row[1])) if row else None
    _State.put(_state.callsigns, callsign, (value, time.monotonic()))
    return value


def _recipients(conn, raw: list[dict]) -> list[dict]:
    result = []
    seen = set()
    for item in raw[:MAX_RECIPIENTS]:
        callsign, uid, account = item.get("callsign"), item.get("uid"), None
        if uid:
            found = _device(conn, uid)
            if found:
                callsign = callsign or found[0]
                account = found[1]
        elif callsign:
            found = _device_by_callsign(conn, callsign)
            if found:
                uid, account = found
        key = uid or f"callsign:{callsign}"
        if key in seen:
            continue
        seen.add(key)
        result.append({"callsign": callsign, "uid": uid, "account": account})
    return result


# --- state resolution -------------------------------------------------------------


def _last_row(conn, column, value, kinds):
    table = TaklabJournal.__table__
    return conn.execute(
        select(table.c.kind, table.c.fingerprint, table.c.latitude, table.c.longitude, table.c.at)
        .where(column(table) == value, table.c.kind.in_(kinds))
        .order_by(table.c.id.desc())
        .limit(1)
    ).first()


def _resolve(conn, draft: dict, now: datetime) -> str | None:
    """Final kind of the draft, or None when it repeats what is already logged."""
    kind = draft["kind"]
    if kind == "object":
        uid = draft["object_uid"]
        known = _state.objects.get(uid)
        if known is None:
            row = _last_row(conn, lambda t: t.c.object_uid, uid, ("object_add", "object_change", "object_delete"))
            if row is not None:
                known = "" if row.kind == "object_delete" else (row.fingerprint or "?")
        if known == draft["fingerprint"]:
            return None
        final = "object_add" if not known else "object_change"
        _State.put(_state.objects, uid, draft["fingerprint"])
        return final

    if kind == "object_delete":
        uid = draft["object_uid"]
        known = _state.objects.get(uid)
        if known is None:
            row = _last_row(conn, lambda t: t.c.object_uid, uid, ("object_add", "object_change", "object_delete"))
            if row is not None:
                known = "" if row.kind == "object_delete" else (row.fingerprint or "?")
        if known == "":
            return None  # already deleted
        _State.put(_state.objects, uid, "")
        return kind

    if kind == "position":
        device = draft["device_uid"]
        last = _state.positions.get(device)
        if last is None:
            row = _last_row(conn, lambda t: t.c.device_uid, device, ("position",))
            if row is not None and row.latitude is not None:
                last = (row.latitude, row.longitude, row.at)
        if last is not None:
            moved = distance_m(last[0], last[1], draft["lat"], draft["lon"])
            elapsed = (now - last[2]).total_seconds()
            if moved < POSITION_MIN_DISTANCE_M and elapsed < POSITION_MIN_INTERVAL_S:
                return None
        _State.put(_state.positions, device, (draft["lat"], draft["lon"], now))
        return kind

    if kind == "disconnect":
        device = draft.get("device_uid") or ""
        last = _state.disconnects.get(device)
        if last is not None and (now - last).total_seconds() < DISCONNECT_DEDUP_S:
            return None
        _State.put(_state.disconnects, device, now)
        return kind

    key_value = draft.get("repeat_key") or draft.get("fingerprint")
    if key_value and draft.get("object_uid"):
        key = (kind, draft["object_uid"])
        fingerprint = draft.get("fingerprint") or key_value
        known = _state.repeats.get(key)
        if known is None:
            row = _last_row(conn, lambda t: t.c.object_uid, draft["object_uid"], (kind,))
            if row is not None:
                known = row.fingerprint or ""
        if known == fingerprint:
            return None
        _State.put(_state.repeats, key, fingerprint)
        draft["fingerprint"] = fingerprint
    return kind


def _title_for(kind: str, draft: dict, callsign: str | None, recipients: list[dict]) -> str:
    name = draft.get("object_name") or draft.get("title_name") or ""
    who = ", ".join(r.get("callsign") or r.get("uid") or "?" for r in recipients)
    if kind == "object_add":
        return f"Dodanie obiektu {name}"
    if kind == "object_change":
        return f"Zmiana obiektu {name}"
    if kind == "object_delete":
        return f"Usunięcie obiektu {name}".strip()
    if kind == "position":
        return f"Pozycja {callsign or draft.get('device_uid') or ''}".strip()
    if kind == "disconnect":
        return f"Rozłączenie {callsign or draft.get('device_uid') or ''}".strip()
    if kind == "chat":
        if draft.get("private"):
            return f"Wiadomość prywatna do {who}"
        return f"Wiadomość w pokoju {draft.get('chatroom') or ALL_CHAT_ROOMS}"
    if kind == "video_share":
        return f"Udostępnienie wideo {name}" + (f" do {who}" if who else "")
    if kind == "file_send":
        return f"Wysłanie pliku {name}" + (f" do {who}" if who else "")
    if kind == "alert":
        return f"Alarm: {name}" + (f" ({draft['object_name']})" if draft.get("object_name") else "")
    if kind == "casevac":
        return f"CASEVAC {name}"
    return KIND_LABELS.get(kind, kind)


def _insert(conn, values: dict) -> None:
    if values.get("title") is not None:
        values["title"] = _title(values["title"])
    for key in ("recipients", "file"):
        if values.get(key) is not None and not isinstance(values[key], str):
            values[key] = json.dumps(values[key], ensure_ascii=False)
    conn.execute(insert(TaklabJournal.__table__).values(**values))


def _connection():
    engine = db.engine
    conn = engine.connect()
    if engine.dialect.name == "postgresql":
        trans = conn.begin()
        conn.exec_driver_sql("SET LOCAL statement_timeout = 3000")
        return conn, trans
    return conn, conn.begin()


class _Short:
    """Own short connection and transaction, committed on success."""

    def __enter__(self):
        self.conn, self.trans = _connection()
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self.trans.commit()
            else:
                self.trans.rollback()
        finally:
            self.conn.close()
        return False


class _NullContext:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


# --- writers ----------------------------------------------------------------------


def record_cot(event, sender_uid: str | None, user_id, context=None) -> None:
    """Journal entry for one CoT received by the server. Never raises."""
    try:
        draft = classify(event, sender_uid)
        if draft is None:
            return
        with context if context is not None else _NullContext():
            with _lock, _Short() as conn:
                _record_draft(conn, draft, sender_uid, user_id)
    except Exception as e:  # noqa: BLE001 - the journal must never stop CoT processing
        logger.warning(f"taklab journal: CoT not logged ({type(e).__name__}: {e})")


def _record_draft(conn, draft: dict, sender_uid, user_id) -> None:
    now = _now()
    kind = _resolve(conn, draft, now)
    if kind is None:
        return
    account = _user_name(conn, user_id)
    sender = _device(conn, sender_uid)
    callsign = sender[0] if sender else None
    device_uid = sender_uid
    if kind == "position":
        device_uid = draft["device_uid"]
        callsign = draft.get("device_callsign") or callsign
    elif kind == "chat":
        callsign = draft.get("sender_callsign") or callsign
    elif kind == "disconnect":
        device_uid = draft.get("device_uid") or sender_uid
        found = _device(conn, device_uid)
        callsign = found[0] if found else callsign
        account = account or (found[1] if found else None)
    if kind == "object_delete":
        draft["object_name"] = _known_name(conn, draft["object_uid"])
    recipients = _recipients(conn, draft.get("recipients_raw") or [])
    _insert(
        conn,
        {
            "at": now,
            "kind": kind,
            "account": account,
            "callsign": (callsign or None) and callsign[:255],
            "device_uid": device_uid,
            "object_uid": draft.get("object_uid"),
            "object_type": (draft.get("object_type") or None) and draft["object_type"][:100],
            "title": _title_for(kind, draft, callsign, recipients),
            "text": draft.get("text"),
            "private": bool(draft.get("private")),
            "recipients": recipients or None,
            "chatroom": draft.get("chatroom"),
            "latitude": draft.get("lat"),
            "longitude": draft.get("lon"),
            "mission": draft.get("mission"),
            "file": draft.get("file"),
            "fingerprint": draft.get("fingerprint"),
        },
    )


def _known_name(conn, object_uid: str) -> str:
    """Name of a deleted object from its last journal entry ("Dodanie obiektu X")."""
    table = TaklabJournal.__table__
    title = conn.execute(
        select(table.c.title)
        .where(table.c.object_uid == object_uid, table.c.kind.in_(("object_add", "object_change")))
        .order_by(table.c.id.desc())
        .limit(1)
    ).scalar()
    if not title:
        return ""
    for prefix in ("Dodanie obiektu ", "Zmiana obiektu "):
        if title.startswith(prefix):
            return title[len(prefix):]
    return title


def record_disconnect(device_uid: str | None, user_id, context=None) -> None:
    """The EUD handler reported a closed connection. Never raises."""
    try:
        if not device_uid:
            return
        with context if context is not None else _NullContext():
            with _lock, _Short() as conn:
                _record_draft(conn, {"kind": "disconnect", "device_uid": device_uid}, device_uid, user_id)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"taklab journal: disconnect not logged ({type(e).__name__}: {e})")


def _request_account() -> tuple[str | None, str | None]:
    """(account, None) of the current request: client certificate first, then the session."""
    try:
        from cryptography.x509.oid import NameOID

        from opentakserver.blueprints.marti_api.marti_api import verify_client_cert

        cert = verify_client_cert()
        if cert is not None:
            return cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value, None
    except Exception:  # noqa: BLE001
        pass
    try:
        from flask_login import current_user

        if current_user and current_user.is_authenticated:
            return current_user.username, None
    except Exception:  # noqa: BLE001
        pass
    return None, None


def record_file_upload(
    source: str,
    ref: str | None,
    filename: str | None,
    size: int | None,
    mime: str | None,
    account: str | None = None,
    device_uid: str | None = None,
) -> None:
    """A file accepted by the server (data package or mission content). Never raises."""
    try:
        if account is None:
            account, _ = _request_account()
        if device_uid is None:
            try:
                from flask import request

                device_uid = request.args.get("creatorUid") or request.args.get("CreatorUid")
            except Exception:  # noqa: BLE001
                device_uid = None
        name = (filename or "plik")[:255]
        with _lock, _Short() as conn:
            found = _device(conn, device_uid)
            _insert(
                conn,
                {
                    "at": _now(),
                    "kind": "file_upload",
                    "account": account,
                    "callsign": found[0] if found else None,
                    "device_uid": (device_uid or None) and device_uid[:255],
                    "object_uid": ref,
                    "title": f"Plik na serwerze: {name}",
                    "file": {
                        "source": source,
                        "ref": ref,
                        "filename": name,
                        "size": size,
                        "mime": mime,
                    },
                },
            )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"taklab journal: upload not logged ({type(e).__name__}: {e})")


def record_data_package(
    file_hash: str | None, account: str | None = None, device_uid: str | None = None
) -> None:
    """A data package stored by save_data_package_file / create_data_package_zip. Never raises."""
    try:
        if not file_hash:
            return
        from opentakserver.models.DataPackage import DataPackage

        table = DataPackage.__table__
        with _Short() as conn:
            row = conn.execute(
                select(table.c.filename, table.c.size, table.c.mime_type).where(
                    table.c.hash == file_hash
                )
            ).first()
        filename, size, mime = (row[0], row[1], row[2]) if row else (None, None, None)
        record_file_upload(
            "data_package", file_hash, filename, size, mime or "application/zip", account, device_uid
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"taklab journal: data package not logged ({type(e).__name__}: {e})")


def record_export(account: str, fmt: str, filters: dict, entries: int) -> None:
    try:
        with _lock, _Short() as conn:
            _insert(
                conn,
                {
                    "at": _now(),
                    "kind": "journal_export",
                    "account": account,
                    "title": f"Eksport dziennika ({fmt.upper()})",
                    "text": json.dumps(
                        {"format": fmt, "filter": filters, "entries": entries}, ensure_ascii=False
                    ),
                },
            )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"taklab journal: export not logged ({type(e).__name__}: {e})")


# --- recordings registry listeners ---------------------------------------------------

_PENDING = "taklab_journal_pending"


def _recording_file(rec) -> dict:
    name = rec.name or RECORDING_NAMES.get(rec.kind, "Nagranie")
    return {
        "source": "recording",
        "ref": rec.id,
        "filename": name[:255],
        "size": rec.size_bytes,
        "mime": rec.mime,
    }


def _queue(target, values: dict) -> None:
    from sqlalchemy.orm import object_session

    session = object_session(target)
    if session is None:
        return
    session.info.setdefault(_PENDING, []).append(values)


def _on_recording_insert(mapper, connection, target) -> None:
    try:
        kind = RECORDING_KINDS.get(target.kind)
        if kind is None:
            return
        name = target.name or RECORDING_NAMES.get(target.kind, "Nagranie")
        _queue(
            target,
            {
                "at": _now(),
                "kind": kind,
                "account": target.account,
                "callsign": target.callsign,
                "device_uid": target.device_uid,
                "object_uid": target.id,
                "title": f"{RECORDING_NAMES.get(target.kind, 'Nagranie')}: {name}",
                "text": target.text,
                "latitude": target.latitude,
                "longitude": target.longitude,
                "file": _recording_file(target),
            },
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"taklab journal: recording not logged ({type(e).__name__}: {e})")


def _on_recording_update(mapper, connection, target) -> None:
    try:
        history = sa_inspect(target).attrs.deleted.history
        if not (target.deleted and True in (history.added or ())):
            return
        if history.deleted:
            was_deleted = bool(history.deleted[0])
        else:
            # Old value not loaded (object expired after a commit): ask the database,
            # this runs before the UPDATE.
            from opentakserver.models.TaklabRecording import TaklabRecording

            table = TaklabRecording.__table__
            was_deleted = bool(
                connection.execute(select(table.c.deleted).where(table.c.id == target.id)).scalar()
            )
        if was_deleted:
            return
        actor, _ = _request_account()
        name = target.name or RECORDING_NAMES.get(target.kind, "Nagranie")
        _queue(
            target,
            {
                "at": _now(),
                "kind": "recording_delete",
                "account": actor,
                "callsign": None,
                "device_uid": None,
                "object_uid": target.id,
                "title": f"Usunięcie nagrania: {name}",
                "text": f"Autor nagrania: {target.account or 'nieznany'}"
                + ("" if actor else " (usunięte przez serwer: retencja albo brak miejsca)"),
                "latitude": target.latitude,
                "longitude": target.longitude,
                "file": _recording_file(target),
            },
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"taklab journal: recording delete not logged ({type(e).__name__}: {e})")


def _after_commit(session) -> None:
    pending = session.info.pop(_PENDING, None)
    if not pending:
        return
    try:
        with _lock, _Short() as conn:
            for values in pending:
                _insert(conn, dict(values))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"taklab journal: recordings not logged ({type(e).__name__}: {e})")


def _after_rollback(session) -> None:
    session.info.pop(_PENDING, None)


def _register_listeners() -> None:
    from sqlalchemy.orm import Session

    from opentakserver.models.TaklabRecording import TaklabRecording

    if not sa_event.contains(TaklabRecording, "after_insert", _on_recording_insert):
        sa_event.listen(TaklabRecording, "after_insert", _on_recording_insert)
        sa_event.listen(TaklabRecording, "before_update", _on_recording_update)
        sa_event.listen(Session, "after_commit", _after_commit)
        sa_event.listen(Session, "after_rollback", _after_rollback)


_register_listeners()

__all__ = [
    "KINDS",
    "classify",
    "clean_url",
    "record_cot",
    "record_disconnect",
    "record_export",
    "record_file_upload",
    "settings",
]
