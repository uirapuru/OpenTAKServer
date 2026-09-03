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
    assert response.json["username"] == "TestUser"


def test_explicit_empty_headers_really_drop_the_token(auth, monkeypatch):
    """headers={} must send no headers, not fall back to the authenticated ones.

    The status code cannot prove this: logging in also left a session cookie on
    the test client, so the request succeeds either way. What matters is which
    headers reach the client, so that is what is captured here.
    """
    sent = {}

    def capture(path, headers=None, **kwargs):
        sent["headers"] = headers
        return "response"

    monkeypatch.setattr(auth.client, "get", capture)

    auth.get("/api/me", headers={})
    assert sent["headers"] == {}

    auth.get("/api/me")
    assert sent["headers"] == auth.headers
