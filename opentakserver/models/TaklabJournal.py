"""taklab: one entry of the environment journal (who did what, when, where).

Design: ~/taklab docs/superpowers/specs/2026-11-24-journal-design.md.

Rows are only ever inserted (by taklab_journal.record_*). Nothing in OTS
updates or deletes them: no foreign keys, so purging CoT, points or EUDs and
clearing the map leave the journal intact.

recipients and file are JSON text (see to_json for their shape). account is
NULL for events of the server itself; the API shows it as "serwer".
"""

import json
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Float, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from opentakserver.extensions import db

KINDS = (
    "object_add",
    "object_change",
    "object_delete",
    "position",
    "disconnect",
    "chat",
    "video_share",
    "file_upload",
    "file_send",
    "alert",
    "casevac",
    "recording_video",
    "recording_voice",
    "recording_scan",
    "recording_delete",
    "journal_export",
)
SERVER_ACCOUNT = "serwer"


def iso(moment: datetime | None) -> str | None:
    if moment is None:
        return None
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def _json(value, default):
    if not value:
        return default
    try:
        return json.loads(value)
    except ValueError:
        return default


class TaklabJournal(db.Model):
    __tablename__ = "taklab_journal"
    __table_args__ = (
        Index("taklab_journal_at_idx", "at"),
        Index("taklab_journal_account_idx", "account"),
        Index("taklab_journal_kind_idx", "kind"),
        Index("taklab_journal_object_uid_idx", "object_uid"),
        Index("taklab_journal_device_kind_idx", "device_uid", "kind"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    account: Mapped[str] = mapped_column(String(255), nullable=True)
    callsign: Mapped[str] = mapped_column(String(255), nullable=True)
    device_uid: Mapped[str] = mapped_column(String(255), nullable=True)
    object_uid: Mapped[str] = mapped_column(String(255), nullable=True)
    object_type: Mapped[str] = mapped_column(String(100), nullable=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    text: Mapped[str] = mapped_column(Text, nullable=True)
    private: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    recipients: Mapped[str] = mapped_column(Text, nullable=True)
    chatroom: Mapped[str] = mapped_column(String(255), nullable=True)
    latitude: Mapped[float] = mapped_column(Float, nullable=True)
    longitude: Mapped[float] = mapped_column(Float, nullable=True)
    mission: Mapped[str] = mapped_column(String(255), nullable=True)
    file: Mapped[str] = mapped_column(Text, nullable=True)
    # Object state hash (type, name, position ~10 m): identical repeats give no entry
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=True)

    def recipient_list(self) -> list:
        value = _json(self.recipients, [])
        return value if isinstance(value, list) else []

    def file_info(self) -> dict | None:
        value = _json(self.file, None)
        return value if isinstance(value, dict) else None

    def to_json(self, available: bool | None = None) -> dict:
        file_info = self.file_info()
        if file_info is not None:
            file_info = dict(file_info)
            file_info["available"] = bool(available)
        return {
            "id": self.id,
            "at": iso(self.at),
            "kind": self.kind,
            "account": self.account or SERVER_ACCOUNT,
            "callsign": self.callsign,
            "device_uid": self.device_uid,
            "object_uid": self.object_uid,
            "object_type": self.object_type,
            "title": self.title or "",
            "text": self.text,
            "private": bool(self.private),
            "recipients": self.recipient_list(),
            "chatroom": self.chatroom,
            "lat": None if self.latitude is None else round(self.latitude, 5),
            "lon": None if self.longitude is None else round(self.longitude, 5),
            "mission": self.mission,
            "file": file_info,
        }
