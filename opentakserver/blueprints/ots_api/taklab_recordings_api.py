"""taklab: /api/taklab/recordings - recordings registry (contract in the design).

Who is asking, in this order:
  1. a client certificate forwarded by marti-tls in X-Ssl-Cert (CloudTAK calls
     https://<env>-ots:8443/api/taklab/recordings with the user's certificate),
  2. a Flask-Security session or Authentication-Token (OpenTAKServer-UI).

Internal ingest (voice register, OCR queue): POST /api/taklab/recordings/ingest
with "Authorization: Bearer <environment secret>". It is refused whenever the
request came through a proxy (X-Forwarded-For / X-Real-IP): the panel route
(Traefik -> ots-ui nginx -> 8081) always adds them, the in-cluster callers
never do. marti-tls does not route /ingest at all.

Access (user decision 24 XI 2026): every route except /ingest is for the
administrator role only - list, details, playback, download, ZIP archive,
rename and delete. Other accounts get 403. Map markers are not affected.
"""

import io
import os
import re
import zipfile
from datetime import datetime, timezone

import requests
from cryptography.x509.oid import NameOID
from flask import Blueprint, Response, jsonify, request, send_file, stream_with_context
from flask_security import current_user
from flask_security.decorators import _check_session, _check_token
from sqlalchemy import select

from opentakserver import taklab_recordings as rec_core
from opentakserver.blueprints.marti_api.marti_api import verify_client_cert
from opentakserver.extensions import db, logger
from opentakserver.models.TaklabRecording import KINDS, STATE_RECORDING, TaklabRecording

taklab_recordings_api = Blueprint("taklab_recordings_api", __name__)

PER_PAGE = 25
MAX_PER_PAGE = 100
MAX_NAME = 200

MSG_AUTH = "Wymagane logowanie."
MSG_OFF = "Rejestr nagrań nie jest włączony na tym serwerze."
MSG_NOT_FOUND = "Nie ma takiego nagrania."
MSG_ADMIN = "Nagrania są dostępne tylko dla administratora."
MSG_NOTHING = "Nie ma plików do pobrania."
MSG_TOO_MANY = "Za dużo nagrań w jednym archiwum."
MAX_ARCHIVE_IDS = 100
MAX_ARCHIVE_ALL = 5000
ARCHIVE_CHUNK = 256 * 1024
MSG_FILE = "Plik nagrania jest niedostępny."
MSG_IN_PROGRESS = "Nagranie jeszcze trwa, plik będzie gotowy po zakończeniu."


def _reply(body, status: int = 200) -> Response:
    response = jsonify(body)
    response.status_code = status
    response.headers["Cache-Control"] = "no-store"
    return response


def _error(message: str, status: int) -> Response:
    return _reply({"error": message}, status)


def _viewer():
    cert = verify_client_cert()
    if cert is not None:
        username = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
        user = rec_core.find_user(username)
        return user if user is not None and user.active else None
    if _check_session() or _check_token():
        user = current_user._get_current_object()
        return user if user is not None and user.active else None
    return None


def _setup():
    config = rec_core.settings()
    if config is None:
        return None, None, _error(MSG_OFF, 404)
    viewer = _viewer()
    if viewer is None:
        return None, None, _error(MSG_AUTH, 401)
    if not rec_core.is_admin(viewer):
        return None, None, _error(MSG_ADMIN, 403)
    return config, viewer, None


def _visible(viewer, rec_id: str):
    rec = db.session.get(TaklabRecording, rec_id) if rec_id and len(rec_id) <= 36 else None
    if rec is None or not rec_core.can_see(viewer, rec):
        return None
    return rec


def _filtered_query():
    """Recordings matching the list filters (kind, account, from, to)."""
    query = select(TaklabRecording).where(TaklabRecording.deleted.is_(False))
    kind = request.args.get("kind")
    if kind:
        if kind not in KINDS:
            return None, _error("Nieznany rodzaj nagrania.", 400)
        query = query.where(TaklabRecording.kind == kind)
    account = (request.args.get("account") or "").strip()
    if account:
        query = query.where(TaklabRecording.account == account)
    for key, compare in (("from", "ge"), ("to", "le")):
        raw = request.args.get(key)
        if not raw:
            continue
        moment = rec_core.parse_time(raw)
        if moment is None:
            return None, _error(f"Nieprawidłowa data w polu {key}.", 400)
        column = TaklabRecording.started_at
        query = query.where(column >= moment if compare == "ge" else column <= moment)
    return query, None


