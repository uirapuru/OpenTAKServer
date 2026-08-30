"""The advertised video port is not the port MediaMTX listens on.

public_video_port lives in config_helpers, which pulls in nothing outside the
standard library, so this file needs neither a database nor a broker.
"""

import pytest

from opentakserver.config_helpers import public_video_port
from opentakserver.defaultconfig import DefaultConfig

BLOK = {"OTS_VIDEO_PUBLIC_RTSP_PORT": 30006, "OTS_VIDEO_PUBLIC_RTMP_PORT": 30007}


@pytest.mark.parametrize("protocol", ["rtsp", "rtsps"])
def test_rtsp_and_rtsps_get_the_rtsp_port(protocol):
    assert public_video_port(protocol, BLOK.get) == 30006


@pytest.mark.parametrize("protocol", ["rtmp", "rtmps"])
def test_rtmp_and_rtmps_get_the_rtmp_port(protocol):
    assert public_video_port(protocol, BLOK.get) == 30007


def test_unknown_protocol_gets_the_rtsp_port():
    """generate_xml forces rtsp for ATAK, so an SRT or WebRTC source still has
    to be advertised on the RTSP port."""
    assert public_video_port("srt", BLOK.get) == 30006


def test_defaults_are_the_former_literals():
    """An install that sets neither variable must behave as before."""
    assert DefaultConfig.OTS_VIDEO_PUBLIC_RTSP_PORT == 8554
    assert DefaultConfig.OTS_VIDEO_PUBLIC_RTMP_PORT == 1935
