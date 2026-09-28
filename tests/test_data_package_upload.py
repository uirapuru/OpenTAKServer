import hashlib
import io
import zipfile

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
