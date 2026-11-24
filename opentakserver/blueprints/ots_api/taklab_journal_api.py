"""taklab: /api/taklab/journal - environment journal (contract in the design).

Design: ~/taklab docs/superpowers/specs/2026-11-24-journal-design.md.

Who is asking, in this order (as /api/taklab/recordings):
  1. a client certificate forwarded by marti-tls in X-Ssl-Cert,
  2. a Flask-Security session or Authentication-Token.
No account = 401. Only the "administrator" role reads the journal; every other
account gets 403 on every route except /access. Entries are read-only here:
there is no route that changes or deletes them.
"""

from cryptography.x509.oid import NameOID
from flask import Blueprint, Response, jsonify, request, stream_with_context
from flask_security import current_user
from flask_security.decorators import _check_session, _check_token
from sqlalchemy import or_, select

from opentakserver import taklab_journal as journal
from opentakserver import taklab_journal_export as export
from opentakserver import taklab_recordings as rec_core
from opentakserver.blueprints.marti_api.marti_api import verify_client_cert
from opentakserver.extensions import db
from opentakserver.models.TaklabJournal import KINDS, SERVER_ACCOUNT, TaklabJournal

taklab_journal_api = Blueprint("taklab_journal_api", __name__)

PER_PAGE = 50
MAX_PER_PAGE = 200

MSG_AUTH = "Wymagane logowanie."
MSG_ADMIN = "Dziennik zdarzeń widzi tylko administrator."
MSG_TOO_MANY = (
    "Za dużo wpisów do PDF ({count}, najwyżej {limit}). Zawęź zakres czasu, konta albo rodzaje."
)


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


def _admin():
    """(viewer, refusal): 401 without an account, 403 without the administrator role."""
    viewer = _viewer()
    if viewer is None:
        return None, _error(MSG_AUTH, 401)
    if not rec_core.is_admin(viewer):
        return None, _error(MSG_ADMIN, 403)
    return viewer, None


class FilterError(ValueError):
    pass


def _list_arg(name: str) -> list[str]:
    raw = request.args.get(name) or ""
    return [part.strip() for part in raw.split(",") if part.strip()]


def _filters() -> dict:
    """The filter of the request (same for the list and the exports)."""
    found = {}
    for key in ("since", "until"):
        raw = request.args.get(key)
        if raw:
            moment = rec_core.parse_time(raw)
            if moment is None:
                raise FilterError(f"Nieprawidłowa data w polu {key}.")
            found[key] = moment
        else:
            found[key] = None
    found["account"] = _list_arg("account")
    kinds = _list_arg("kind")
    unknown = [k for k in kinds if k not in KINDS]
    if unknown:
        raise FilterError(f"Nieznany rodzaj wpisu: {', '.join(unknown)}.")
    found["kind"] = kinds
    found["q"] = (request.args.get("q") or "").strip()[:200]
    found["positions"] = request.args.get("positions") in ("1", "true")
    return found


def _described(filters: dict) -> dict:
    """The filter as JSON (export audit entry, PDF title page)."""
    return {
        "since": filters["since"] and filters["since"].isoformat() + "Z",
        "until": filters["until"] and filters["until"].isoformat() + "Z",
        "account": ",".join(filters["account"]) or None,
        "kind": ",".join(filters["kind"]) or None,
        "q": filters["q"] or None,
        "positions": filters["positions"],
    }


def _time_and_account(query, filters: dict):
    table = TaklabJournal
    if filters["since"] is not None:
        query = query.where(table.at >= filters["since"])
    if filters["until"] is not None:
        query = query.where(table.at <= filters["until"])
    accounts = filters["account"]
    if accounts:
        named = [a for a in accounts if a != SERVER_ACCOUNT]
        conditions = [table.account.in_(named)] if named else []
        if SERVER_ACCOUNT in accounts:
            conditions.append(table.account.is_(None))
        query = query.where(or_(*conditions))
    return query


def _query(filters: dict):
    query = _time_and_account(select(TaklabJournal), filters)
    if filters["kind"]:
        query = query.where(TaklabJournal.kind.in_(filters["kind"]))
    elif not filters["positions"]:
        query = query.where(TaklabJournal.kind != "position")
    if filters["q"]:
        escaped = filters["q"].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        query = query.where(
            or_(
                TaklabJournal.title.ilike(pattern, escape="\\"),
                TaklabJournal.text.ilike(pattern, escape="\\"),
                TaklabJournal.callsign.ilike(pattern, escape="\\"),
            )
        )
    return query


def _ordered(query):
    return query.order_by(TaklabJournal.at, TaklabJournal.id)


def _positions(filters: dict) -> list:
    """Every position in the time and account range (participants, GPX), as light rows."""
    t = TaklabJournal
    query = select(t.id, t.at, t.account, t.callsign, t.device_uid, t.latitude, t.longitude).where(
        t.kind == "position"
    )
    query = _time_and_account(query, filters).order_by(t.at, t.id)
    return db.session.execute(query).all()


def _count(query) -> int:
    return db.session.execute(select(db.func.count()).select_from(query.subquery())).scalar() or 0


