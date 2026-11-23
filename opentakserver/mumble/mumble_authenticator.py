import os

import Ice
from flask import Flask
from flask_ldap3_login import AuthenticationResponseStatus
from flask_security import verify_password

from ..extensions import ldap_manager
from .auth_rules import decide, parse_display_name

# Load up Murmur slice file into Ice (Ice 3.8: one list argument)
Ice.loadSlice(
    [
        "-I" + Ice.getSliceDir(),
        os.path.join(os.path.dirname(os.path.realpath(__file__)), "Murmur.ice"),
    ]
)
import Murmur  # noqa: E402


class MumbleAuthenticator(Murmur.ServerUpdatingAuthenticator):
    """Murmur authenticator backed by OTS accounts.

    Ice 3.8 marshals return values by their Slice types, so every method
    returns a value of the declared type (no None for strings/sequences).
    """

    def __init__(self, app, logger, ice):
        Murmur.ServerUpdatingAuthenticator.__init__(self)
        self.app: Flask = app
        self.logger = logger
        self.ice = ice

    def authenticate(self, username, password, certlist, certhash, strong, current=None):
        """
        This function is called to authenticate a user.

        taklab: "<callsign> (<account>)" with the account's enrollment token or
        password, see opentakserver.mumble.auth_rules.
        """
        from opentakserver.models.Token import Token

        with self.app.app_context():

            def find_user(account):
                return self.app.security.datastore.find_user(username=account)

            def check_password(given, stored):
                if self.app.config.get("OTS_ENABLE_LDAP"):
                    parsed = parse_display_name(username)
                    if not parsed:
                        return False
                    result = ldap_manager.authenticate(parsed[1], given)
                    return result.status == AuthenticationResponseStatus.success
                return bool(stored) and verify_password(given, stored)

            user_id, new_name = decide(
                username,
                password or "",
                find_user,
                Token.belongs_to,
                check_password,
                self.app.config.get("OTS_MUMBLE_SHARED_PASSWORD") or "",
            )

        if user_id >= 0:
            self.logger.info("Mumble auth: {} has been authenticated".format(new_name))
        elif user_id == -1:
            self.logger.warning("Mumble auth: rejected {}".format(username))
        return user_id, new_name or "", []

    def idToTexture(self, id, current=None):
        return b""

    def getInfo(self, id, current=None):
        # No additional information; fall through
        return False, {}

    def nameToId(self, name, current=None):
        # Unknown here; fall through to Murmur
        return -2

    def idToName(self, id, current=None):
        return ""

    def registerUser(self, name, current=None):
        # taklab: accounts live in OTS, so Mumble self-registration is refused.
        return -1

    def unregisterUser(self, id, current=None):
        return -2

    def getRegisteredUsers(self, filter, current=None):
        return {}

    def setInfo(self, id, info, current=None):
        return -1

    def setTexture(self, id, texture, current=None):
        return -1
