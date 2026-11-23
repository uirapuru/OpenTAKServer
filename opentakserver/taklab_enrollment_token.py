"""Token rejestracji konta jako hasło do głosu i wideo (wtyczka TakLab).

Ta sama logika co kod ICU w CloudTAK (taklab9): istniejący token konta, nowy
tylko wtedy, gdy konto go nie ma, bo ponowne wydanie zerowałoby ustawienia
tokenu założone gdzie indziej. Token wyłączony, wygasły albo wykorzystany daje
komunikat zamiast hasła, które serwer i tak by odrzucił.
"""

import time

from opentakserver.extensions import db
from opentakserver.models.Token import Token


def enrollment_token(username: str) -> tuple[str | None, str | None]:
    """Zwraca (token, None) albo (None, komunikat po polsku)."""
    token = db.session.query(Token).filter_by(username=username).first()
    if token is None:
        token = Token(username=username, creation=int(time.time()))
        token.hash_token()
        db.session.add(token)
        db.session.commit()

    if token.disabled:
        return None, "Token rejestracji tego konta jest wyłączony w OTS"
    if token.expiration and token.expiration <= time.time():
        return None, "Token rejestracji tego konta wygasł w OTS"
    if token.max_uses and (token.total_uses or 0) >= token.max_uses:
        return None, "Token rejestracji tego konta wykorzystał limit użyć w OTS"

    value = token.generate_token()
    if not value:
        return None, "OTS nie wydał tokenu rejestracji"
    return value, None
