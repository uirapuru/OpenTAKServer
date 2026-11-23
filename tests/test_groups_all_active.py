"""/Marti/api/groups/all oddaje prawdziwy stan kanału (GroupUser.enabled).

Upstream wpisywał zawsze active=True. CloudTAK pobiera tę listę przy
wczytaniu mapy, więc kanał wyłączony przełącznikiem wracał na ekranie jako
włączony, choć serwer nic na nim nie wysyłał (taklab, 23 XI 2026).
"""

import importlib
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def cert_user(monkeypatch):
    module = importlib.import_module("opentakserver.blueprints.marti_api.group_marti_api")
    current = {"username": "alice"}

    def fake_cert():
        cert = MagicMock()
        cert.subject.get_attributes_for_oid.return_value = [MagicMock(value=current["username"])]
        return cert

    monkeypatch.setattr(module, "verify_client_cert", fake_cert)
    return current


def _membership(app, username, group_name, direction, enabled):
    from opentakserver.extensions import db
    from opentakserver.models.Group import Group
    from opentakserver.models.GroupUser import GroupUser

    with app.app_context():
        user = app.security.datastore.find_user(username=username)
        if user is None:
            user = app.security.datastore.create_user(username=username, password="x")
            db.session.commit()
        group = db.session.query(Group).filter_by(name=group_name).first()
        if group is None:
            group = Group()
            group.name = group_name
            group.type = Group.SYSTEM
            group.bitpos = 3
            db.session.add(group)
            db.session.commit()
        db.session.add(GroupUser(user_id=user.id, group_id=group.id, direction=direction, enabled=enabled))
        db.session.commit()


def _groups(client):
    response = client.get("/Marti/api/groups/all")
    assert response.status_code == 200, response.text
    return {(g["name"], g["direction"]): g["active"] for g in response.json["data"]}


def test_disabled_membership_is_reported_inactive(app, client, cert_user):
    _membership(app, "alice", "__ANON__", "IN", False)
    _membership(app, "alice", "__ANON__", "OUT", False)
    _membership(app, "alice", "biali", "IN", True)
    _membership(app, "alice", "biali", "OUT", True)
    assert _groups(client) == {
        ("__ANON__", "IN"): False,
        ("__ANON__", "OUT"): False,
        ("biali", "IN"): True,
        ("biali", "OUT"): True,
    }


def test_user_without_groups_still_gets_active_anon(app, client, cert_user):
    from opentakserver.extensions import db

    with app.app_context():
        app.security.datastore.create_user(username="alice", password="x")
        db.session.commit()
    assert _groups(client) == {("__ANON__", "OUT"): True, ("__ANON__", "IN"): True}