@taklab_recordings_api.route("/api/taklab/recordings", methods=["GET"])
def list_recordings():
    config, viewer, refusal = _setup()
    if refusal:
        return refusal
    query, refusal = _filtered_query()
    if refusal:
        return refusal
    try:
        page = max(1, int(request.args.get("page", 1)))
        per_page = min(MAX_PER_PAGE, max(1, int(request.args.get("per_page", PER_PAGE))))
    except ValueError:
        return _error("Nieprawidłowy numer strony.", 400)

    total = db.session.execute(select(db.func.count()).select_from(query.subquery())).scalar() or 0
    rows = db.session.execute(
        query.order_by(TaklabRecording.started_at.desc(), TaklabRecording.id)
        .offset((page - 1) * per_page)
        .limit(per_page)
    ).scalars().all()
    items = [r.to_json(rec_core.can_manage(viewer, r)) for r in rows]
    return _reply(
        {
            # Both envelopes: CloudTAK reads items/page, OpenTAKServer-UI results/current_page.
            "items": items,
            "results": items,
            "total": total,
            "page": page,
            "current_page": page,
            "per_page": per_page,
            "total_pages": max(1, -(-total // per_page)),
            "admin": True,
            "enabled": True,
        }
    )


@taklab_recordings_api.route("/api/taklab/recordings/usage", methods=["GET"])
def recordings_usage():
    config, viewer, refusal = _setup()
    if refusal:
        return refusal
    return _reply(rec_core.usage(config))


@taklab_recordings_api.route("/api/taklab/recordings/<rec_id>", methods=["GET", "PATCH", "DELETE"])
def one_recording(rec_id):
    config, viewer, refusal = _setup()
    if refusal:
        return refusal
    rec = _visible(viewer, rec_id)
    if rec is None:
        return _error(MSG_NOT_FOUND, 404)
    manage = rec_core.can_manage(viewer, rec)
    if request.method == "GET":
        return _reply(rec.to_detail_json(manage))
    if request.method == "DELETE":
        rec_core.delete_recording(rec, config, f"by {viewer.username}")
        return _reply({"success": True})
    body = request.get_json(silent=True) or {}
    name = body.get("name")
    if not isinstance(name, str) or not name.strip():
        return _error("Podaj nazwę nagrania.", 400)
    rec.name = " ".join(name.split())[:MAX_NAME]
    db.session.add(rec)
    db.session.commit()
    rec_core.publish_marker(rec, config)
    db.session.commit()
    return _reply(rec.to_json(manage))


def _playback(config, rec: TaklabRecording) -> Response:
    """A recording still in progress (or not joined): MP4 straight from MediaMTX playback."""
    if not rec.source_path or rec.started_at is None:
        return _error(MSG_FILE, 404)
    duration = max(1.0, rec.duration_s() or 1.0)
    params = {
        "path": rec.source_path,
        "start": rec.started_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "duration": f"{duration:.3f}",
        "format": "mp4",
    }
    try:
        upstream = requests.get(
            config.playback_url.rstrip("/") + "/get", params=params, stream=True, timeout=(5, 60)
        )
    except requests.RequestException as e:
        logger.warning(f"taklab recordings: playback unreachable ({type(e).__name__})")
        return _error(MSG_IN_PROGRESS, 409)
    if upstream.status_code != 200:
        upstream.close()
        return _error(MSG_IN_PROGRESS, 409)
    response = Response(
        stream_with_context(upstream.iter_content(64 * 1024)), status=200, mimetype="video/mp4"
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@taklab_recordings_api.route("/api/taklab/recordings/<rec_id>/file", methods=["GET"])
def recording_file(rec_id):
    config, viewer, refusal = _setup()
    if refusal:
        return refusal
    rec = _visible(viewer, rec_id)
    if rec is None:
        return _error(MSG_NOT_FOUND, 404)
    path = rec_core.absolute(config, rec.file_path)
    if path is None or not path.exists():
        if rec.kind == "video" and (rec.state == STATE_RECORDING or rec.segment_list()):
            return _playback(config, rec)
        return _error(MSG_FILE, 404)
    filename = f"{rec.kind}-{rec.started_at.strftime('%Y%m%d-%H%M%S')}{path.suffix}"
    response = send_file(
        path,
        mimetype=rec.mime or "application/octet-stream",
        conditional=True,
        download_name=filename,
        as_attachment=request.args.get("download") in ("1", "true"),
        max_age=0,
    )
    response.headers["Cache-Control"] = "private, no-store"
    return response


_UNSAFE = re.compile(r'[\x00-\x1f\x7f/\\:*?"<>|]+')


def _archive_name(rec: TaklabRecording, suffix: str, taken: set[str]) -> str:
    """'<name>_<YYYYmmdd-HHMMSS><suffix>', safe on Windows, unique in the archive."""
    stamp = rec.started_at.strftime("%Y%m%d-%H%M%S") if rec.started_at else "bez-daty"
    base = _UNSAFE.sub("_", (rec.name or "").strip()).strip(" ._")[:120] or rec.kind
    stem = f"{base}_{stamp}"
    name = f"{stem}{suffix}"
    number = 2
    while name.lower() in taken:
        name = f"{stem} ({number}){suffix}"
        number += 1
    taken.add(name.lower())
    return name


class _Sink(io.RawIOBase):
    """Write-only, non-seekable buffer: zipfile streams into it, we drain it."""

    def __init__(self):
        super().__init__()
        self._chunks: list[bytes] = []

    def writable(self) -> bool:
        return True

    def write(self, data) -> int:
        self._chunks.append(bytes(data))
        return len(data)

    def take(self) -> bytes:
        out = b"".join(self._chunks)
        self._chunks.clear()
        return out


def _zip_stream(entries: list[tuple], skipped: list[str]):
    sink = _Sink()
    with zipfile.ZipFile(sink, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
        for path, arcname, moment in entries:
            info = zipfile.ZipInfo(arcname, date_time=moment)
            info.compress_type = zipfile.ZIP_STORED
            try:
                with path.open("rb") as source, archive.open(info, "w", force_zip64=True) as target:
                    while chunk := source.read(ARCHIVE_CHUNK):
                        target.write(chunk)
                        yield sink.take()
            except OSError as e:
                # Deleted by the janitor while we were streaming: say so in the archive.
                logger.warning(f"taklab recordings: archive skips {arcname} ({type(e).__name__})")
                skipped.append(arcname)
            yield sink.take()
        if skipped:
            note = "Tych nagrań nie było w archiwum (nagranie trwa albo plik jest niedostępny):\n"
            archive.writestr("pominiete.txt", note + "\n".join(skipped) + "\n")
    yield sink.take()


@taklab_recordings_api.route("/api/taklab/recordings/archive", methods=["GET"])
def recordings_archive():
    """ZIP of chosen recordings: ?ids=a,b,c (up to 100), or ?all=1 with the list filters."""
    config, viewer, refusal = _setup()
    if refusal:
        return refusal
    raw_ids = [part for part in (request.args.get("ids") or "").split(",") if part.strip()]
    if raw_ids:
        if len(raw_ids) > MAX_ARCHIVE_IDS:
            return _error(MSG_TOO_MANY, 400)
        ids = {part.strip() for part in raw_ids if len(part.strip()) <= 36}
        query = select(TaklabRecording).where(
            TaklabRecording.deleted.is_(False), TaklabRecording.id.in_(ids)
        )
    elif request.args.get("all") in ("1", "true"):
        query, refusal = _filtered_query()
        if refusal:
            return refusal
    else:
        return _error("Wybierz nagrania albo wszystkie.", 400)
    rows = db.session.execute(
        query.order_by(TaklabRecording.started_at, TaklabRecording.id).limit(MAX_ARCHIVE_ALL + 1)
    ).scalars().all()
    if len(rows) > MAX_ARCHIVE_ALL:
        return _error(MSG_TOO_MANY, 400)

    entries, skipped, taken = [], [], {"pominiete.txt"}
    for rec in rows:
        path = rec_core.absolute(config, rec.file_path)
        suffix = path.suffix if path is not None else ""
        arcname = _archive_name(rec, suffix, taken)
        if path is None or not path.is_file():
            skipped.append(arcname)
            continue
        local = rec_core._local(rec.started_at or datetime(1980, 1, 2), config)
        stamp = max(local.timetuple()[:6], (1980, 1, 1, 0, 0, 0))
        entries.append((path, arcname, stamp))
    if not entries:
        return _error(MSG_NOTHING, 404)

    logger.info(f"taklab recordings: {viewer.username} downloads {len(entries)} recordings as ZIP")
    filename = datetime.now(timezone.utc).strftime("nagrania-%Y%m%d-%H%M%S.zip")
    response = Response(stream_with_context(_zip_stream(entries, skipped)), mimetype="application/zip")
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Accel-Buffering"] = "no"
    return response


@taklab_recordings_api.route("/api/taklab/recordings/ingest", methods=["POST"])
def ingest_recording():
    config = rec_core.settings()
    if config is None:
        return _error(MSG_OFF, 404)
    if request.headers.get("X-Forwarded-For") or request.headers.get("X-Real-IP"):
        return _error(MSG_NOT_FOUND, 404)
    header = request.headers.get("Authorization") or ""
    presented = header[7:].strip() if header.lower().startswith("bearer ") else None
    kinds = rec_core.token_kinds(config, presented)
    if not kinds:
        return _error("Nieprawidłowy sekret.", 401)
    kind = (request.form.get("kind") or "").strip()
    if kind not in kinds:
        return _error("Ten sekret nie przyjmuje takiego rodzaju nagrania.", 403)
    if request.content_length is not None and request.content_length > config.max_ingest_bytes + 64 * 1024:
        return _error("Plik jest za duży.", 413)
    upload = request.files.get("file")
    if upload is None:
        return _error("Brak pliku.", 400)
    data = upload.read(config.max_ingest_bytes + 1)
    if len(data) > config.max_ingest_bytes:
        return _error("Plik jest za duży.", 413)
    mime = (upload.mimetype or "").lower()
    fields = {key: request.form.get(key) for key in (
        "account", "callsign", "author", "started_at", "ended_at", "duration_s", "text", "name",
        "source_ref",
    )}
    try:
        rec, created = rec_core.ingest(config, kind=kind, data=data, mime=mime, fields=fields)
    except rec_core.IngestError as e:
        return _error(str(e), e.status)
    return _reply(rec.to_json(False), 201 if created else 200)
