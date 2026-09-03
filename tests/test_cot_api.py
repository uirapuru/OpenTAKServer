import uuid

POPRAWNE = {
    "type": "a-h-G",
    "latitude": 52.1,
    "longitude": 21.0,
    "callsign": "SZTAB",
}


def zadanie(**zmiany):
    tresc = dict(POPRAWNE)
    tresc["uid"] = str(uuid.uuid4())
    tresc.update(zmiany)
    return tresc


def test_requires_authentication(client):
    response = client.post("/api/cot", json=zadanie())
    assert response.status_code in (302, 401)


def test_accepts_a_valid_marker(auth):
    tresc = zadanie()
    response = auth.post("/api/cot", json=tresc)
    assert response.status_code == 201
    assert response.json["uid"] == tresc["uid"]


def test_rejects_a_type_outside_the_whitelist(auth):
    response = auth.post("/api/cot", json=zadanie(type="a-x-Q"))
    assert response.status_code == 400
    assert response.json["success"] is False


def test_rejects_a_uid_that_is_not_uuid4(auth):
    response = auth.post("/api/cot", json=zadanie(uid="nie-uuid"))
    assert response.status_code == 400


def test_rejects_latitude_outside_the_range(auth):
    response = auth.post("/api/cot", json=zadanie(latitude=91.0))
    assert response.status_code == 400


def test_rejects_longitude_outside_the_range(auth):
    response = auth.post("/api/cot", json=zadanie(longitude=181.0))
    assert response.status_code == 400


def test_missing_coordinates_are_rejected(auth):
    tresc = zadanie()
    del tresc["latitude"]
    response = auth.post("/api/cot", json=tresc)
    assert response.status_code == 400


def test_callsign_defaults_to_the_logged_in_user(auth):
    tresc = zadanie()
    del tresc["callsign"]
    response = auth.post("/api/cot", json=tresc)
    assert response.status_code == 201


def test_chat_event_with_a_dict_detail_succeeds(auth):
    tresc = zadanie(type="b-t-f", detail={"message": "zbiorka"})
    response = auth.post("/api/cot", json=tresc)
    assert response.status_code == 201
    assert response.json["uid"] == tresc["uid"]


def test_chat_event_with_a_string_detail_is_rejected(auth):
    response = auth.post("/api/cot", json=zadanie(type="b-t-f", detail="zbiorka"))
    assert response.status_code == 400
    assert response.json["success"] is False
    assert "detail" in response.json["error"].lower()


def test_chat_event_with_a_list_detail_is_rejected(auth):
    response = auth.post("/api/cot", json=zadanie(type="b-t-f", detail=[1, 2, 3]))
    assert response.status_code == 400
    assert response.json["success"] is False
    assert "detail" in response.json["error"].lower()
