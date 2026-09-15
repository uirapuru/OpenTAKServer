"""A callsign belongs to one device at a time.

Upstream lets the second device through the handler and relies on the INSERT
failing, then retries as an UPDATE keyed on the new uid. That UPDATE matches no
row, so the device ends up with no EUD record and no error is raised anywhere.
On a server anyone may join, two strangers picking the same callsign is a matter
of time.
"""

import pytest

from opentakserver.eud_handler.callsign import callsign_owner


@pytest.fixture
def session(app):
    from opentakserver.extensions import db

    with app.app_context():
        yield db.session


def _add_eud(session, uid, callsign):
    from opentakserver.models.EUD import EUD

    eud = EUD()
    eud.uid = uid
    eud.callsign = callsign
    session.add(eud)
    session.commit()
    return eud


def test_free_callsign_has_no_owner(session):
    assert callsign_owner(session, "Alpha", "device-a") is None


def test_taken_callsign_names_the_device_holding_it(session):
    _add_eud(session, "device-a", "Alpha")

    owner = callsign_owner(session, "Alpha", "device-b")

    assert owner is not None
    assert owner.uid == "device-a"


def test_device_reconnecting_under_its_own_callsign_is_not_a_collision(session):
    """Every reconnect runs through this check. Treating a device's own
    callsign as taken would lock it out of its own server."""
    _add_eud(session, "device-a", "Alpha")

    assert callsign_owner(session, "Alpha", "device-a") is None


def test_missing_callsign_never_collides(session):
    """euds.callsign is nullable and CoT relayed from an RF network may carry
    no <contact> tag, so several EUDs legitimately have no callsign."""
    _add_eud(session, "device-a", None)

    assert callsign_owner(session, None, "device-b") is None
    assert callsign_owner(session, "", "device-b") is None


def test_another_callsign_does_not_collide(session):
    _add_eud(session, "device-a", "Alpha")

    assert callsign_owner(session, "Bravo", "device-b") is None
