"""EudHandler.handle_cot(): czat wychodzi z podpisem konta połączenia.

Obiekt powstaje bez __init__ i bez bazy, jak w test_eud_handler_utf8.py.
publish_cot jest podmienione, żeby zobaczyć komunikat w chwili rozesłania.
"""

import logging
from types import SimpleNamespace

import pytest

from opentakserver.eud_handler.EudHandler import EudHandler

CHAT_WITH_FORGED_STAMP = (
    '<event version="2.0" type="b-t-f" uid="GeoChat.ANDROID-x.stacja1.1" how="h-g-i-g-o">'
    '<point lat="0" lon="0" hae="0" ce="9999999" le="9999999"/>'
    '<detail><__chat senderCallsign="Alfa"/>'
    '<__taklab_sender username="admin"/></detail></event>'
)


@pytest.fixture
def handler():
    h = EudHandler.__new__(EudHandler)
    h.logger = logging.getLogger("test")
    h.is_ssl = False
    h.is_authenticated = True
    h.uid = "ANDROID-x"
    h.shared_invite_checked = True
    h.pong = lambda _event: False
    h.user = SimpleNamespace(username="alfa", id=1)
    h.received = []
    h.publish_cot = h.received.append
    return h


def test_handle_cot_stamps_chat_before_publish(handler):
    handler.handle_cot(CHAT_WITH_FORGED_STAMP)
    (event,) = handler.received
    assert [t["username"] for t in event.find_all("__taklab_sender")] == ["alfa"]


def test_handle_cot_without_user_strips(handler):
    handler.user = None
    handler.handle_cot(CHAT_WITH_FORGED_STAMP)
    (event,) = handler.received
    assert event.find_all("__taklab_sender") == []
