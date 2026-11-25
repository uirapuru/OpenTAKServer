"""Zaufanie do nagłówka X-Ssl-Cert.

marti-tls (nginx) kończy TLS z uwierzytelnianiem klienta i przekazuje
certyfikat do OTS w X-Ssl-Cert. Sam certyfikat jest publiczny: kto zna jego
PEM (podpisany urzędem środowiska), a dotrze do 8081 z pominięciem nginx,
podawał się za właściciela certyfikatu. Od tej zmiany nagłówek liczy się tylko
razem z sekretem pośrednika (X-Taklab-Proxy-Token), który zna wyłącznie
marti-tls i kontener ots (plik z OTS_SSL_CERT_PROXY_TOKEN_FILE).

Do tego certyfikat musi być ważny w czasie i wystawiony przez urząd
środowiska; zły podpis albo śmieci w nagłówku dają brak certyfikatu, nie 500.
"""

import datetime
import os
from urllib.parse import quote

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

TOKEN = "s3kret-posrednika-0123456789abcdef"


def _name(cn):
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def _key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _cert(subject_cn, issuer_cn, issuer_key, *, days_from=-1, days_to=30, ca=False):
    key = _key()
    now = datetime.datetime.now(datetime.timezone.utc)
    builder = (
        x509.CertificateBuilder()
        .subject_name(_name(subject_cn))
        .issuer_name(_name(issuer_cn))
        .public_key(key.public_key() if not ca else issuer_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now + datetime.timedelta(days=days_from))
        .not_valid_after(now + datetime.timedelta(days=days_to))
    )
    if ca:
        builder = builder.add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
    else:
        builder = builder.add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), True
        )
    return builder.sign(issuer_key, hashes.SHA256())


def _pem(cert):
    return quote(cert.public_bytes(serialization.Encoding.PEM).decode())


@pytest.fixture
def pki(app, tmp_path):
    ca_key = _key()
    ca = _cert("test-CA", "test-CA", ca_key, days_to=3650, ca=True)
    folder = tmp_path / "ca"
    folder.mkdir()
    (folder / "ca.pem").write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    app.config["OTS_CA_FOLDER"] = str(folder)
    return {"ca": ca, "ca_key": ca_key}


@pytest.fixture
def token_file(tmp_path, monkeypatch):
    path = tmp_path / "proxy-token"
    path.write_text(TOKEN + "\n")
    monkeypatch.setenv("OTS_SSL_CERT_PROXY_TOKEN_FILE", str(path))
    return path


def _verify(app, headers):
    from opentakserver.blueprints.marti_api.marti_api import verify_client_cert

    with app.test_request_context("/", headers=headers):
        return verify_client_cert()


def _cn(cert):
    return cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value


def test_header_with_proxy_token_is_accepted(app, pki, token_file):
    cert = _cert("administrator", "test-CA", pki["ca_key"])
    got = _verify(app, {"X-Ssl-Cert": _pem(cert), "X-Taklab-Proxy-Token": TOKEN})
    assert got is not None and _cn(got) == "administrator"


def test_header_without_proxy_token_is_ignored(app, pki, token_file):
    """Podróbka z poda w przestrzeni albo z NodePortu 8081: sam publiczny PEM."""
    cert = _cert("administrator", "test-CA", pki["ca_key"])
    assert _verify(app, {"X-Ssl-Cert": _pem(cert)}) is None


def test_header_with_wrong_proxy_token_is_ignored(app, pki, token_file):
    cert = _cert("administrator", "test-CA", pki["ca_key"])
    assert _verify(app, {"X-Ssl-Cert": _pem(cert), "X-Taklab-Proxy-Token": "zly"}) is None


def test_unreadable_token_file_fails_closed(app, pki, tmp_path, monkeypatch):
    monkeypatch.setenv("OTS_SSL_CERT_PROXY_TOKEN_FILE", str(tmp_path / "brak"))
    cert = _cert("administrator", "test-CA", pki["ca_key"])
    assert _verify(app, {"X-Ssl-Cert": _pem(cert), "X-Taklab-Proxy-Token": ""}) is None
    assert _verify(app, {"X-Ssl-Cert": _pem(cert), "X-Taklab-Proxy-Token": TOKEN}) is None


def test_empty_token_file_fails_closed(app, pki, tmp_path, monkeypatch):
    path = tmp_path / "pusty"
    path.write_text("\n")
    monkeypatch.setenv("OTS_SSL_CERT_PROXY_TOKEN_FILE", str(path))
    cert = _cert("administrator", "test-CA", pki["ca_key"])
    assert _verify(app, {"X-Ssl-Cert": _pem(cert), "X-Taklab-Proxy-Token": ""}) is None


def test_without_token_configured_keeps_upstream_behaviour(app, pki, monkeypatch):
    """Bez zmiennej (np. takpi z nginx upstreamu) zostaje stare zachowanie."""
    monkeypatch.delenv("OTS_SSL_CERT_PROXY_TOKEN_FILE", raising=False)
    cert = _cert("administrator", "test-CA", pki["ca_key"])
    got = _verify(app, {"X-Ssl-Cert": _pem(cert)})
    assert got is not None and _cn(got) == "administrator"


def test_certificate_from_foreign_ca_is_rejected_without_500(app, pki, token_file):
    other_key = _key()
    cert = _cert("administrator", "test-CA", other_key)
    assert _verify(app, {"X-Ssl-Cert": _pem(cert), "X-Taklab-Proxy-Token": TOKEN}) is None


def test_expired_certificate_is_rejected(app, pki, token_file):
    cert = _cert("administrator", "test-CA", pki["ca_key"], days_from=-30, days_to=-1)
    assert _verify(app, {"X-Ssl-Cert": _pem(cert), "X-Taklab-Proxy-Token": TOKEN}) is None


def test_garbage_header_is_rejected_without_500(app, pki, token_file):
    assert _verify(app, {"X-Ssl-Cert": "nie-certyfikat", "X-Taklab-Proxy-Token": TOKEN}) is None


def test_route_refuses_forged_header(app, pki, token_file):
    """Pełna droga przez trasę: /Marti/api/taklab/config bez sekretu = 401."""
    cert = _cert("administrator", "test-CA", pki["ca_key"])
    client = app.test_client()
    response = client.get("/Marti/api/taklab/config", headers={"X-Ssl-Cert": _pem(cert)})
    assert response.status_code == 401
