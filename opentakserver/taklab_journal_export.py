"""taklab: journal exports - PDF (fpdf2), CSV, JSON, GPX and the streamed ZIP.

Design: ~/taklab docs/superpowers/specs/2026-11-24-journal-design.md.

Files for the ZIP come from where the server keeps them, never from a copy:
    * recording      - the recordings registry (taklab_recordings.absolute)
    * data_package   - UPLOAD_FOLDER/<sha256>.zip (save_data_package_file)
    * mission_content - OTS_DATA_FOLDER/missions/<file name> (upload_content);
                       OTS keeps these by name, so a later file with the same
                       name overwrites it - a size mismatch counts as missing.
A file that is gone is listed in BRAKUJACE.txt instead.

The ZIP is written by zipfile into a non-seekable sink (data descriptors) and
handed out chunk by chunk; video, audio, images and zips are STORED.
"""

import csv
import io
import json
import os
import re
import unicodedata
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

from flask import current_app as app
from werkzeug.utils import secure_filename

from opentakserver.models.TaklabJournal import SERVER_ACCOUNT, TaklabJournal
from opentakserver.taklab_journal import KIND_LABELS, distance_m, local

FONT_DIR = "/usr/share/fonts/truetype/dejavu"
FONT_REGULAR = "DejaVuSans.ttf"
FONT_BOLD = "DejaVuSans-Bold.ttf"
PDF_CELL_MAX = 1500
CHUNK = 256 * 1024
STORED_PREFIXES = ("video/", "audio/", "image/")
STORED_EXTENSIONS = {
    ".mp4", ".m4a", ".mov", ".mkv", ".webm", ".ogg", ".opus", ".mp3", ".wav", ".aac",
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".zip", ".kmz", ".gz", ".7z", ".pdf",
}
SHA256 = re.compile(r"[0-9a-f]{64}")
_UNSAFE = re.compile(r"[^\w.\-]+", re.UNICODE)
TRANSLIT = str.maketrans("ąćęłńóśźżĄĆĘŁŃÓŚŹŻ", "acelnoszzACELNOSZZ")


# --- names ---------------------------------------------------------------------


def safe_component(value: str | None, default: str = "plik", limit: int = 80) -> str:
    """One path component for the ZIP: no separators, no "..", no control characters."""
    text = unicodedata.normalize("NFC", str(value or ""))
    text = "".join(ch for ch in text if unicodedata.category(ch)[0] != "C")
    text = text.replace("/", "_").replace("\\", "_")
    text = _UNSAFE.sub("_", text)
    text = re.sub(r"\.{2,}", ".", text)
    text = re.sub(r"_+", "_", text).strip("._ ")
    text = text[:limit].strip("._ ")
    return text or default


def _unique(name: str, used: set[str]) -> str:
    if name not in used:
        used.add(name)
        return name
    stem, ext = os.path.splitext(name)
    index = 2
    while f"{stem}-{index}{ext}" in used:
        index += 1
    name = f"{stem}-{index}{ext}"
    used.add(name)
    return name


# --- file look-up -----------------------------------------------------------------


@dataclass
class PlannedFile:
    entry: TaklabJournal
    info: dict
    path: Path | None
    reason: str | None
    mime: str | None
    size: int = 0
    text: str | None = None  # scan text, saved next to the image
    suffix: str = ""


def locate(info: dict | None) -> tuple[Path | None, str | None, dict]:
    """(path, reason when missing, extra) of a journal file entry."""
    if not info:
        return None, "brak pliku", {}
    source, ref = info.get("source"), info.get("ref")
    if source == "recording":
        from opentakserver import taklab_recordings as rec_core
        from opentakserver.extensions import db
        from opentakserver.models.TaklabRecording import STATE_RECORDING, TaklabRecording

        config = rec_core.settings()
        if config is None:
            return None, "rejestr nagrań jest wyłączony", {}
        rec = db.session.get(TaklabRecording, ref) if ref and len(str(ref)) <= 36 else None
        if rec is None:
            return None, "nagranie usunięte z rejestru", {}
        if rec.deleted:
            return None, "nagranie usunięte", {}
        path = rec_core.absolute(config, rec.file_path)
        if path is None or not path.is_file():
            if rec.state == STATE_RECORDING:
                return None, "nagranie jeszcze trwa", {}
            return None, "brak pliku nagrania na serwerze", {}
        return path, None, {"mime": rec.mime, "text": rec.text if rec.kind == "scan" else None}
    if source == "data_package":
        ref = str(ref or "").lower()
        if not SHA256.fullmatch(ref):
            return None, "nieznany plik paczki", {}
        path = Path(app.config.get("UPLOAD_FOLDER")) / f"{ref}.zip"
        if not path.is_file():
            return None, "paczka usunięta z serwera", {}
        return path, None, {"mime": "application/zip"}
    if source == "mission_content":
        name = secure_filename(Path(str(info.get("filename") or "")).name)
        if not name:
            return None, "nieznany plik", {}
        path = Path(app.config.get("OTS_DATA_FOLDER")) / "missions" / name
        if not path.is_file():
            return None, "plik usunięty z serwera", {}
        size = info.get("size")
        if isinstance(size, int) and size > 0 and path.stat().st_size != size:
            return None, "plik nadpisany innym o tej samej nazwie", {}
        return path, None, {}
    return None, "nieznane źródło pliku", {}


