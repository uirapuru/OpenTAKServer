"""Pure helpers that turn configuration values into strings.

Nothing outside the standard library is pulled in here, on purpose. Every
other module that holds these strings today (functions.py, app.py) drags in
pika and flask, which makes a test for a piece of string building depend on a
full OpenTAKServer install.

Each helper takes a getter - app.config.get, or dict.get in tests - instead of
a mapping, so a plain dictionary is enough to exercise them.
"""

from datetime import timedelta
from urllib.parse import quote

NOT_BEFORE_FORMAT = "%y%m%d%H%M%SZ"


def rabbitmq_message_queue_url(config) -> str:
    """Build the AMQP URL that Flask-SocketIO uses as its message queue.

    OTS_RABBITMQ_SERVER_ADDRESS is also handed to pika as a bare host name
    (pika.ConnectionParameters(host=...)), so credentials must never be
    stored in it. Take them from OTS_RABBITMQ_USERNAME/OTS_RABBITMQ_PASSWORD,
    the same values the pika connections already use.

    Without credentials Flask-SocketIO falls back to the default guest user.
    On any broker that disabled guest (or is not on localhost) RabbitMQ
    answers (403) ACCESS_REFUSED, every socketio.emit() then raises, and the
    web UI silently stops receiving live events.
    """
    host = config("OTS_RABBITMQ_SERVER_ADDRESS") or "127.0.0.1"

    # Someone may already have put credentials into the address. Leave it be.
    if "@" in host:
        return "amqp://{}".format(host)

    username = config("OTS_RABBITMQ_USERNAME")
    if not username:
        return "amqp://{}".format(host)

    password = config("OTS_RABBITMQ_PASSWORD") or ""
    return "amqp://{}:{}@{}".format(
        quote(str(username), safe=""), quote(str(password), safe=""), host
    )


def mumble_ice_proxy(config) -> str:
    """Build the Ice proxy string for the Mumble server's Meta interface.

    The address was hard coded to 127.0.0.1, which only holds when Mumble and
    OpenTAKServer share a machine. In a container deployment Mumble lives
    under its own host name and the daemon otherwise connects to itself.
    """
    host = config("OTS_MUMBLE_ICE_HOST") or "127.0.0.1"
    port = config("OTS_MUMBLE_ICE_PORT") or 6502
    return "Meta:tcp -h {} -p {}".format(host, port)


def not_before_argument(config, now) -> str:
    """Build the openssl -not_before argument, or an empty string.

    OTS_CERT_BACKDATE_DNI shifts the start of validity into the past so that
    certificates stay usable on devices whose clock runs behind - a field
    server has no time source, and a tablet that boots with a stale clock
    rejects a certificate that is not valid yet.

    Returns a leading space with the argument so callers can concatenate the
    result straight into an openssl command line. Zero or a negative number of
    days means the argument is left out entirely: a negative shift would push
    the start of validity into the future, which is the very failure this is
    meant to prevent.
    """
    try:
        days = int(config("OTS_CERT_BACKDATE_DNI") or 0)
    except (TypeError, ValueError):
        return ""

    if days <= 0:
        return ""

    return " -not_before {}".format((now - timedelta(days=days)).strftime(NOT_BEFORE_FORMAT))
