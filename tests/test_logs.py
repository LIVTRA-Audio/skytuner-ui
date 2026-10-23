import pytest

from skytuner_hid import HidTrigger, ProtocolError
from skytuner_hid.hid_trigger import (
    Command,
    LOG_TAIL_CURSOR,
    _cursor_forward_distance,
    _cursor_successor,
)

from helpers import QueueDevice, make_response


def _payload(start, next_cursor, tail, data=b""):
    return (
        start.to_bytes(4, "little")
        + next_cursor.to_bytes(4, "little")
        + tail.to_bytes(4, "little")
        + data
    )


def test_read_log_empty_oldest_and_from_now():
    device = QueueDevice(
        [
            make_response(Command.READ_LOG, 1, _payload(7, 7, 7)),
            make_response(Command.READ_LOG, 2, _payload(9, 9, 9)),
        ]
    )
    trigger = HidTrigger(device, initial_sequence=1)
    assert trigger.read_log() == (  # dataclass compares fieldwise
        __import__("skytuner_hid").LogChunk(7, 7, 7, "", False)
    )
    tail = trigger.read_log(LOG_TAIL_CURSOR)
    assert (tail.start, tail.next, tail.tail, tail.gap) == (9, 9, 9, False)
    assert device.writes[0][10:12] == b"\x00\x00"
    assert device.writes[1][12:16] == b"\xff\xff\xff\xff"


def test_read_log_record_and_gap():
    device = QueueDevice(
        [
            make_response(
                Command.READ_LOG, 4, _payload(3, 4, 5, b"usb configured")
            ),
            make_response(
                Command.READ_LOG, 5, _payload(8, 9, 10, b"after gap")
            ),
        ]
    )
    trigger = HidTrigger(device, initial_sequence=4)
    record = trigger.read_log(3)
    assert record.data == "usb configured"
    assert not record.gap
    gap = trigger.read_log(0)
    assert gap.gap
    assert gap.start == 8


@pytest.mark.parametrize(
    "payload,match",
    [
        (b"\x00" * 11, "12-byte"),
        (_payload(1, 3, 4, b"x"), "start \\+ 1"),
        (_payload(1, 2, 40, b"x"), "retained window"),
        (_payload(4, 5, 4), "unchanged"),
        (_payload(1, 2, 2, b"\xff"), "ASCII"),
    ],
)
def test_read_log_rejects_malformed_metadata(payload, match):
    device = QueueDevice(
        [make_response(Command.READ_LOG, 1, payload)]
    )
    with pytest.raises(ProtocolError, match=match):
        HidTrigger(device, initial_sequence=1).read_log(1)


def test_transient_empty_slot_can_be_retried_at_same_cursor():
    device = QueueDevice(
        [
            make_response(Command.READ_LOG, 1, _payload(3, 3, 5)),
            make_response(Command.READ_LOG, 2, _payload(3, 4, 5, b"ready")),
        ]
    )
    trigger = HidTrigger(device, initial_sequence=1)
    first = trigger.read_log(3)
    assert first.next == 3 and not first.data
    assert trigger.read_log(first.next).data == "ready"


def test_read_log_accepts_reserved_sentinel_skipping_wrap():
    device = QueueDevice(
        [
            make_response(
                Command.READ_LOG,
                1,
                _payload(0xFFFFFFFE, 0, 0, b"wrapped"),
            )
        ]
    )
    chunk = HidTrigger(
        device, initial_sequence=1
    ).read_log(0xFFFFFFFE)
    assert (chunk.start, chunk.next, chunk.tail, chunk.data) == (
        0xFFFFFFFE,
        0,
        0,
        "wrapped",
    )


@pytest.mark.parametrize(
    "payload",
    [
        _payload(LOG_TAIL_CURSOR, 0, 0),
        _payload(0, 0, LOG_TAIL_CURSOR),
        _payload(0xFFFFFFFE, LOG_TAIL_CURSOR, 0, b"x"),
    ],
)
def test_read_log_rejects_sentinel_in_response_metadata(payload):
    device = QueueDevice(
        [make_response(Command.READ_LOG, 1, payload)]
    )
    with pytest.raises(ProtocolError, match="reserved"):
        HidTrigger(device, initial_sequence=1).read_log(0)


def test_cursor_model_skips_sentinel_and_uses_smaller_domain():
    assert _cursor_successor(0xFFFFFFFD) == 0xFFFFFFFE
    assert _cursor_successor(0xFFFFFFFE) == 0
    assert _cursor_forward_distance(0xFFFFFFFE, 0) == 1
    assert _cursor_forward_distance(0, 0xFFFFFFFE) == 0xFFFFFFFE