def is_available(info: dict | None) -> bool:
    path, _, _ = locate(info)
    return path is not None


def file_key(info: dict) -> tuple:
    return (info.get("source"), info.get("ref") or info.get("filename"))


def plan_files(entries: list[TaklabJournal]) -> list[PlannedFile]:
    """One planned file per (source, ref), first entry wins."""
    seen = set()
    planned = []
    for entry in entries:
        info = entry.file_info()
        if not info:
            continue
        key = file_key(info)
        if key in seen:
            continue
        seen.add(key)
        path, reason, extra = locate(info)
        size = path.stat().st_size if path is not None else 0
        mime = extra.get("mime") or info.get("mime")
        planned.append(
            PlannedFile(
                entry=entry,
                info=info,
                path=path,
                reason=reason,
                mime=mime,
                size=size,
                text=extra.get("text"),
                suffix=path.suffix if path is not None else "",
            )
        )
    return planned


def estimate(entries: list[TaklabJournal]) -> dict:
    planned = plan_files(entries)
    present = [p for p in planned if p.path is not None]
    return {
        "entries": len(entries),
        "files": len(present),
        "bytes": sum(p.size for p in present),
        "missing": len(planned) - len(present),
    }


# --- text forms ----------------------------------------------------------------------


def _who(entry: TaklabJournal) -> str:
    account = entry.account or SERVER_ACCOUNT
    return f"{entry.callsign} ({account})" if entry.callsign else account


def _recipients_text(entry: TaklabJournal) -> str:
    names = []
    for r in entry.recipient_list():
        label = r.get("callsign") or r.get("uid") or "?"
        if r.get("account"):
            label += f" ({r['account']})"
        names.append(label)
    return ", ".join(names)


def _coords(entry: TaklabJournal) -> str:
    if entry.latitude is None or entry.longitude is None:
        return ""
    return f"{entry.latitude:.5f}, {entry.longitude:.5f}"


def _when(moment: datetime, zone: str) -> str:
    return local(moment, zone).strftime("%Y-%m-%d %H:%M:%S")


CSV_COLUMNS = [
    "id", "czas", "rodzaj", "konto", "znak", "urzadzenie", "obiekt", "typ", "tytul", "tresc",
    "prywatna", "adresaci", "pokoj", "szerokosc", "dlugosc", "misja", "plik",
]


def _csv_cell(value) -> str:
    text = "" if value is None else str(value)
    # Spreadsheet formula injection: a cell must not start a formula.
    if text and text[0] in "=+-@\t\r":
        text = "'" + text
    return text


