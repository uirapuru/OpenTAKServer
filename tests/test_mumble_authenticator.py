from unittest.mock import MagicMock

import pytest

pytest.importorskip("Ice")

from opentakserver.mumble.mumble_authenticator import MumbleAuthenticator


def make_authenticator():
    auth = MumbleAuthenticator.__new__(MumbleAuthenticator)  # skip __init__/Ice setup
    auth.app = MagicMock()
    auth.logger = MagicMock()
    datastore = auth.app.security.datastore
    datastore.find_user.return_value = None  # -> returns -1 before touching password logic
    return auth, datastore


def test_account_taken_from_parentheses():
    auth, datastore = make_authenticator()

    result = auth.authenticate("Alfa 1 (pluton1)", "password", None, None, None)

    datastore.find_user.assert_called_once_with(username="pluton1")
    assert result == (-1, "", [])


def test_vx_style_name_rejected_without_lookup():
    # taklab 23 XI 2026: Vx dropped; "callsign---uid" is no longer an account name
    auth, datastore = make_authenticator()

    assert auth.authenticate("ANVIL---47c4c853", "password", None, None, None) == (-1, "", [])
    datastore.find_user.assert_not_called()


def test_shared_password_falls_through():
    auth, datastore = make_authenticator()
    auth.app.config.get.side_effect = lambda key, *a: "wspolne" if key == "OTS_MUMBLE_SHARED_PASSWORD" else None

    assert auth.authenticate("ADMIN-CloudTAK", "wspolne", None, None, None) == (-2, "", [])
