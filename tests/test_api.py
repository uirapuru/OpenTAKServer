def test_marti_api_clientendpoints(client):
    response = client.get("/Marti/api/clientEndPoints")
    assert response.status_code == 200


def test_marti_api_tls_config(auth):
    response = auth.get("/Marti/api/tls/config", headers=auth.basic_auth)
    assert response.status_code == 200


def test_points(auth):
    response = auth.get("/api/point")
    assert response.status_code == 200


def test_me(auth):
    response = auth.get("/api/me")
    assert response.status_code == 200
