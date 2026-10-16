"""Reguły misji wspólnej (OTS_SHARED_MISSION).

Obiekt rozgłoszony (zdarzenie bez żadnego <dest>) trafia do misji wspólnej, gdy jest
rysunkiem, trasą, punktem albo jednostką, ale nie jest własną pozycją nadawcy, czatem
ani zdarzeniem grupy, ani śladem krótszym niż 300 s. t-x-d-d z <__forcedelete/>
zdejmuje obiekt z misji wspólnej.

Moduł zależy tylko od bs4, żeby dało się go testować bez aplikacji.
"""

from datetime import datetime

SHAREABLE_PREFIXES = ("u-d-", "b-m-p-", "a-")
SHAREABLE_TYPES = ("b-m-r",)
NEVER_PREFIXES = ("t-", "b-t-f", "y-", "b-a-")
# Ślady krótkotrwałe (UAS, SPI, czujniki) mają stale krócej niż start + 300 s
# i nie należą do mapy wspólnej
MIN_SHARED_LIFETIME_SECONDS = 300
# Konta techniczne federacji zakładane przez taklab
FEDERATION_USERNAME_PREFIX = "fed_"


def parse_cot_time(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def is_transient(event) -> bool:
    """True dla zdarzenia żyjącego krócej niż 300 s; brak lub zły czas to nie ślad."""
    start = parse_cot_time(event.attrs.get("start"))
    stale = parse_cot_time(event.attrs.get("stale"))
    if start is None or stale is None:
        return False
    try:
        return (stale - start).total_seconds() < MIN_SHARED_LIFETIME_SECONDS
    except TypeError:
        # Jeden czas ze strefą, drugi bez
        return False


def is_broadcast(event) -> bool:
    return event.find("dest") is None


def is_shareable(event, sender_uid) -> bool:
    """True, gdy rozgłoszone zdarzenie ma trafić do misji wspólnej."""
    if not sender_uid or not is_broadcast(event):
        return False

    event_type = event.attrs.get("type", "")
    if event_type.startswith(NEVER_PREFIXES):
        return False
    if not (event_type in SHAREABLE_TYPES or event_type.startswith(SHAREABLE_PREFIXES)):
        return False

    # Własna pozycja nadawcy, zdarzenia czatu i grup nie należą do mapy wspólnej
    if event.attrs.get("uid") == sender_uid:
        return False
    if event.find("__chat") is not None or event.find("__group") is not None:
        return False
    if is_transient(event):
        return False

    return True


def forced_delete_target(event) -> str | None:
    """uid obiektu usuwanego przez t-x-d-d z <__forcedelete/>, inaczej None."""
    if event.attrs.get("type") != "t-x-d-d" or event.find("__forcedelete") is None:
        return None

    detail = event.find("detail")
    link = detail.find("link") if detail is not None else None
    if link is None:
        return None
    return link.attrs.get("uid") or None
