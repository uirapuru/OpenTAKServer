"""EudHandler.handle(): CoT whose multi-byte UTF-8 character is split across recv() chunks.

TLS delivers a stream in records of any size, so a Polish character (two bytes
in UTF-8) can end one recv() and start the next. Decoding each chunk on its own
raised UnicodeDecodeError and dropped the connection.

The test needs no database: the handler is built without __init__/setup and
the fake socket stands in for the TCP/TLS connection.
"""

import logging

from opentakserver.eud_handler.EudHandler import EudHandler
from opentakserver.eud_handler.EudHandlerSSL import EudHandlerSSL

COT = (
    '<event version="2.0" uid="route-1" type="b-m-r" how="h-e">'
    '<point lat="52.0" lon="21.0" hae="0" ce="9999999" le="9999999"/>'
    '<detail><contact callsign="Trasa do Łodzi"/>'
    "<remarks>Zażółć gęślą jaźń</remarks></detail></event>"
)


class FakeSocket:
    def __init__(self, chunks):
        self.chunks = list(chunks)

    def recv(self, _size):
        return self.chunks.pop(0) if self.chunks else b""


def run_handler(handler_class, chunks):
    handler = handler_class.__new__(handler_class)
    handler.request = FakeSocket(chunks)
    handler.logger = logging.getLogger("test")
    received = []
    handler.handle_cot = received.append
    handler.handle_auth = lambda _auth: None
    handler.close_connection = lambda: None
    handler.handle()
    return received


def split_inside_character(text, char):
    data = text.encode("utf-8")
    # Cut between the two bytes of the character
    cut = data.index(char.encode("utf-8")) + 1
    return [data[:cut], data[cut:]]


def test_character_split_between_chunks_is_decoded():
    chunks = split_inside_character(COT, "Ł")
    # Sanity check: the first chunk alone is not valid UTF-8
    try:
        chunks[0].decode("utf-8")
        raise AssertionError("first chunk should end inside a character")
    except UnicodeDecodeError:
        pass

    assert run_handler(EudHandler, chunks) == [COT]


def test_ssl_handler_uses_the_same_stream_decoding():
    chunks = split_inside_character(COT, "ź")

    assert run_handler(EudHandlerSSL, chunks) == [COT]


def test_one_byte_per_chunk():
    chunks = [bytes([b]) for b in COT.encode("utf-8")]

    assert run_handler(EudHandler, chunks) == [COT]
