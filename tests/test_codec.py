import pytest

from skytuner_hid import ProtocolError
from skytuner_hid.hid_trigger import (
    Command,
    ENTER_UPDATE_TOKEN,
    _HidApiDevice,
    _decode_response,
    crc16_ccitt_false,
    encode_request,
)

from helpers import QueueDevice, make_response


INFO_REQUEST = (
    "50 44 31 30 01 01 00 00 34 12 00 00 "
    + "00 " * 50
    + "bf 01"
)
UPDATE_REQUEST = (
    "50 44 31 30 01 02 00 00 78 56 04 00 55 50 44 31 "
    + "00 " * 46
    + "e1 04"
)
INFO_RESPONSE = (
    "50 44 31 30 01 01 00 00 34 12 13 00 "
    "50 44 31 30 30 56 30 35 32 36 30 31 30 31 30 30 30 30 01 "
    + "00 " * 31
    + "4e 71"
)
UPDATE_ACK = (
    "50 44 31 30 01 02 00 00 78 56 00 00 "
    + "00 " * 50
    + "25 fa"
)


def test_crc_known_vector():
    assert crc16_ccitt_false(b"123456789") == 0x29B1


def test_existing_info_and_update_request_vectors():
    assert encode_request(Command.INFO, 0x1234) == bytes.fromhex(
        INFO_REQUEST
    )
    assert encode_request(
        Command.ENTER_UPDATE, 0x5678, ENTER_UPDATE_TOKEN
    ) == bytes.fromhex(UPDATE_REQUEST)
    assert make_response(
        Command.INFO, 0x1234, (bytes((0x50, 0x44, 0x31, 0x30, 0x30)) + b"V050100000000\x01")
    ) == bytes.fromhex(INFO_RESPONSE)
    assert make_response(
        Command.ENTER_UPDATE, 0x5678
    ) == bytes.fromhex(UPDATE_ACK)


def test_response_decode_checks_crc_and_padding():
    valid = make_response(Command.INFO, 4, b"ok")
    assert _decode_response(
        valid, expected_command=Command.INFO, expected_sequence=4
    ).payload == b"ok"

    corrupt = bytearray(valid)
    corrupt[20] = 1
    with pytest.raises(ProtocolError, match="CRC"):
        _decode_response(
            corrupt, expected_command=Command.INFO, expected_sequence=4
        )

    padded = bytearray(make_response(Command.INFO, 4, b"ok"))
    padded[20] = 1
    padded[62:64] = crc16_ccitt_false(padded[:62]).to_bytes(2, "little")
    with pytest.raises(ProtocolError, match="padding"):
        _decode_response(
            padded, expected_command=Command.INFO, expected_sequence=4
        )


def test_hidapi_report_id_framing():
    raw = QueueDevice([b"\x00" + bytes(64)])
    framed = _HidApiDevice(raw)
    assert framed.write(bytes(64)) == 64
    assert raw.writes == [bytes(65)]
    assert framed.read(64, 100) == bytes(64)


def test_hidapi_prefers_feature_reports_when_available():
    class FeatureDevice:
        def __init__(self):
            self.sent = []
            self.requested = []

        def send_feature_report(self, packet):
            self.sent.append(bytes(packet))
            return len(packet)

        def get_feature_report(self, report_id, size):
            self.requested.append((report_id, size))
            return b"\x00" + bytes(64)

        def close(self):
            pass

    raw = FeatureDevice()
    framed = _HidApiDevice(raw)
    assert framed.write(bytes(64)) == 64
    assert raw.sent == [bytes(65)]
    assert framed.read(64, 100) == bytes(64)
    assert raw.requested == [(0, 65)]
