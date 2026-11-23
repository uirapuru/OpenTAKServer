"""Logowanie do Mumble kontem OTS, bez Ice (testowalne bez Murmura).

Decyzja taklab z 23 XI 2026 (wtyczka ATAK TakLab): nazwa w Mumble to
"<znak wywoławczy> (<konto>)", hasło to token rejestracji albo hasło konta.
Wspólne hasło środowiska nie trafia do telefonów; przepuszczamy je dalej do
Murmura (-2), który sam sprawdza serverpassword. Tak łączą się połączenia
techniczne w klastrze (rejestr głosu, echo, PTT z CloudTAK). SuperUser także
idzie dalej, bo jego hasło trzyma Murmur.
"""

import re
import zlib
from typing import Callable

# Konto jak w UsernameValidator OTS: litery i cyfry Unicode, "_" i ".".
_DISPLAY_NAME = re.compile(r"^(?P<callsign>.*\S)\s*\((?P<account>[\w.]+)\)$")

FALL_THROUGH = -2
REJECT = -1


def parse_display_name(name: str) -> tuple[str, str] | None:
    """("Alfa 1 (pluton1)") -> ("Alfa 1", "pluton1"); konto z ostatniego nawiasu."""
    match = _DISPLAY_NAME.match(name or "")
    if not match:
        return None
    return match.group("callsign").strip(), match.group("account")


def session_id(account: str, callsign: str) -> int:
    """Stały identyfikator pary konto+znak. Kilka osób na jednym koncie ma
    różne znaki, więc różne identyfikatory i nie wyrzucają się nawzajem; ten
    sam znak na tym samym koncie wyrzuca poprzednie połączenie, jak w Mumble."""
    return 1_000_000_000 + zlib.crc32(f"{account}\0{callsign}".encode()) % 1_000_000_000


def decide(
    name: str,
    password: str,
    find_user: Callable[[str], object],
    token_belongs_to: Callable[[str, str], bool],
    verify_password: Callable[[str, str], bool],
    shared_password: str,
) -> tuple[int, str | None]:
    """(identyfikator, nowa nazwa) dla Murmura: >=0 wpuść, -1 odrzuć, -2 dalej."""
    if name == "SuperUser":
        return FALL_THROUGH, None
    if shared_password and password == shared_password:
        return FALL_THROUGH, None

    parsed = parse_display_name(name)
    if parsed is None:
        return REJECT, None
    callsign, account = parsed

    user = find_user(account)
    if user is None or not getattr(user, "active", False):
        return REJECT, None

    if token_belongs_to(password, account) or (
        password and verify_password(password, getattr(user, "password", None))
    ):
        return session_id(account, callsign), name
    return REJECT, None
