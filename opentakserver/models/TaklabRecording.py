"""taklab: one recording on the server - a video, a voice message or an OCR scan.

A row is written by the server itself (MediaMTX hooks for video) or by the
internal ingest route (voice register, OCR queue). The file lives on the
recordings volume; file_path is relative to OTS_TAKLAB_RECORDINGS_DIR.

Video is one row per continuous publication: MediaMTX writes 10 minute
segments, each completed segment extends the row (segments, ended_at), and
the janitor joins the segments into one MP4 once the publisher has been gone
for longer than the reconnect window (state "recording" -> "ready").
"""

import json
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Float, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from opentakserver.extensions import db

KINDS = ("video", "voice", "scan")
STATE_RECORDING = "recording"
STATE_READY = "ready"
STATE_ERROR = "error"


def iso(moment: datetime | None) -> str | None:
    if moment is None:
        return None
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


class TaklabRecording(db.Model):
    __tablename__ = "taklab_recordings"
    __table_args__ = (
        Index("taklab_recordings_started_idx", "started_at"),
        Index("taklab_recordings_account_idx", "account"),
        Index("taklab_recordings_kind_idx", "kind"),
        Index("taklab_recordings_source_path_idx", "source_path", "state"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    account: Mapped[str] = mapped_column(String(255), nullable=True)
    callsign: Mapped[str] = mapped_column(String(255), nullable=True)
    device_uid: Mapped[str] = mapped_column(String(255), nullable=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    ended_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    latitude: Mapped[float] = mapped_column(Float, nullable=True)
    longitude: Mapped[float] = mapped_column(Float, nullable=True)
    # JSON [[lat, lon, "ISO time"], ...], decimated, from the points table
    track: Mapped[str] = mapped_column(Text, nullable=True)
    file_path: Mapped[str] = mapped_column(String(1024), nullable=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=True)
    mime: Mapped[str] = mapped_column(String(100), nullable=True)
    text: Mapped[str] = mapped_column(Text, nullable=True)
    deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    deleted_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    # Video only: MediaMTX path, JSON list of segment files, assembly state
    source_path: Mapped[str] = mapped_column(String(255), nullable=True)
    segments: Mapped[str] = mapped_column(Text, nullable=True)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default=STATE_READY)
    last_activity_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    # Idempotency key of the ingest caller ("voice:<id>", "ocr:<job>")
    source_ref: Mapped[str] = mapped_column(String(255), nullable=True, unique=True)
    marker_uid: Mapped[str] = mapped_column(String(255), nullable=True)

    def segment_list(self) -> list[str]:
        try:
            value = json.loads(self.segments or "[]")
        except ValueError:
            return []
        return [s for s in value if isinstance(s, str)]

    def track_list(self) -> list:
        try:
            value = json.loads(self.track or "[]")
        except ValueError:
            return []
        return value if isinstance(value, list) else []

    def duration_s(self) -> float | None:
        if self.ended_at is None or self.started_at is None:
            return None
        return round(max(0.0, (self.ended_at - self.started_at).total_seconds()), 1)

    def to_json(self, can_manage: bool = False) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "account": self.account or "",
            "callsign": self.callsign or "",
            "uid": self.device_uid or "",
            "name": self.name or "",
            "started_at": iso(self.started_at),
            "ended_at": iso(self.ended_at),
            "duration_s": self.duration_s(),
            "lat": self.latitude,
            "lon": self.longitude,
            "size_bytes": self.size_bytes,
            "mime": self.mime,
            "has_text": bool(self.text),
            "in_progress": self.state == STATE_RECORDING,
            # Same flag under both names the clients read: rename and delete allowed.
            "can_manage": can_manage,
            "can_edit": can_manage,
        }

    def to_detail_json(self, can_manage: bool = False) -> dict:
        body = self.to_json(can_manage)
        body["track"] = self.track_list()
        body["text"] = self.text
        return body
