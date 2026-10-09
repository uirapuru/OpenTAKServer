"""GET /Marti/vcm: adres strumienia zgloszonego CoT-em (np. przez federacje)
zostaje taki, jaki podal nadawca; adres strumienia z MediaMTX dostaje host
z zadania, bo MediaMTX zapisuje 127.0.0.1."""

from datetime import datetime, timezone

from bs4 import BeautifulSoup


def _add_stream(app, path, address, with_cot):
    from opentakserver.extensions import db
    from opentakserver.models.CoT import CoT
    from opentakserver.models.VideoStream import VideoStream

    with app.app_context():
        stream = VideoStream()
        stream.path = path
        stream.alias = path
        stream.port = 30036
        stream.uid = f"{path}-uid"
        if with_cot:
            now = datetime.now(timezone.utc)
            cot = CoT()
            cot.uid = f"cot-{path}"
            cot.type = "b-i-v"
            cot.xml = "<event/>"
            cot.timestamp = cot.start = cot.stale = now
            db.session.add(cot)
            db.session.commit()
            stream.cot_id = cot.id
        stream.generate_xml(address)
        db.session.add(stream)
        db.session.commit()


def _addresses(client):
    response = client.get("/Marti/vcm", base_url="https://cama.taklab.eu:8443")
    assert response.status_code == 200
    feeds = BeautifulSoup(response.data, "xml").find_all("feed")
    return {feed.find("path").text: feed.find("address").text for feed in feeds}


def test_stream_reported_by_cot_keeps_sender_address(app, client):
    _add_stream(app, "kamera1", "camb.taklab.eu", with_cot=True)

    assert _addresses(client)["kamera1"] == "camb.taklab.eu"


def test_stream_from_mediamtx_gets_request_host(app, client):
    _add_stream(app, "lokalny", "127.0.0.1", with_cot=False)

    assert _addresses(client)["lokalny"] == "cama.taklab.eu"


def test_stream_from_cot_with_loopback_address_gets_request_host(app, client):
    _add_stream(app, "petla", "127.0.0.1", with_cot=True)

    assert _addresses(client)["petla"] == "cama.taklab.eu"
