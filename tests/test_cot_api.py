import uuid

VALID_BODY = {
    "type": "a-h-G",
    "latitude": 52.1,
    "longitude": 21.0,
    "callsign": "SZTAB",
}


def request_body(**overrides):
    body = dict(VALID_BODY)
    body["uid"] = str(uuid.uuid4())
    body.update(overrides)
    return body


def test_requires_authentication(client):
    response = client.post("/api/cot", json=request_body())
    assert response.status_code in (302, 401)


def test_accepts_a_valid_marker(auth):
    body = request_body()
    response = auth.post("/api/cot", json=body)
    assert response.status_code == 201
    assert response.json["uid"] == body["uid"]


def test_rejects_a_type_outside_the_whitelist(auth):
    response = auth.post("/api/cot", json=request_body(type="a-x-Q"))
    assert response.status_code == 400
    assert response.json["success"] is False


def test_rejects_a_uid_that_is_not_uuid4(auth):
    response = auth.post("/api/cot", json=request_body(uid="nie-uuid"))
    assert response.status_code == 400


def test_rejects_latitude_outside_the_range(auth):
    response = auth.post("/api/cot", json=request_body(latitude=91.0))
    assert response.status_code == 400


def test_rejects_longitude_outside_the_range(auth):
    response = auth.post("/api/cot", json=request_body(longitude=181.0))
    assert response.status_code == 400


def test_missing_coordinates_are_rejected(auth):
    body = request_body()
    del body["latitude"]
    response = auth.post("/api/cot", json=body)
    assert response.status_code == 400


def test_non_numeric_stale_seconds_is_rejected(auth):
    response = auth.post("/api/cot", json=request_body(stale_seconds="abc"))
    assert response.status_code == 400
    assert response.json["success"] is False


def test_null_stale_seconds_is_rejected(auth):
    response = auth.post("/api/cot", json=request_body(stale_seconds=None))
    assert response.status_code == 400
    assert response.json["success"] is False


def test_negative_stale_seconds_is_rejected(auth):
    response = auth.post("/api/cot", json=request_body(stale_seconds=-1))
    assert response.status_code == 400


def test_zero_stale_seconds_is_rejected(auth):
    response = auth.post("/api/cot", json=request_body(stale_seconds=0))
    assert response.status_code == 400


def test_absurdly_large_stale_seconds_is_rejected(auth):
    """1e30 overflows timedelta, which used to escape as an uncaught OverflowError."""
    response = auth.post("/api/cot", json=request_body(stale_seconds=10**30))
    assert response.status_code == 400
    assert response.json["success"] is False


def test_stale_seconds_at_the_ceiling_is_accepted(auth):
    from opentakserver.blueprints.ots_api.cot_api import MAX_STALE_SECONDS

    response = auth.post("/api/cot", json=request_body(stale_seconds=MAX_STALE_SECONDS))
    assert response.status_code == 201


def test_stale_seconds_above_the_ceiling_is_rejected(auth):
    from opentakserver.blueprints.ots_api.cot_api import MAX_STALE_SECONDS

    response = auth.post("/api/cot", json=request_body(stale_seconds=MAX_STALE_SECONDS + 1))
    assert response.status_code == 400


def test_non_numeric_error_value_is_rejected(auth):
    response = auth.post("/api/cot", json=request_body(ce="x"))
    assert response.status_code == 400
    assert response.json["success"] is False


def test_null_error_value_is_rejected(auth):
    response = auth.post("/api/cot", json=request_body(hae=None))
    assert response.status_code == 400


def test_callsign_defaults_to_the_logged_in_user(auth):
    body = request_body()
    del body["callsign"]
    response = auth.post("/api/cot", json=body)
    assert response.status_code == 201


def test_chat_event_with_a_dict_detail_succeeds(auth):
    body = request_body(type="b-t-f", detail={"message": "zbiorka"})
    response = auth.post("/api/cot", json=body)
    assert response.status_code == 201
    assert response.json["uid"] == body["uid"]


def test_chat_event_with_an_empty_message_is_rejected(auth):
    """Without a message the event carries no <remarks> and cot_parser drops it."""
    response = auth.post("/api/cot", json=request_body(type="b-t-f", detail={"message": "  "}))
    assert response.status_code == 400
    assert response.json["success"] is False


