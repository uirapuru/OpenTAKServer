"""What the enrollment profile tells a freshly enrolled client.

A client can join in two ways. A QR code carries the stream address and port
inside its host field, so such a client knows where to connect. A client that
enrolled by typing an address, a username and a password - ATAK's "quick
connect" - knows neither, and falls back to the port compiled into the app.

The enrollment profile is the only channel that can tell it. These tests pin
what goes into that channel, because every failure here looks the same on the
phone: a server entry that sits there refusing to connect, with nothing on
screen saying why.
"""

import os
import zipfile
from xml.etree.ElementTree import fromstring

import pytest
from flask_security import hash_password


def _wystaw_klucz_urzedu(app):
    """The enrollment profile reads the truststore out of the CA folder."""

    folder = os.path.join(app.config.get("OTS_CA_FOLDER"))
    os.makedirs(folder, exist_ok=True)
    sciezka = os.path.join(folder, "truststore-root.p12")
    if not os.path.exists(sciezka):
        # A real PKCS#12 is not needed - the route only copies the bytes.
        with open(sciezka, "wb") as f:
            f.write(b"nie-jest-czytany-przez-ten-kod")
    return sciezka


@pytest.fixture
def profil(app, client):
    from opentakserver.extensions import db

    _wystaw_klucz_urzedu(app)
    with app.app_context():
        app.security.datastore.create_user(
            username="operator", password=hash_password("TajneHaslo1")
        )
        db.session.commit()

    odpowiedz = client.get("/Marti/api/tls/profile/enrollment")
    assert odpowiedz.status_code == 200

    import io

    with zipfile.ZipFile(io.BytesIO(odpowiedz.data)) as paczka:
        nazwa = [n for n in paczka.namelist() if n.endswith("preference.pref")][0]
        return fromstring(paczka.read(nazwa))


def _blok(prefs, nazwa):
    for blok in prefs.findall("preference"):
        if blok.get("name") == nazwa:
            return blok
    return None


def _wpis(blok, klucz):
    for wpis in blok.findall("entry"):
        if wpis.get("key") == klucz:
            return wpis
    return None


def test_profil_niesie_blok_strumieni(profil):
    """'cot_streams' to osobny blok. Klucze strumienia wpisane miedzy
    preferencje aplikacji ATAK po prostu pomija."""
    assert _blok(profil, "cot_streams") is not None


def test_strumien_wskazuje_port_na_ktorym_serwer_naprawde_slucha(profil, app):
    """Port z konfiguracji, nie stala 8089 wkompilowana w klienta.

    Wdrozenie, ktore wystawia strumien na innym numerze, dawalo klientowi
    z 'szybkiego polaczenia' wpis serwera odmawiajacy polaczenia."""
    strumienie = _blok(profil, "cot_streams")
    connect = _wpis(strumienie, "connectString0")
    assert connect is not None
    port = app.config.get("OTS_SSL_STREAMING_PORT")
    assert connect.text.endswith(f":{port}:ssl"), connect.text


def test_strumien_jest_wlaczony_i_policzony(profil):
    """ATAK czyta strumienie po liczniku. Wpis bez 'count' jest niewidoczny,
    wpis z 'enabled0' rownym false jest widoczny i martwy."""
    strumienie = _blok(profil, "cot_streams")
    assert _wpis(strumienie, "count").text == "1"
    assert _wpis(strumienie, "enabled0").text == "true"
    assert _wpis(strumienie, "count").get("class") == "class java.lang.Integer"
    assert _wpis(strumienie, "enabled0").get("class") == "class java.lang.Boolean"


def test_preferencje_aplikacji_zostaly_nietkniete(profil):
    """Nowy blok dochodzi OBOK, a nie zamiast - profil dalej niesie to, co
    niosl wczesniej."""
    aplikacja = _blok(profil, "com.atakmap.app_preferences")
    assert aplikacja is not None
    assert _wpis(aplikacja, "deviceProfileEnableOnConnect") is not None


def test_profil_polaczeniowy_nie_przepisuje_strumieni(app, client):
    """Profil polaczeniowy schodzi przy KAZDYM starcie aplikacji. Gdyby niosl
    strumien 0, kasowalby po cichu drugi serwer dopisany recznie przez
    operatora."""
    _wystaw_klucz_urzedu(app)
    odpowiedz = client.get("/api/connection")
    assert odpowiedz.status_code == 200

    import io

    with zipfile.ZipFile(io.BytesIO(odpowiedz.data)) as paczka:
        nazwa = [n for n in paczka.namelist() if n.endswith("preference.pref")][0]
        prefs = fromstring(paczka.read(nazwa))

    assert _blok(prefs, "cot_streams") is None
