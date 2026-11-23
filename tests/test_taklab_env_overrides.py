"""Zmienne środowiska wygrywają z config.yml dla kluczy TakLab i logowania do
Mumble. OTS przy pierwszym starcie zapisuje config.yml z wartości domyślnych,
a plik potem przesłania zmienne - włączone w charcie uwierzytelnianie Ice
zostawało wyłączone (plug0, 23 XI 2026)."""

from opentakserver.config_helpers import apply_env_overrides


def test_env_wins_over_file_values(monkeypatch):
    monkeypatch.setenv("OTS_ENABLE_MUMBLE_AUTHENTICATION", "True")
    monkeypatch.setenv("OTS_TAKLAB_HOST", "e.taklab.eu")
    monkeypatch.setenv("OTS_TAKLAB_MUMBLE_PORT", "30005")
    monkeypatch.setenv("OTS_TAKLAB_RTSP_PORT", "30006")
    monkeypatch.setenv("OTS_TAKLAB_MUMBLE_DB", "/app/ots/mumble/mumble-server.sqlite")
    monkeypatch.setenv("OTS_MUMBLE_SHARED_PASSWORD", "wspolne")
    config = {"OTS_ENABLE_MUMBLE_AUTHENTICATION": False, "OTS_TAKLAB_HOST": "", "OTS_TAKLAB_MUMBLE_PORT": 0,
              "OTS_TAKLAB_RTSP_PORT": 0, "OTS_TAKLAB_MUMBLE_DB": "", "OTS_MUMBLE_SHARED_PASSWORD": "",
              "OTHER": "x"}
    apply_env_overrides(config)
    assert config == {"OTS_ENABLE_MUMBLE_AUTHENTICATION": True, "OTS_TAKLAB_HOST": "e.taklab.eu",
                      "OTS_TAKLAB_MUMBLE_PORT": 30005, "OTS_TAKLAB_RTSP_PORT": 30006,
                      "OTS_TAKLAB_MUMBLE_DB": "/app/ots/mumble/mumble-server.sqlite",
                      "OTS_MUMBLE_SHARED_PASSWORD": "wspolne", "OTHER": "x"}


def test_missing_env_keeps_file_values(monkeypatch):
    for key in ("OTS_ENABLE_MUMBLE_AUTHENTICATION", "OTS_TAKLAB_HOST", "OTS_TAKLAB_MUMBLE_PORT"):
        monkeypatch.delenv(key, raising=False)
    config = {"OTS_ENABLE_MUMBLE_AUTHENTICATION": True, "OTS_TAKLAB_HOST": "z.pliku"}
    apply_env_overrides(config)
    assert config == {"OTS_ENABLE_MUMBLE_AUTHENTICATION": True, "OTS_TAKLAB_HOST": "z.pliku"}


def test_false_and_bad_port(monkeypatch):
    monkeypatch.setenv("OTS_ENABLE_MUMBLE_AUTHENTICATION", "False")
    monkeypatch.setenv("OTS_TAKLAB_MUMBLE_PORT", "abc")
    config = {"OTS_ENABLE_MUMBLE_AUTHENTICATION": True, "OTS_TAKLAB_MUMBLE_PORT": 5}
    apply_env_overrides(config)
    assert config["OTS_ENABLE_MUMBLE_AUTHENTICATION"] is False
    assert config["OTS_TAKLAB_MUMBLE_PORT"] == 0


def test_create_app_applies_overrides(monkeypatch, data_folder):
    import os

    with open(os.path.join(data_folder, "config.yml"), "w") as f:
        f.write("OTS_ENABLE_MUMBLE_AUTHENTICATION: false\nOTS_TAKLAB_HOST: ''\n")
    monkeypatch.setenv("OTS_ENABLE_MUMBLE_AUTHENTICATION", "True")
    monkeypatch.setenv("OTS_TAKLAB_HOST", "e.taklab.eu")
    from opentakserver.defaultconfig import DefaultConfig

    from tests.conftest import TEST_DATABASE_URI

    DefaultConfig.OTS_DATA_FOLDER = data_folder
    DefaultConfig.SQLALCHEMY_DATABASE_URI = TEST_DATABASE_URI
    from opentakserver.app import create_app

    app = create_app(cli=False)
    assert app.config["OTS_ENABLE_MUMBLE_AUTHENTICATION"] is True
    assert app.config["OTS_TAKLAB_HOST"] == "e.taklab.eu"
