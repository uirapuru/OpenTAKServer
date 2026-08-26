from datetime import datetime, timezone

from opentakserver.config_helpers import (
    mumble_ice_proxy,
    not_before_argument,
    rabbitmq_message_queue_url,
)

CHWILA = datetime(2026, 8, 26, 12, 0, 0, tzinfo=timezone.utc)


def test_url_carries_credentials():
    config = {
        "OTS_RABBITMQ_USERNAME": "ots",
        "OTS_RABBITMQ_PASSWORD": "tajne",
        "OTS_RABBITMQ_SERVER_ADDRESS": "rabbitmq",
    }
    assert rabbitmq_message_queue_url(config.get) == "amqp://ots:tajne@rabbitmq"


def test_url_percent_encodes_special_characters():
    config = {
        "OTS_RABBITMQ_USERNAME": "ots",
        "OTS_RABBITMQ_PASSWORD": "a/b@c:d",
        "OTS_RABBITMQ_SERVER_ADDRESS": "rabbitmq",
    }
    assert rabbitmq_message_queue_url(config.get) == "amqp://ots:a%2Fb%40c%3Ad@rabbitmq"


def test_ice_proxy_uses_defaults_when_config_is_empty():
    assert mumble_ice_proxy({}.get) == "Meta:tcp -h 127.0.0.1 -p 6502"


def test_ice_proxy_uses_configured_host_and_port():
    config = {"OTS_MUMBLE_ICE_HOST": "mumble", "OTS_MUMBLE_ICE_PORT": 6502}
    assert mumble_ice_proxy(config.get) == "Meta:tcp -h mumble -p 6502"


def test_no_argument_when_backdating_is_off():
    assert not_before_argument({}.get, CHWILA) == ""
    assert not_before_argument({"OTS_CERT_BACKDATE_DNI": 0}.get, CHWILA) == ""


def test_argument_is_shifted_back_by_the_configured_number_of_days():
    result = not_before_argument({"OTS_CERT_BACKDATE_DNI": 365}.get, CHWILA)
    assert result == " -not_before 250826120000Z"


def test_negative_value_is_treated_as_off():
    assert not_before_argument({"OTS_CERT_BACKDATE_DNI": -5}.get, CHWILA) == ""
