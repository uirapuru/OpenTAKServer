"""Podpis nadawcy czatu: serwer dopisuje konto rozpoznane przy połączeniu.

Nadawcę wiadomości czatu (<link uid>, <__chat senderCallsign>) wpisuje klient,
więc da się go podrobić. Stacja leśna wtyczki TakLab przyjmuje polecenia tylko
od kont z listy i wierzy wyłącznie elementowi <__taklab_sender>, który dopisuje
serwer. Element podrzucony przez klienta znika z każdego komunikatu.
"""

from bs4 import BeautifulSoup

from opentakserver.eud_handler.sender_stamp import SENDER_TAG, stamp_sender

CHAT = (
    '<event version="2.0" type="b-t-f" uid="GeoChat.A.B.1" how="h-g-i-g-o">'
    '<point lat="0" lon="0" hae="0" ce="9999999" le="9999999"/>'
    '<detail><__chat senderCallsign="A"/></detail></event>'
)


def ev(xml):
    return BeautifulSoup(xml, "xml").find("event")


def test_chat_gets_server_username():
    e = ev(CHAT)
    stamp_sender(e, "alfa")
    tags = e.find_all(SENDER_TAG)
    assert len(tags) == 1
    assert tags[0]["username"] == "alfa"
    assert tags[0].parent.name == "detail"


def test_client_supplied_stamp_removed_and_replaced():
    e = ev(
        '<event type="b-t-f" uid="x"><detail>'
        '<__taklab_sender username="admin"/>'
        '<__chat senderCallsign="A"><__taklab_sender username="admin"/></__chat>'
        "</detail></event>"
    )
    stamp_sender(e, "alfa")
    assert [t["username"] for t in e.find_all(SENDER_TAG)] == ["alfa"]


def test_no_user_strips_and_does_not_stamp():
    e = ev(CHAT.replace("<__chat", '<__taklab_sender username="admin"/><__chat'))
    stamp_sender(e, None)
    assert e.find_all(SENDER_TAG) == []


def test_non_chat_stripped_not_stamped():
    e = ev('<event type="a-f-G" uid="x"><detail><__taklab_sender username="admin"/></detail></event>')
    stamp_sender(e, "alfa")
    assert e.find_all(SENDER_TAG) == []


def test_chat_without_detail_gets_detail():
    e = ev('<event type="b-t-f" uid="x"><point lat="0" lon="0"/></event>')
    stamp_sender(e, "alfa")
    assert e.find("detail").find(SENDER_TAG)["username"] == "alfa"


def test_special_characters_survive_serialisation():
    e = ev(CHAT)
    stamp_sender(e, 'Żaba&"<1>')
    again = ev(str(e))
    assert again.find(SENDER_TAG)["username"] == 'Żaba&"<1>'
