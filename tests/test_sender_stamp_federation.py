"""Federacja nie przenosi podpisu nadawcy czatu w żadną stronę.

Przychodzące zdarzenia wbudowanej federacji OTS idą prosto do grup, z pominięciem
cot_parser, więc podpis podrzucony przez partnera musi zniknąć w konwerterze.
Wychodzące nie zdradzają partnerowi nazw lokalnych kont.

Środowisko testowe ma protobuf starszy niż wygenerowany fig_pb2, więc moduł
protobuf zastępuje tu prosta atrapa z tymi polami, których konwerter używa.
"""

import importlib
import sys
import types

import pytest
from bs4 import BeautifulSoup

CHAT = (
    '<event version="2.0" uid="GeoChat.x.y.1" type="b-t-f" how="h-g-i-g-o" '
    'time="2026-11-27T20:00:00Z" start="2026-11-27T20:00:00Z" stale="2026-11-27T20:05:00Z">'
    '<point lat="52.0" lon="21.0" hae="0" ce="9999999" le="9999999"/>'
    '<detail><__chat senderCallsign="A"><__taklab_sender username="admin"/></__chat>'
    '<__taklab_sender username="alfa"/><remarks>kamera wl</remarks></detail></event>'
)


class FakeCotEvent:
    def __init__(self):
        self.type = self.uid = self.coordSource = self.other = ""
        self.sendTime = self.startTime = self.staleTime = 0
        self.lat = self.lon = self.hae = self.ce = self.le = 0.0


class FakeFederatedEvent:
    def __init__(self):
        self.event = FakeCotEvent()

    def HasField(self, name):
        return name == "event"


@pytest.fixture
def converter(monkeypatch):
    fig = types.ModuleType("opentakserver.proto.fig_pb2")
    fig.FederatedEvent = FakeFederatedEvent
    monkeypatch.setitem(sys.modules, "opentakserver.proto.fig_pb2", fig)
    monkeypatch.delitem(sys.modules, "opentakserver.fed_client.data_converter", raising=False)
    module = importlib.import_module("opentakserver.fed_client.data_converter")
    yield module
    sys.modules.pop("opentakserver.fed_client.data_converter", None)


def test_outbound_federation_drops_sender_stamp(converter):
    federated = converter.cot2federated_event(CHAT, "node-1")
    assert "__taklab_sender" not in federated.event.other
    assert "kamera wl" in federated.event.other


def test_inbound_federation_drops_sender_stamp(converter):
    federated = FakeFederatedEvent()
    federated.event.type = "b-t-f"
    federated.event.uid = "GeoChat.x.y.1"
    federated.event.coordSource = "h-g-i-g-o"
    federated.event.startTime = 1_800_000_000_000
    federated.event.staleTime = 1_800_000_300_000
    federated.event.other = (
        '<detail><__chat senderCallsign="A"><__taklab_sender username="admin"/></__chat>'
        '<__taklab_sender username="admin"/><remarks>kamera wl</remarks></detail>'
    )

    cot = converter.federated_event2cot(federated)

    soup = BeautifulSoup(cot, "xml")
    assert soup.find_all("__taklab_sender") == []
    assert soup.find("remarks").text == "kamera wl"