def build_csv(entries: list[TaklabJournal], zone: str) -> bytes:
    out = io.StringIO()
    writer = csv.writer(out, delimiter=";", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(CSV_COLUMNS)
    for e in entries:
        info = e.file_info() or {}
        writer.writerow(
            [_csv_cell(v) for v in (
                e.id,
                _when(e.at, zone),
                KIND_LABELS.get(e.kind, e.kind),
                e.account or SERVER_ACCOUNT,
                e.callsign,
                e.device_uid,
                e.object_uid,
                e.object_type,
                e.title,
                e.text,
                "tak" if e.private else "nie",
                _recipients_text(e),
                e.chatroom,
                None if e.latitude is None else f"{e.latitude:.5f}",
                None if e.longitude is None else f"{e.longitude:.5f}",
                e.mission,
                info.get("filename"),
            )]
        )
    return ("﻿" + out.getvalue()).encode("utf-8")


def build_json(entries: list[TaklabJournal], available: dict[int, bool]) -> bytes:
    body = [e.to_json(available.get(e.id)) for e in entries]
    return json.dumps(body, ensure_ascii=False, indent=1).encode("utf-8")


# --- participants and tracks -----------------------------------------------------------


@dataclass
class Participant:
    device_uid: str
    account: str
    callsign: str
    points: list = field(default_factory=list)  # (at, lat, lon)

    @property
    def distance_m(self) -> float:
        total = 0.0
        for a, b in zip(self.points, self.points[1:]):
            total += distance_m(a[1], a[2], b[1], b[2])
        return total


def participants(positions: list[TaklabJournal]) -> list[Participant]:
    found: dict[str, Participant] = {}
    for e in positions:
        if e.latitude is None or e.longitude is None:
            continue
        key = e.device_uid or e.callsign or "?"
        person = found.get(key)
        if person is None:
            person = found[key] = Participant(
                device_uid=key, account=e.account or SERVER_ACCOUNT, callsign=e.callsign or key
            )
        if e.callsign:
            person.callsign = e.callsign
        person.points.append((e.at, e.latitude, e.longitude))
    for person in found.values():
        person.points.sort(key=lambda p: p[0])
    return sorted(found.values(), key=lambda p: (p.callsign.lower(), p.account))


def build_gpx(person: Participant) -> bytes:
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<gpx version="1.1" creator="TakLab OTS" xmlns="http://www.topografix.com/GPX/1/1">',
        f"  <trk><name>{escape(person.callsign)} ({escape(person.account)})</name><trkseg>",
    ]
    for at, lat, lon in person.points:
        stamp = at.strftime("%Y-%m-%dT%H:%M:%SZ")
        lines.append(f'    <trkpt lat="{lat:.6f}" lon="{lon:.6f}"><time>{stamp}</time></trkpt>')
    lines.append("  </trkseg></trk>")
    lines.append("</gpx>")
    return ("\n".join(lines) + "\n").encode("utf-8")


def _duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours} h {minutes:02d} min"
    if minutes:
        return f"{minutes} min {secs:02d} s"
    return f"{secs} s"


def _km(meters: float) -> str:
    return f"{meters / 1000:.2f} km".replace(".", ",")


# --- PDF -------------------------------------------------------------------------


def _fonts(font_dir: str | None) -> tuple[Path, Path] | None:
    for folder in [d for d in (font_dir, FONT_DIR) if d]:
        regular, bold = Path(folder) / FONT_REGULAR, Path(folder) / FONT_BOLD
        if regular.is_file() and bold.is_file():
            return regular, bold
    return None


def _cut(text: str | None, limit: int = PDF_CELL_MAX) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: limit - 1] + "…"


@dataclass
class PdfMeta:
    since: datetime | None
    until: datetime | None
    generated_by: str
    generated_at: datetime
    zone: str
    font_dir: str | None
    filters: dict


