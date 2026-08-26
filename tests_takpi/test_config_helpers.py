from datetime import datetime, timezone

from opentakserver.config_helpers import (
    subject_alt_names,
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


# --- subject_alt_names -------------------------------------------------------------
#
# The server certificate used to carry a single address. A field server reachable both
# over its own access point and over a VPN needs every address it answers on, or the TLS
# handshake fails for whichever address was left out.


def test_alt_names_single_ipv4():
    assert subject_alt_names("192.168.4.1") == "IP.1 = 192.168.4.1"


def test_alt_names_single_hostname():
    assert subject_alt_names("takpi.local") == "DNS.1 = takpi.local"


def test_alt_names_numbers_each_kind_separately():
    block = subject_alt_names("192.168.4.1", ["100.64.0.5", "takpi.example"])
    assert block == "IP.1 = 192.168.4.1\nIP.2 = 100.64.0.5\nDNS.1 = takpi.example"


def test_alt_names_keeps_common_name_first():
    block = subject_alt_names("10.0.0.1", ["10.0.0.2"])
    assert block.splitlines()[0] == "IP.1 = 10.0.0.1"


def test_alt_names_drops_duplicates():
    block = subject_alt_names("10.0.0.1", ["10.0.0.1", "10.0.0.2", "10.0.0.2"])
    assert block == "IP.1 = 10.0.0.1\nIP.2 = 10.0.0.2"


def test_alt_names_ignores_blanks():
    assert subject_alt_names("10.0.0.1", ["", None, "  "]) == "IP.1 = 10.0.0.1"


def test_alt_names_accepts_no_extras():
    assert subject_alt_names("10.0.0.1", None) == "IP.1 = 10.0.0.1"


def test_alt_names_trims_whitespace():
    assert subject_alt_names(" 10.0.0.1 ", [" 10.0.0.2 "]) == "IP.1 = 10.0.0.1\nIP.2 = 10.0.0.2"


# config.yml delivers this setting as plain text, while the environment variable arrives
# already split. Both have to work, or a certificate ends up with one entry per character.


def test_alt_names_accepts_comma_separated_text():
    block = subject_alt_names("10.0.0.1", "10.0.0.2,10.0.0.3")
    assert block == "IP.1 = 10.0.0.1\nIP.2 = 10.0.0.2\nIP.3 = 10.0.0.3"


def test_alt_names_accepts_single_address_as_text():
    assert subject_alt_names("10.0.0.1", "10.0.0.2") == "IP.1 = 10.0.0.1\nIP.2 = 10.0.0.2"


def test_alt_names_text_tolerates_spaces_and_empties():
    block = subject_alt_names("10.0.0.1", " 10.0.0.2 , , 10.0.0.3 ")
    assert block == "IP.1 = 10.0.0.1\nIP.2 = 10.0.0.2\nIP.3 = 10.0.0.3"


def test_alt_names_empty_text_changes_nothing():
    assert subject_alt_names("10.0.0.1", "") == "IP.1 = 10.0.0.1"
