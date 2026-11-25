"""API federacji wymaga roli administratora.

Upstream (1.8.3) stawiał @roles_required NAD @federation_blueprint.route, więc
Flask rejestrował funkcję bez sprawdzania roli: GET /api/federation/all dawał
200 każdemu z internetu (zmierzone 25 XI 2026 na public). Do tego PATCH
/api/federation/certificate przekazywał common_name prosto do
CertificateAuthority.issue_certificate, które robi shutil.rmtree(certs/<cn>):
common_name ".." kasował cały katalog urzędu.
"""

import pytest


@pytest.fixture(autouse=True)
def isolated_ca(app, tmp_path):
    """Urząd w katalogu tymczasowym: domyślny OTS_CA_FOLDER to ~/ots/ca, a te
    testy wołają issue_certificate z nazwami, które bez poprawki kasowały katalog."""
    app.config["OTS_CA_FOLDER"] = str(tmp_path / "ca")
    (tmp_path / "ca" / "certs").mkdir(parents=True)
    (tmp_path / "ca" / "ca.pem").write_text("placeholder")
    return tmp_path / "ca"

READ_ROUTES = [
    "/api/federation",
    "/api/federation/all",
    "/api/federation/token",
    "/api/federation/certificate",
    "/api/federation/federate",
    "/api/federation/federate/all",
    "/api/federation/groups",
    "/api/federation/federate_groups",
    "/api/federation/group_mapping",
]

WRITE_ROUTES = [
    ("post", "/api/federation"),
    ("delete", "/api/federation"),
    ("patch", "/api/federation"),
    ("post", "/api/federation/token"),
    ("patch", "/api/federation/certificate"),
    ("post", "/api/federation/certificate"),
    ("post", "/api/federation/federate"),
    ("delete", "/api/federation/federate"),
    ("put", "/api/federation/groups"),
    ("delete", "/api/federation/groups"),
    ("post", "/api/federation/group_mapping"),
    ("delete", "/api/federation/group_mapping"),
]


@pytest.mark.parametrize("path", READ_ROUTES)
def test_read_routes_refuse_anonymous(client, path):
    assert client.get(path).status_code in (401, 403)


@pytest.mark.parametrize("method,path", WRITE_ROUTES)
def test_write_routes_refuse_anonymous(client, method, path):
    response = getattr(client, method)(path, json={"common_name": ".."})
    assert response.status_code in (401, 403)


def test_every_federation_route_is_wrapped(app):
    """Każda trasa blueprintu ma sprawdzenie roli (nowa trasa bez niego = czerwony test)."""
    for rule in app.url_map.iter_rules():
        if not rule.rule.startswith("/api/federation"):
            continue
        view = app.view_functions[rule.endpoint]
        assert hasattr(view, "__wrapped__"), f"{rule.rule} bez @roles_required"


def test_admin_can_list_federations(auth):
    response = auth.get("/api/federation/all")
    assert response.status_code == 200


@pytest.mark.parametrize("bad", ["..", ".", "../x", "a/b", "", "x" * 256, "a\\b"])
def test_regenerate_certificate_rejects_path_like_names(auth, bad):
    response = auth.client.patch(
        "/api/federation/certificate", json={"common_name": bad}, headers=auth.headers
    )
    assert response.status_code == 400


@pytest.mark.parametrize("bad", ["..", ".", "../ca", "a/b", ""])
def test_issue_certificate_refuses_path_like_names(app, bad):
    from opentakserver.certificate_authority import CertificateAuthority
    from opentakserver.extensions import logger

    with app.app_context():
        with pytest.raises(ValueError):
            CertificateAuthority(logger, app).issue_certificate(bad)


def test_blueprint_name_unchanged(app):
    from opentakserver.blueprints.ots_api.federation_api import federation_blueprint

    assert federation_blueprint.name == "federation_blueprint"


@pytest.mark.parametrize("good", ["administrator", "taklab-shared-map", "ai_buddy", "Łukasz.K", "federation"])
def test_safe_common_name_accepts_real_account_names(good):
    from opentakserver.certificate_authority import is_safe_common_name

    assert is_safe_common_name(good)
