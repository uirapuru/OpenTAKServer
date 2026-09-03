"""Shared fixtures for the test suite.

The suite needs the test database from compose.test.yaml. Start it with:

    docker compose -f compose.test.yaml up -d --wait

then run the suite with:

    .venv-test/bin/python -m pytest tests/ -q --no-cov

Override the connection string with OTS_TEST_DATABASE_URI if you run your own.
"""

import base64
import os
import tempfile
from unittest.mock import MagicMock

import pika
import pytest

# This stub has to stand BEFORE the application is imported: create_app(cli=False)
# opens a connection to RabbitMQ during startup.
pika.BlockingConnection = MagicMock()

from flask_security import hash_password  # noqa: E402

from opentakserver.defaultconfig import DefaultConfig  # noqa: E402

TEST_DATABASE_URI = os.environ.get(
    "OTS_TEST_DATABASE_URI",
    "postgresql+psycopg://ots:test@127.0.0.1:55432/ots_test",
)


@pytest.fixture(scope="session")
def data_folder():
    with tempfile.TemporaryDirectory() as folder:
        yield folder


@pytest.fixture
def app(data_folder):
    DefaultConfig.OTS_DATA_FOLDER = data_folder
    DefaultConfig.SQLALCHEMY_DATABASE_URI = TEST_DATABASE_URI

    from opentakserver.app import create_app
    from opentakserver.extensions import db

    application = create_app(cli=False)
    application.config["TESTING"] = True
    application.config["WTF_CSRF_ENABLED"] = False
    application.config["SQLALCHEMY_DATABASE_URI"] = TEST_DATABASE_URI

    with application.app_context():
        db.drop_all()
        db.create_all()

    yield application

    with application.app_context():
        db.drop_all()


@pytest.fixture
def client(app):
    with app.test_client() as test_client:
        yield test_client


class AuthActions:
    def __init__(self, app, client, username="TestUser", password="TestPass"):
        self.app = app
        self.client = client
        self.username = username
        self.password = password
        self.headers = {}
        self.create()
        self.login()

    def create(self):
        from opentakserver.extensions import db

        with self.app.app_context():
            # create_app(cli=False) never seeds roles -- that only happens in
            # app.py's main(), which tests never call. Mirror it here so the
            # "administrator" role exists before it is assigned below.
            self.app.security.datastore.find_or_create_role(
                name="administrator", permissions={"administrator"}
            )
            self.app.security.datastore.create_user(
                username=self.username,
                password=hash_password(self.password),
                roles=["administrator"],
            )
            db.session.commit()

    def login(self):
        response = self.client.post(
            "/api/login?include_auth_token",
            json={"username": self.username, "password": self.password},
        )
        token = response.json["response"]["user"]["authentication_token"]
        self.headers = {"Authentication-Token": token}
        return response

    @property
    def basic_auth(self):
        raw = f"{self.username}:{self.password}".encode("utf-8")
        return {"Authorization": "Basic " + base64.b64encode(raw).decode("utf-8")}

    # headers is compared against None rather than tested for truth, so an
    # explicit headers={} really sends no headers instead of quietly falling
    # back to the authenticated ones. A test proving an endpoint rejects
    # unauthenticated requests would otherwise pass while sending a valid token.
    def _headers(self, headers):
        return headers if headers is not None else self.headers

    def get(self, path, headers=None, **kwargs):
        return self.client.get(path, headers=self._headers(headers), **kwargs)

    def post(self, path, headers=None, **kwargs):
        return self.client.post(path, headers=self._headers(headers), **kwargs)

    def delete(self, path, headers=None, **kwargs):
        return self.client.delete(path, headers=self._headers(headers), **kwargs)


@pytest.fixture
def auth(app, client):
    return AuthActions(app, client)
