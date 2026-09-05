"""The credential check every other service leans on.

MediaMTX asks this route before it lets a phone publish video, and the XMPP
server asks it before it lets a phone into chat. Both send the same thing: the
account name and whatever secret the phone happens to hold.

Which secret that is depends on how the operator joined. A connection package
carries a certificate and the operator types the password by hand; a QR code
carries an enrollment token instead, and ATAK keeps THAT as the account's
credential. Both have to be accepted here, or half the ways of joining a server
end with services that turn the operator away for no reason they can see.
"""

import os
import time

import pytest
from flask_security import hash_password


def _wystaw_klucz_urzedu(app):
    """Writes the RSA pair that signs enrollment tokens into the CA folder.

    Tests never run `ots create-ca`, so the pair the token code reads does not
    exist unless the test makes it. Kept here rather than in conftest because
    this is the only suite that touches tokens.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    folder = os.path.join(app.config.get("OTS_CA_FOLDER"), "certs", "opentakserver")
    os.makedirs(folder, exist_ok=True)

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with open(os.path.join(folder, "opentakserver.nopass.key"), "wb") as f:
        f.write(
            key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
    with open(os.path.join(folder, "opentakserver.pub"), "wb") as f:
        f.write(
            key.public_key().public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        )


def _zaloz_konto(app, username, password):
    from opentakserver.extensions import db

    with app.app_context():
        app.security.datastore.create_user(
            username=username, password=hash_password(password)
        )
        db.session.commit()


def _wystaw_token(app, username):
    """Mints an enrollment token for the account, the way the QR route does."""
    from opentakserver.extensions import db
    from opentakserver.models.Token import Token

    with app.app_context():
        token = Token()
        token.username = username
        token.creation = int(time.time())
        token.hash_token()
        encoded = token.generate_token()

        db.session.add(token)
        db.session.commit()
        return encoded


def _zapytaj(client, username, password):
    """Sends exactly what the XMPP auth module sends, field for field.

    The route runs every field through bleach, and bleach raises on None, so a
    request missing one of them fails with a 500 that says nothing about the
    credentials. Sending the full shape keeps this suite testing the check
    rather than the argument handling.
    """
    return client.post(
        "/api/external_auth",
        json={
            "user": username,
            "password": password,
            "action": "read",
            "path": "",
            "protocol": "xmpp",
            "id": "",
            "query": "",
            "ip": "",
        },
    )


@pytest.fixture
def konto(app):
    _wystaw_klucz_urzedu(app)
    _zaloz_konto(app, "operator", "TajneHaslo1")
    return "operator"


def test_haslo_konta_przechodzi(app, client, konto):
    assert _zapytaj(client, konto, "TajneHaslo1").status_code == 200


def test_zle_haslo_odpada(app, client, konto):
    assert _zapytaj(client, konto, "NieToHaslo").status_code == 401


def test_nieznane_konto_odpada(app, client, konto):
    assert _zapytaj(client, "obcy", "TajneHaslo1").status_code == 401


def test_token_rejestracji_przechodzi(app, client, konto):
    """The QR path: the phone holds a token, never the password."""
    token = _wystaw_token(app, konto)
    assert _zapytaj(client, konto, token).status_code == 200


def test_token_cudzego_konta_odpada(app, client, konto):
    """A genuine token is not a skeleton key for other accounts."""
    _zaloz_konto(app, "drugi", "InneHaslo1")
    token = _wystaw_token(app, konto)
    assert _zapytaj(client, "drugi", token).status_code == 401


def test_token_wylaczony_odpada(app, client, konto):
    from opentakserver.extensions import db
    from opentakserver.models.Token import Token

    token = _wystaw_token(app, konto)
    with app.app_context():
        wpis = db.session.query(Token).filter_by(username=konto).first()
        wpis.disabled = True
        db.session.add(wpis)
        db.session.commit()

    assert _zapytaj(client, konto, token).status_code == 401


def test_token_nie_zuzywa_uzycia(app, client, konto):
    """Chat logs in over and over; counting a use per login would burn the token."""
    from opentakserver.extensions import db
    from opentakserver.models.Token import Token

    token = _wystaw_token(app, konto)
    for _ in range(3):
        assert _zapytaj(client, konto, token).status_code == 200

    with app.app_context():
        wpis = db.session.query(Token).filter_by(username=konto).first()
        assert wpis.total_uses == 0
