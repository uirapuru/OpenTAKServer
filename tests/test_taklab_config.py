"""/Marti/api/taklab/config: ustawienia głosu i wideo konta dla wtyczki TakLab.

Wtyczka woła tę trasę certyfikatem telefonu (marti-tls przekazuje go w
X-Ssl-Cert), więc konto ustalamy z certyfikatu. Token rejestracji konta jest
hasłem do głosu (uwierzytelnianie Mumble w OTS) i do wideo (/api/external_auth).
"""

import importlib
import json
import time
from unittest.mock import MagicMock

import pytest

CHANNELS = [{"id": 1, "name": "Dowodzenie"}, {"id": 5, "name": "Echo"}]


@pytest.fixture
def taklab_env(app):
    app.config["OTS_TAKLAB_HOST"] = "e.taklab.eu"
    app.config["OTS_TAKLAB_MUMBLE_PORT"] = 30005
    app.config["OTS_TAKLAB_RTSP_PORT"] = 30006
    app.config["OTS_TAKLAB_MUMBLE_CHANNELS"] = json.dumps(CHANNELS)
    return app


@pytest.fixture
def cert_user(monkeypatch):
    module = importlib.import_module("opentakserver.blueprints.marti_api.taklab_config_marti_api")
    current = {"username": "alice"}

    def fake_cert():
        if current["username"] is None:
            return None
        cert = MagicMock()
        cert.subject.get_attributes_for_oid.return_value = [MagicMock(value=current["username"])]
        return cert

    monkeypatch.setattr(module, "verify_client_cert", fake_cert)
    return current


@pytest.fixture(autouse=True)
def fake_jwt(monkeypatch):
    from opentakserver.models.Token import Token

    monkeypatch.setattr(Token, "generate_token", lambda self: f"JWT-{self.username}-{self.id}")


def _user(app, username="alice", active=True):
    from opentakserver.extensions import db

    with app.app_context():
        user = app.security.datastore.create_user(username=username, password="x", active=active)
        db.session.commit()
        return user.id


def _token(app, username="alice", **fields):
    from opentakserver.extensions import db
    from opentakserver.models.Token import Token

    with app.app_context():
        token = Token(username=username, creation=int(time.time()), **fields)
        token.hash_token()
        db.session.add(token)
        db.session.commit()
        return token.id


def _tokens(app, username="alice"):
    from opentakserver.extensions import db
    from opentakserver.models.Token import Token

    with app.app_context():
        return db.session.query(Token).filter_by(username=username).count()


def _get(client):
    return client.get("/Marti/api/taklab/config")


def test_config_for_cert_account(taklab_env, client, cert_user):
    _user(taklab_env)
    token_id = _token(taklab_env)
    res = _get(client)
    assert res.status_code == 200, res.text
    assert res.headers["Cache-Control"] == "no-store"
    body = res.json
    assert body["account"] == "alice"
    assert body["server"] == "e.taklab.eu"
    assert body["voice"] == {
        "host": "e.taklab.eu", "port": 30005, "username": "alice",
        "token": f"JWT-alice-{token_id}", "channels": CHANNELS,
    }
    assert body["video"] == {
        "host": "e.taklab.eu", "port": 30006, "path": "alice", "username": "alice",
        "token": f"JWT-alice-{token_id}",
    }
    assert isinstance(body["version"], str) and len(body["version"]) == 16


def test_creates_token_when_missing(taklab_env, client, cert_user):
    _user(taklab_env)
    assert _tokens(taklab_env) == 0
    assert _get(client).status_code == 200
    assert _get(client).status_code == 200
    assert _tokens(taklab_env) == 1


def test_disabled_token_gives_section_error(taklab_env, client, cert_user):
    _user(taklab_env)
    _token(taklab_env, disabled=True)
    body = _get(client).json
    assert set(body["voice"]) == {"error"} and body["voice"]["error"]
    assert set(body["video"]) == {"error"}
    assert "JWT-" not in json.dumps(body)


def test_expired_and_used_up_tokens_give_errors(taklab_env, client, cert_user):
    _user(taklab_env)
    _token(taklab_env, expiration=int(time.time()) - 10)
    assert "error" in _get(client).json["voice"]
    _user(taklab_env, "bob")
    _token(taklab_env, "bob", max_uses=2, total_uses=2)
    cert_user["username"] = "bob"
    assert "error" in _get(client).json["video"]


def test_no_cert_401(taklab_env, client, cert_user):
    cert_user["username"] = None
    assert _get(client).status_code == 401


def test_unknown_or_inactive_account_403(taklab_env, client, cert_user):
    cert_user["username"] = "nobody"
    assert _get(client).status_code == 403
    _user(taklab_env, "carol", active=False)
    cert_user["username"] = "carol"
    assert _get(client).status_code == 403


def test_ports_zero_give_null_sections(taklab_env, client, cert_user):
    _user(taklab_env)
    taklab_env.config["OTS_TAKLAB_MUMBLE_PORT"] = 0
    taklab_env.config["OTS_TAKLAB_RTSP_PORT"] = 0
    body = _get(client).json
    assert body["voice"] is None and body["video"] is None
    assert _tokens(taklab_env) == 0
    taklab_env.config["OTS_TAKLAB_MUMBLE_PORT"] = 30005
    taklab_env.config["OTS_TAKLAB_HOST"] = ""
    assert _get(client).json["voice"] is None


def test_version_changes_with_channels(taklab_env, client, cert_user):
    _user(taklab_env)
    first = _get(client).json["version"]
    assert _get(client).json["version"] == first
    taklab_env.config["OTS_TAKLAB_MUMBLE_CHANNELS"] = json.dumps(CHANNELS[:1])
    assert _get(client).json["version"] != first


def test_channels_read_from_mumble_db(taklab_env, client, cert_user, tmp_path):
    import sqlite3

    db_path = tmp_path / "mumble-server.sqlite"
    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE channels (server_id INTEGER, channel_id INTEGER, parent_id INTEGER, name TEXT)")
    con.executemany(
        "INSERT INTO channels VALUES (?, ?, ?, ?)",
        [(1, 0, None, "Root"), (1, 3, 0, "Pluton 2"), (1, 1, 0, "Dowodzenie"), (1, 7, 0, "Ręczny"), (2, 9, 0, "Inny serwer")],
    )
    con.commit()
    con.close()
    taklab_env.config["OTS_TAKLAB_MUMBLE_DB"] = str(db_path)
    _user(taklab_env)
    assert _get(client).json["voice"]["channels"] == [
        {"id": 1, "name": "Dowodzenie"}, {"id": 3, "name": "Pluton 2"}, {"id": 7, "name": "Ręczny"},
    ]


def test_unreadable_mumble_db_falls_back_to_env(taklab_env, client, cert_user, tmp_path):
    taklab_env.config["OTS_TAKLAB_MUMBLE_DB"] = str(tmp_path / "missing.sqlite")
    _user(taklab_env)
    assert _get(client).json["voice"]["channels"] == CHANNELS