def test_chat_event_without_a_message_is_rejected(auth):
    response = auth.post("/api/cot", json=request_body(type="b-t-f", detail={}))
    assert response.status_code == 400
    assert response.json["success"] is False


def test_chat_event_with_a_string_detail_is_rejected(auth):
    response = auth.post("/api/cot", json=request_body(type="b-t-f", detail="zbiorka"))
    assert response.status_code == 400
    assert response.json["success"] is False
    assert "detail" in response.json["error"].lower()


def test_chat_event_with_a_list_detail_is_rejected(auth):
    response = auth.post("/api/cot", json=request_body(type="b-t-f", detail=[1, 2, 3]))
    assert response.status_code == 400
    assert response.json["success"] is False
    assert "detail" in response.json["error"].lower()


def test_atom_type_creates_a_marker_row(auth, app):
    from opentakserver.extensions import db
    from opentakserver.models.CoT import CoT
    from opentakserver.models.Marker import Marker

    body = request_body()
    assert auth.post("/api/cot", json=body).status_code == 201

    with app.app_context():
        marker = db.session.execute(
            db.session.query(Marker).filter_by(uid=body["uid"])
        ).first()
        assert marker is not None
        cot = db.session.execute(db.session.query(CoT).filter_by(type="a-h-G")).first()
        assert cot is not None


def test_alert_type_creates_no_marker_row(auth, app):
    from opentakserver.extensions import db
    from opentakserver.models.CoT import CoT
    from opentakserver.models.Marker import Marker

    body = request_body(type="b-a-o-tbl")
    assert auth.post("/api/cot", json=body).status_code == 201

    with app.app_context():
        marker = db.session.execute(
            db.session.query(Marker).filter_by(uid=body["uid"])
        ).first()
        assert marker is None
        cot = db.session.execute(db.session.query(CoT).filter_by(type="b-a-o-tbl")).first()
        assert cot is not None


def test_point_row_carries_the_coordinates(auth, app):
    from opentakserver.extensions import db
    from opentakserver.models.Point import Point

    body = request_body(latitude=50.5, longitude=19.5)
    assert auth.post("/api/cot", json=body).status_code == 201

    with app.app_context():
        rows = db.session.execute(db.session.query(Point)).all()
        assert any(
            abs(row[0].latitude - 50.5) < 1e-9 and abs(row[0].longitude - 19.5) < 1e-9
            for row in rows
        )


def test_resending_the_same_atom_uid_moves_the_marker(auth, app):
    from opentakserver.extensions import db
    from opentakserver.models.Marker import Marker

    body = request_body(latitude=10.0, longitude=20.0)
    first = auth.post("/api/cot", json=body)
    assert first.status_code == 201

    with app.app_context():
        marker = db.session.execute(
            db.session.query(Marker).filter_by(uid=body["uid"])
        ).first()[0]
        first_point_id = marker.point_id
        first_cot_id = marker.cot_id

    second_body = dict(body)
    second_body["latitude"] = 30.0
    second_body["longitude"] = 40.0
    second = auth.post("/api/cot", json=second_body)
    assert second.status_code == 201

    with app.app_context():
        markers = db.session.execute(
            db.session.query(Marker).filter_by(uid=body["uid"])
        ).all()
        assert len(markers) == 1

        marker = markers[0][0]
        assert marker.point_id != first_point_id
        assert marker.cot_id != first_cot_id
        assert abs(marker.point.latitude - 30.0) < 1e-9
        assert abs(marker.point.longitude - 40.0) < 1e-9


def test_event_is_published_to_all_three_exchanges(auth, monkeypatch):
    import opentakserver.blueprints.ots_api.cot_api as cot_api_module

    published_to = []

    class FakeChannel:
        def basic_publish(self, exchange, routing_key, body, properties=None):
            published_to.append(exchange)

        def close(self):
            pass

    class FakeConnection:
        def channel(self):
            return FakeChannel()

        def close(self):
            pass

    monkeypatch.setattr(cot_api_module.pika, "BlockingConnection", lambda *a, **k: FakeConnection())

    import opentakserver.blueprints.ots_api.api as api_module

    monkeypatch.setattr(api_module.pika, "BlockingConnection", lambda *a, **k: FakeConnection())

    assert auth.post("/api/cot", json=request_body()).status_code == 201
    assert "cot_parser" in published_to
    assert "firehose" in published_to
    assert "groups" in published_to
