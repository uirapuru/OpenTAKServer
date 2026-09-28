import hashlib
import importlib
import io
import zipfile
from unittest.mock import MagicMock

from opentakserver.extensions import db
from opentakserver.models.DataPackage import DataPackage


def _package(marker: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("MANIFEST/manifest.xml", "<MissionPackageManifest version='2'/>")
        archive.writestr(f"{marker}/{marker}.cot", f"<event uid='{marker}'/>")
    return buffer.getvalue()


def _upload(client, data: bytes):
    sha = hashlib.sha256(data).hexdigest()
    return sha, client.post(
        f"/Marti/sync/missionupload?hash={sha}&filename=chat-transfer.zip&creatorUid=ANDROID-1",
        data={"assetfile": (io.BytesIO(data), "chat-transfer.zip", "application/x-zip-compressed")},
        content_type="multipart/form-data",
    )


def _stored(app, sha: str) -> bool:
    with app.app_context():
        return db.session.execute(db.select(DataPackage).filter_by(hash=sha)).first() is not None


def test_two_chat_attachments_with_the_same_name_are_both_stored(app, client):
    first_sha, first = _upload(client, _package("a"))
    second_sha, second = _upload(client, _package("b"))
    assert first.status_code == 200
    assert second.status_code == 200
    assert _stored(app, first_sha)
    assert _stored(app, second_sha)


def test_resending_identical_package_is_success_and_stays_downloadable(app, client):
    data = _package("same")
    sha, first = _upload(client, data)
    _, again = _upload(client, data)
    assert first.status_code == 200
    assert again.status_code == 200
    assert client.get(f"/Marti/sync/content?hash={sha}").status_code == 200


def _fake_cert(monkeypatch):
    mission_marti_api = importlib.import_module("opentakserver.blueprints.marti_api.mission_marti_api")

    cert = MagicMock()
    cert.get_subject.return_value.commonName = "itak-user"
    monkeypatch.setattr(mission_marti_api, "verify_client_cert", lambda: cert)


def _itak_upload(client, data: bytes):
    return client.post(
        "/Marti/sync/upload?name=chat-transfer",
        data=data,
        content_type="application/x-zip-compressed",
        headers={"User-Agent": "iTAK/2.0"},
    )


def test_itak_upload_reports_failed_save_as_500(app, client, monkeypatch):
    data_package_marti_api = importlib.import_module(
        "opentakserver.blueprints.marti_api.data_package_marti_api"
    )

    _fake_cert(monkeypatch)
    monkeypatch.setattr(data_package_marti_api, "save_data_package_to_db", lambda *a, **k: False)
    response = _itak_upload(client, _package("itak-fail"))
    assert response.status_code == 500
    assert response.get_json()["success"] is False


def test_itak_upload_stores_package_and_returns_hash(app, client, monkeypatch):
    _fake_cert(monkeypatch)
    data = _package("itak-ok")
    response = _itak_upload(client, data)
    sha = hashlib.sha256(data).hexdigest()
    assert response.status_code == 200
    assert response.get_json()["Hash"] == sha
    assert _stored(app, sha)
