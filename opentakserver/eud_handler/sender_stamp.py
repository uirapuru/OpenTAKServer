"""Taklab: serwer podpisuje nadawcę wiadomości czatu.

Nadawcę czatu wpisuje klient, więc każde konto mogłoby podać się za inne.
Przed rozesłaniem komunikatu serwer usuwa element SENDER_TAG przysłany przez
klienta (w każdym typie komunikatu) i do czatu (b-t-f) dopisuje konto, które
rozpoznał przy połączeniu. Odbiorca, np. stacja leśna wtyczki TakLab, wierzy
tylko temu elementowi.
"""

from bs4 import BeautifulSoup
from bs4.element import Tag

SENDER_TAG = "__taklab_sender"
CHAT_TYPE = "b-t-f"


def stamp_sender(event: Tag, username: str | None) -> None:
    for tag in event.find_all(SENDER_TAG):
        tag.decompose()

    if event.get("type") != CHAT_TYPE or not username:
        return

    factory = BeautifulSoup("", "xml")
    detail = event.find("detail")
    if detail is None:
        detail = factory.new_tag("detail")
        event.append(detail)
    detail.append(factory.new_tag(SENDER_TAG, attrs={"username": username}))
