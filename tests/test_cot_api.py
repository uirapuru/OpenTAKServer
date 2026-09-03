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


def test_atom_type_creates_a_marker_row(auth, app):
    from opentakserver.extensions import db
    from opentakserver.models.CoT import CoT
    from opentakserver.models.Marker import Marker

    tresc = zadanie()
    assert auth.post("/api/cot", json=tresc).status_code == 201

    with app.app_context():
        marker = db.session.execute(
            db.session.query(Marker).filter_by(uid=tresc["uid"])
        ).first()
        assert marker is not None
        cot = db.session.execute(db.session.query(CoT).filter_by(type="a-h-G")).first()
        assert cot is not None


def test_alert_type_creates_no_marker_row(auth, app):
    from opentakserver.extensions import db
    from opentakserver.models.CoT import CoT
    from opentakserver.models.Marker import Marker

    tresc = zadanie(type="b-a-o-tbl")
    assert auth.post("/api/cot", json=tresc).status_code == 201

    with app.app_context():
        marker = db.session.execute(
            db.session.query(Marker).filter_by(uid=tresc["uid"])
        ).first()
        assert marker is None
        cot = db.session.execute(db.session.query(CoT).filter_by(type="b-a-o-tbl")).first()
        assert cot is not None


def test_point_row_carries_the_coordinates(auth, app):
    from opentakserver.extensions import db
    from opentakserver.models.Point import Point

    tresc = zadanie(latitude=50.5, longitude=19.5)
    assert auth.post("/api/cot", json=tresc).status_code == 201

    with app.app_context():
        rows = db.session.execute(db.session.query(Point)).all()
        assert any(
            abs(row[0].latitude - 50.5) < 1e-9 and abs(row[0].longitude - 19.5) < 1e-9
            for row in rows
        )


def test_resending_the_same_atom_uid_moves_the_marker(auth, app):
    from opentakserver.extensions import db
    from opentakserver.models.Marker import Marker

    tresc = zadanie(latitude=10.0, longitude=20.0)
    first = auth.post("/api/cot", json=tresc)
    assert first.status_code == 201

    with app.app_context():
        marker = db.session.execute(
            db.session.query(Marker).filter_by(uid=tresc["uid"])
        ).first()[0]
        first_point_id = marker.point_id
        first_cot_id = marker.cot_id

    tresc_ponowny = dict(tresc)
    tresc_ponowny["latitude"] = 30.0
    tresc_ponowny["longitude"] = 40.0
    second = auth.post("/api/cot", json=tresc_ponowny)
    assert second.status_code == 201

    with app.app_context():
        markers = db.session.execute(
            db.session.query(Marker).filter_by(uid=tresc["uid"])
        ).all()
        assert len(markers) == 1

        marker = markers[0][0]
        assert marker.point_id != first_point_id
        assert marker.cot_id != first_cot_id
        assert abs(marker.point.latitude - 30.0) < 1e-9
        assert abs(marker.point.longitude - 40.0) < 1e-9


def test_event_is_published_to_all_three_exchanges(auth, monkeypatch):
    import opentakserver.blueprints.ots_api.cot_api as modul

    wyslane = []

    class AtrapaKanalu:
        def basic_publish(self, exchange, routing_key, body, properties=None):
            wyslane.append(exchange)

        def close(self):
            pass

    class AtrapaPolaczenia:
        def channel(self):
            return AtrapaKanalu()

        def close(self):
            pass

    monkeypatch.setattr(modul.pika, "BlockingConnection", lambda *a, **k: AtrapaPolaczenia())

    import opentakserver.blueprints.ots_api.api as api_modul

    monkeypatch.setattr(api_modul.pika, "BlockingConnection", lambda *a, **k: AtrapaPolaczenia())

    assert auth.post("/api/cot", json=zadanie()).status_code == 201
    assert "cot_parser" in wyslane
    assert "firehose" in wyslane
    assert "groups" in wyslane