def _parse_or_error():
    try:
        return _filters(), None
    except FilterError as e:
        return None, _error(str(e), 400)


@taklab_journal_api.route("/api/taklab/journal/access", methods=["GET"])
def journal_access():
    viewer = _viewer()
    if viewer is None:
        return _error(MSG_AUTH, 401)
    return _reply({"admin": rec_core.is_admin(viewer)})


@taklab_journal_api.route("/api/taklab/journal", methods=["GET"])
def journal_list():
    viewer, refusal = _admin()
    if refusal:
        return refusal
    filters, refusal = _parse_or_error()
    if refusal:
        return refusal
    try:
        page = max(1, int(request.args.get("page", 1)))
        per_page = min(MAX_PER_PAGE, max(1, int(request.args.get("per_page", PER_PAGE))))
    except ValueError:
        return _error("Nieprawidłowy numer strony.", 400)
    query = _query(filters)
    total = _count(query)
    rows = db.session.execute(
        _ordered(query).offset((page - 1) * per_page).limit(per_page)
    ).scalars().all()
    items = [r.to_json(export.is_available(r.file_info()) if r.file else None) for r in rows]
    return _reply({"items": items, "total": total, "page": page, "per_page": per_page})


@taklab_journal_api.route("/api/taklab/journal/accounts", methods=["GET"])
def journal_accounts():
    viewer, refusal = _admin()
    if refusal:
        return refusal
    rows = db.session.execute(
        select(TaklabJournal.account, TaklabJournal.callsign)
        .distinct()
        .order_by(TaklabJournal.account, TaklabJournal.callsign)
    ).all()
    found: dict[str, set] = {}
    for account, callsign in rows:
        key = account or SERVER_ACCOUNT
        found.setdefault(key, set())
        if callsign:
            found[key].add(callsign)
    accounts = [
        {"account": key, "callsigns": sorted(found[key], key=str.lower)}
        for key in sorted(found, key=str.lower)
    ]
    return _reply({"accounts": accounts})


@taklab_journal_api.route("/api/taklab/journal/export/estimate", methods=["GET"])
def journal_estimate():
    viewer, refusal = _admin()
    if refusal:
        return refusal
    filters, refusal = _parse_or_error()
    if refusal:
        return refusal
    entries = db.session.execute(_ordered(_query(filters))).scalars().all()
    return _reply(export.estimate(entries))


def _meta(viewer, filters: dict, config) -> export.PdfMeta:
    return export.PdfMeta(
        since=filters["since"],
        until=filters["until"],
        generated_by=viewer.username,
        generated_at=journal.utcnow(),
        zone=config.timezone,
        font_dir=config.font_dir,
        filters=_described(filters),
    )


def _stamp() -> str:
    return journal.local(journal.utcnow(), journal.settings().timezone).strftime("%Y%m%d-%H%M%S")


@taklab_journal_api.route("/api/taklab/journal/export.pdf", methods=["GET"])
def journal_pdf():
    viewer, refusal = _admin()
    if refusal:
        return refusal
    filters, refusal = _parse_or_error()
    if refusal:
        return refusal
    config = journal.settings()
    query = _query(filters)
    count = _count(query)
    if count > config.pdf_limit:
        return _error(MSG_TOO_MANY.format(count=count, limit=config.pdf_limit), 413)
    entries = db.session.execute(_ordered(query)).scalars().all()
    data = export.build_pdf(entries, _positions(filters), _meta(viewer, filters, config))
    journal.record_export(viewer.username, "pdf", _described(filters), len(entries))
    response = Response(data, mimetype="application/pdf")
    response.headers["Content-Disposition"] = f'attachment; filename="dziennik-{_stamp()}.pdf"'
    response.headers["Cache-Control"] = "no-store"
    return response


@taklab_journal_api.route("/api/taklab/journal/export.zip", methods=["GET"])
def journal_zip():
    viewer, refusal = _admin()
    if refusal:
        return refusal
    filters, refusal = _parse_or_error()
    if refusal:
        return refusal
    config = journal.settings()
    entries = db.session.execute(_ordered(_query(filters))).scalars().all()
    positions = _positions(filters)
    planned = export.plan_files(entries)
    pdf_bytes, pdf_note = None, None
    if len(entries) <= config.pdf_limit:
        pdf_bytes = export.build_pdf(entries, positions, _meta(viewer, filters, config))
    else:
        pdf_note = (
            f"PDF pominięty: {len(entries)} wpisów, najwyżej {config.pdf_limit}. "
            "Pełna lista jest w dziennik.csv i dziennik.json.\n"
        )
    journal.record_export(viewer.username, "zip", _described(filters), len(entries))
    stream = export.stream_zip(
        entries, positions, planned, pdf_bytes, pdf_note, config.timezone, journal.utcnow()
    )
    response = Response(stream_with_context(stream), mimetype="application/zip")
    response.headers["Content-Disposition"] = f'attachment; filename="dziennik-{_stamp()}.zip"'
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Accel-Buffering"] = "no"
    return response