def build_pdf(entries: list[TaklabJournal], positions: list[TaklabJournal], meta: PdfMeta) -> bytes:
    from fpdf import FPDF
    from fpdf.fonts import FontFace

    fonts = _fonts(meta.font_dir)
    pdf = FPDF(orientation="L", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=12)
    pdf.set_title("Dziennik zdarzeń")
    pdf.set_creator("TakLab OTS")
    if fonts is not None:
        pdf.add_font("dejavu", "", str(fonts[0]))
        pdf.add_font("dejavu", "B", str(fonts[1]))
        family = "dejavu"

        def t(value) -> str:
            return "" if value is None else str(value)
    else:
        # No DejaVu in the image: core font, Polish letters without diacritics.
        family = "helvetica"

        def t(value) -> str:
            text = "" if value is None else str(value).translate(TRANSLIT)
            return text.encode("latin-1", "replace").decode("latin-1")

    def zoned(moment):
        return _when(moment, meta.zone) if moment else None

    # Title page
    pdf.add_page()
    pdf.set_font(family, "B", 22)
    pdf.cell(0, 14, t("Dziennik zdarzeń"), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font(family, "", 12)
    span = f"od {zoned(meta.since) or 'początku dziennika'} do {zoned(meta.until) or zoned(meta.generated_at)}"
    pdf.multi_cell(0, 7, t(f"Zakres: {span} (czas {meta.zone})"), new_x="LMARGIN", new_y="NEXT")
    pdf.multi_cell(
        0, 7, t(f"Wygenerował: {meta.generated_by}, {zoned(meta.generated_at)}"),
        new_x="LMARGIN", new_y="NEXT",
    )
    extra = {k: v for k, v in meta.filters.items() if k not in ("since", "until") and v}
    if extra:
        described = "; ".join(f"{k}: {v}" for k, v in extra.items())
        pdf.multi_cell(0, 7, t(f"Filtr: {described}"), new_x="LMARGIN", new_y="NEXT")
    pdf.multi_cell(0, 7, t(f"Liczba wpisów: {len(entries)}"), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)
    counts: dict[str, int] = {}
    for e in entries:
        counts[e.kind] = counts.get(e.kind, 0) + 1
    pdf.set_font(family, "", 11)
    heading = FontFace(emphasis="BOLD", fill_color=(225, 225, 225))
    with pdf.table(col_widths=(90, 30), width=120, align="LEFT", headings_style=heading) as table:
        row = table.row()
        row.cell(t("Rodzaj"))
        row.cell(t("Wpisy"))
        for kind, label in KIND_LABELS.items():
            if counts.get(kind):
                row = table.row()
                row.cell(t(label))
                row.cell(str(counts[kind]))

    # Entries
    pdf.add_page()
    pdf.set_font(family, "B", 14)
    pdf.cell(0, 10, t("Wpisy"), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font(family, "", 8)
    if not entries:
        pdf.cell(0, 8, t("Brak wpisów w tym zakresie."), new_x="LMARGIN", new_y="NEXT")
    else:
        with pdf.table(
            col_widths=(32, 45, 30, 166),
            headings_style=heading,
            line_height=4.2,
            text_align="LEFT",
        ) as table:
            row = table.row()
            for label in ("Czas", "Kto", "Rodzaj", "Opis"):
                row.cell(t(label))
            for e in entries:
                parts = [e.title or ""]
                if e.text:
                    parts.append(_cut(e.text))
                recipients = _recipients_text(e)
                if recipients:
                    parts.append(f"Adresaci: {recipients}")
                if e.chatroom and e.kind == "chat":
                    parts.append(f"Pokój: {e.chatroom}")
                if e.mission:
                    parts.append(f"Misja: {e.mission}")
                coords = _coords(e)
                if coords:
                    parts.append(f"Położenie: {coords}")
                info = e.file_info()
                if info and info.get("filename"):
                    parts.append(f"Plik: {info['filename']}")
                if e.private:
                    parts.append("(prywatna)")
                row = table.row()
                row.cell(t(_when(e.at, meta.zone)))
                row.cell(t(_cut(_who(e), 200)))
                row.cell(t(KIND_LABELS.get(e.kind, e.kind)))
                row.cell(t(_cut("\n".join(parts), PDF_CELL_MAX + 600)))

    # Participants
    pdf.add_page()
    pdf.set_font(family, "B", 14)
    pdf.cell(0, 10, t("Uczestnicy"), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font(family, "", 8)
    people = participants(positions)
    if not people:
        pdf.cell(0, 8, t("Brak pozycji uczestników w tym zakresie."), new_x="LMARGIN", new_y="NEXT")
    else:
        with pdf.table(
            col_widths=(55, 62, 62, 40, 30), headings_style=heading, line_height=4.2, text_align="LEFT"
        ) as table:
            row = table.row()
            for label in ("Uczestnik", "Pierwsza pozycja", "Ostatnia pozycja", "Czas", "Dystans"):
                row.cell(t(label))
            for person in people:
                first, last = person.points[0], person.points[-1]
                row = table.row()
                row.cell(t(f"{person.callsign} ({person.account})"))
                row.cell(t(f"{_when(first[0], meta.zone)}\n{first[1]:.5f}, {first[2]:.5f}"))
                row.cell(t(f"{_when(last[0], meta.zone)}\n{last[1]:.5f}, {last[2]:.5f}"))
                row.cell(t(_duration((last[0] - first[0]).total_seconds())))
                row.cell(t(_km(person.distance_m)))
    return bytes(pdf.output())


# --- ZIP -------------------------------------------------------------------------


class _Sink(io.RawIOBase):
    """Write-only, non-seekable buffer drained by the streaming generator."""

    def __init__(self):
        self._chunks: list[bytes] = []
        self._written = 0

    def writable(self):
        return True

    def seekable(self):
        return False

    def tell(self):
        return self._written

    def write(self, data):
        data = bytes(data)
        self._chunks.append(data)
        self._written += len(data)
        return len(data)

    def flush(self):
        pass

    def drain(self) -> bytes:
        data = b"".join(self._chunks)
        self._chunks.clear()
        return data


def _stored(name: str, mime: str | None) -> bool:
    if mime and mime.lower().startswith(STORED_PREFIXES):
        return True
    if mime and "zip" in mime.lower():
        return True
    return os.path.splitext(name)[1].lower() in STORED_EXTENSIONS


def _member(name: str, moment: datetime | None, compress: bool) -> zipfile.ZipInfo:
    stamp = (moment or datetime(1980, 1, 1)).timetuple()[:6]
    if stamp[0] < 1980:
        stamp = (1980, 1, 1, 0, 0, 0)
    info = zipfile.ZipInfo(name, date_time=stamp)
    info.compress_type = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
    info.external_attr = 0o644 << 16
    return info


def file_name_in_zip(planned: PlannedFile, zone: str) -> str:
    e = planned.entry
    stamp = local(e.at, zone).strftime("%Y%m%d-%H%M%S")
    filename = planned.info.get("filename") or "plik"
    stem, ext = os.path.splitext(filename)
    ext = planned.suffix or ext
    if ext and not re.fullmatch(r"\.[A-Za-z0-9]{1,8}", ext):
        ext = ""
    parts = [
        stamp,
        safe_component(e.account or SERVER_ACCOUNT, "konto", 40),
        safe_component(e.kind, "wpis", 30),
        safe_component(stem if ext else filename, "plik", 80),
    ]
    return "_".join(parts) + ext.lower()


def stream_zip(
    entries: list[TaklabJournal],
    positions: list[TaklabJournal],
    planned: list[PlannedFile],
    pdf_bytes: bytes | None,
    pdf_note: str | None,
    zone: str,
    now: datetime,
):
    """Generator of ZIP bytes. Everything that needs the database is done by the caller."""
    sink = _Sink()
    archive = zipfile.ZipFile(sink, mode="w", allowZip64=True)
    used: set[str] = set()
    present = {file_key(p.info): p.path is not None for p in planned}
    available = {e.id: bool(present.get(file_key(e.file_info()))) for e in entries if e.file_info()}

    def small(name: str, data: bytes, compress: bool = True):
        archive.writestr(_member(_unique(name, used), now, compress), data)

    if pdf_bytes is not None:
        small("dziennik.pdf", pdf_bytes, compress=False)
    if pdf_note:
        small("dziennik-pdf-pominiety.txt", pdf_note.encode("utf-8"))
    small("dziennik.csv", build_csv(entries, zone))
    small("dziennik.json", build_json(entries, available))
    yield sink.drain()

    missing = []
    for p in planned:
        if p.path is None:
            missing.append(p)
            continue
        name = _unique("pliki/" + file_name_in_zip(p, zone), used)
        try:
            with open(p.path, "rb") as source:
                info = _member(name, p.entry.at, not _stored(name, p.mime))
                with archive.open(info, mode="w", force_zip64=p.size > 1024**3) as target:
                    while True:
                        chunk = source.read(CHUNK)
                        if not chunk:
                            break
                        target.write(chunk)
                        out = sink.drain()
                        if out:
                            yield out
        except OSError:
            p.reason = "nie udało się odczytać pliku"
            missing.append(p)
            continue
        if p.text:
            stem = os.path.splitext(name)[0]
            small(f"{stem}.txt", p.text.encode("utf-8"))
        yield sink.drain()

    for person in participants(positions):
        name = f"trasy/{safe_component(person.callsign, 'uczestnik', 60)}_{safe_component(person.account, 'konto', 40)}.gpx"
        small(name, build_gpx(person))
    yield sink.drain()

    if missing:
        lines = ["Pliki z dziennika, których nie ma już na serwerze:", ""]
        for p in missing:
            e = p.entry
            lines.append(
                f"{_when(e.at, zone)}\t{KIND_LABELS.get(e.kind, e.kind)}\t{e.account or SERVER_ACCOUNT}"
                f"\t{p.info.get('filename') or '?'}\t{p.reason or 'brak pliku'}"
            )
        small("BRAKUJACE.txt", ("\n".join(lines) + "\n").encode("utf-8"))
    archive.close()
    yield sink.drain()
