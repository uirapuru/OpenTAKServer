import os
import threading
import time
from threading import Timer

import Ice

from opentakserver.config_helpers import mumble_ice_proxy
from opentakserver.mumble.mumble_authenticator import MumbleAuthenticator

# Load up Murmur slice file into Ice
Ice.loadSlice(
    [
        "-I" + Ice.getSliceDir(),
        os.path.join(os.path.dirname(os.path.realpath(__file__)), "Murmur.ice"),
    ]
)
import Murmur  # noqa: E402


class MumbleIceDaemon(threading.Thread):
    def __init__(self, app, logger):
        super().__init__()
        self.app = app
        self.logger = logger
        self.logger.info("mumble daemon init")
        self.daemon = True

    def run(self):
        # Configure Ice properties
        props = Ice.createProperties()
        props.setProperty("Ice.ImplicitContext", "Shared")
        props.setProperty("Ice.Default.InvocationTimeout", str(30 * 1000))
        props.setProperty("Ice.MessageSizeMax", str(1024))
        idata = Ice.InitializationData()
        idata.properties = props

        ice = Ice.initialize(initData=idata)
        mumble_ice_app = MumbleIceApp(self.app, self.logger, ice)
        # taklab: Murmur in the same pod may start after OTS; keep trying
        # instead of giving up on the first refused connection.
        while not mumble_ice_app.start():
            self.logger.warning("Mumble Ice not ready, retrying in 10 s")
            time.sleep(10)
        # Ice 3.8 waitForShutdown() keeps the GIL and would freeze the whole
        # OTS process; the adapter serves callbacks on Ice threads anyway.
        threading.Event().wait()


class MumbleIceApp:
    def __init__(self, app, logger, ice):
        self.app = app
        self.logger = logger
        self.ice = ice
        self.meta = None
        self.metacb = None
        self.connected = False
        self.failed_watch = False
        self.watchdog = None
        self.auth = None
        self.adapter = None

    def start(self) -> bool:
        """Attach the authenticator once; the watchdog then re-attaches it
        every 10 s, so it survives a Murmur restart (taklab: the original
        cancelled the watchdog right after starting it)."""
        try:
            if not self.initialize_ice_connection():
                return False
        except Ice.Exception as e:
            self.logger.warning("Mumble Ice connection failed: {}".format(e))
            return False
        self.watchdog = Timer(10, self.check_connection)
        self.watchdog.daemon = True
        self.watchdog.start()
        return True

    def initialize_ice_connection(self):
        """
        Establishes the two-way Ice connection and adds the authenticator to the
        configured servers
        """

        # if False and 'ice_secret':
        #     self.ice.getImplicitContext().put("secret", "some_secret")

        proxy_string = mumble_ice_proxy(self.app.config.get)
        self.logger.debug("Connecting to Ice server ({})".format(proxy_string))
        base = self.ice.stringToProxy(proxy_string)
        self.meta = Murmur.MetaPrx.uncheckedCast(base)

        if self.adapter is None:
            self.adapter = self.ice.createObjectAdapterWithEndpoints(
                "Callback.Client", "tcp -h 127.0.0.1"
            )
            self.adapter.activate()
        adapter = self.adapter

        metacbprx = adapter.addWithUUID(MetaCallback(self))
        self.metacb = Murmur.MetaCallbackPrx.uncheckedCast(metacbprx)

        authprx = adapter.addWithUUID(MumbleAuthenticator(self.app, self.logger, self.ice))
        self.auth = Murmur.ServerUpdatingAuthenticatorPrx.uncheckedCast(authprx)

        return self.attach_callbacks()

    def attach_callbacks(self):
        """
        Attaches all callbacks for meta and authenticators
        """

        try:
            self.logger.debug("Attaching meta callback")

            self.meta.addCallback(self.metacb)

            for server in self.meta.getBootedServers():
                self.logger.debug(
                    "Setting mumble authenticator for virtual server {}".format(server.id())
                )
                server.setAuthenticator(self.auth)

        except (
            Murmur.InvalidSecretException,
            Ice.UnknownUserException,
            Ice.ConnectionRefusedException,
        ) as e:
            if isinstance(e, Ice.ConnectionRefusedException):
                self.logger.warning("Server refused connection")
            elif (
                isinstance(e, Murmur.InvalidSecretException)
                or isinstance(e, Ice.UnknownUserException)
                and (e.unknown == "Murmur::InvalidSecretException")
            ):
                self.logger.error("Invalid ice secret")
            else:
                # We do not actually want to handle this one, re-raise it
                raise e

            self.connected = False
            return False

        self.connected = True
        return True

    def check_connection(self):
        """
        Tries reapplies all callbacks to make sure the authenticator
        survives server restarts and disconnects.
        """

        try:
            self.attach_callbacks()
        except Ice.Exception as e:
            self.logger.warning(
                "{}: Failed connection check, will retry in next watchdog run ({}s)".format(e, 10)
            )

        # Renew the timer
        self.watchdog = Timer(10, self.check_connection)
        self.watchdog.daemon = True
        self.watchdog.start()


class MetaCallback(Murmur.MetaCallback):
    def __init__(self, authenticator):
        Murmur.MetaCallback.__init__(self)
        self.authenticator = authenticator

    def started(self, server, current=None):
        """
        This function is called when a virtual server is started
        and makes sure an authenticator gets attached if needed.
        """
        self.authenticator.logger.info(
            "Setting authenticator for virtual server {}".format(server.id())
        )
        try:
            server.setAuthenticator(self.authenticator.auth)
        # Apparently this server was restarted without us noticing
        except (Murmur.InvalidSecretException, Ice.UnknownUserException) as e:
            if hasattr(e, "unknown") and e.unknown != "Murmur::InvalidSecretException":
                # Special handling for Murmur 1.2.2 servers with invalid slice files
                raise e

            return

    def stopped(self, server, current=None):
        """
        This function is called when a virtual server is stopped
        """
        if self.authenticator.connected:
            # Only try to output the server id if we think we are still connected to prevent
            # flooding of our thread pool
            try:
                self.authenticator.logger.info(
                    "Authenticated virtual server {} got stopped".format(server.id())
                )
                return
            except Ice.ConnectionRefusedException:
                self.authenticator.connected = False

        self.authenticator.logger.info("Server shutdown stopped a virtual server")
