import threading
import time

import pytest

from skytuner_hid import (
    Button,
    DeviceNotFoundError,
    HidTrigger,
    ProtocolError,
    UpdateTimeoutError,
)
from skytuner_hid.hid_trigger import (
    Command,
    Status,
    FRAMEBUFFER_ENCODING_INDEX8_RGB565,
    FRAMEBUFFER_OP_BEGIN,
    FRAMEBUFFER_OP_END,
    FRAMEBUFFER_OP_READ_PALETTE,
    FRAMEBUFFER_OP_READ_PIXELS,
    FRAMEBUFFER_OP_STATUS,
    FRAMEBUFFER_STATE_PENDING,
    FRAMEBUFFER_STATE_READY,
    HID_OPEN_SETTLE_SECONDS,
    HID_INTERFACE_NUMBER,
    HID_USAGE,
    HID_USAGE_PAGE,
)

from helpers import QueueDevice, make_response


INFO = (bytes((0x50, 0x44, 0x31, 0x30, 0x30)) + b"V05") + b"0100000000" + b"\x01"


def test_game_status_retries_busy_and_validates_snapshot():
    snapshot = bytes((1, 3, 1, 2)) + bytes(44)
    device = QueueDevice([
        make_response(Command.GAME_STATUS, 11, status=Status.BUSY),
        make_response(Command.GAME_STATUS, 12, snapshot),
    ])
    assert HidTrigger(device, initial_sequence=11).game_status() == snapshot
    assert [p[5] for p in device.writes] == [Command.GAME_STATUS] * 2
    assert all(p[10:12] == b"\x00\x00" for p in device.writes)


@pytest.mark.parametrize("snapshot", [bytes(48), b"\x01", b"\x01" + bytes(48)])
def test_game_status_rejects_incompatible_payload(snapshot):
    device = QueueDevice([make_response(Command.GAME_STATUS, 11, snapshot)])
    with pytest.raises(ProtocolError, match="GAME_STATUS payload"):
        HidTrigger(device, initial_sequence=11).game_status()


def _framebuffer_info(operation, state, capture_id, palette_count):
    return (
        bytes(
            (
                operation,
                state,
                FRAMEBUFFER_ENCODING_INDEX8_RGB565,
                0,
            )
        )
        + (2).to_bytes(2, "little")
        + (2).to_bytes(2, "little")
        + palette_count.to_bytes(2, "little")
        + capture_id.to_bytes(4, "little")
    )


def _framebuffer_chunk(operation, capture_id, offset, total, data):
    return (
        bytes((operation,))
        + capture_id.to_bytes(4, "little")
        + offset.to_bytes(2, "little")
        + total.to_bytes(2, "little")
        + data
    )


class FakeRawHid(QueueDevice):
    def open_path(self, path):
        self.path = path


class ShortFirstButtonWriteDevice(QueueDevice):
    def __init__(self, responses=()):
        super().__init__(responses)
        self.failed = False

    def write(self, packet):
        self.writes.append(bytes(packet))
        if packet[5] == Command.DISPLAY_BUTTON and not self.failed:
            self.failed = True
            return -1
        return len(packet)


class FakeHidModule:
    def __init__(self, entries):
        self.entries = entries
        self.raw = FakeRawHid()

    def enumerate(self, vid, pid):
        return self.entries

    def device(self):
        return self.raw


def _entry(**changes):
    entry = {
        "vendor_id": 0x301A,
        "product_id": 0x4001,
        "usage_page": HID_USAGE_PAGE,
        "usage": HID_USAGE,
        "interface_number": HID_INTERFACE_NUMBER,
        "serial_number": "ABC",
        "path": b"path",
    }
    entry.update(changes)
    return entry


def test_discovery_filters_collection_and_serial(monkeypatch):
    settles = []
    monkeypatch.setattr(
        "skytuner_hid.hid_trigger.time.sleep", settles.append
    )
    module = FakeHidModule(
        [
            _entry(usage=2),
            _entry(interface_number=1),
            _entry(serial_number="OTHER"),
            _entry(),
        ]
    )
    trigger = HidTrigger.find(
        hid_module=module,
        serial_number="ABC",
        initial_sequence=9,
    )
    assert module.raw.path == b"path"
    assert settles == [HID_OPEN_SETTLE_SECONDS]
    trigger.close()
    assert module.raw.closed


def test_discovery_accepts_macos_unavailable_interface_number():
    module = FakeHidModule([_entry(interface_number=-1)])
    trigger = HidTrigger.find(
        hid_module=module,
        serial_number="ABC",
        initial_sequence=9,
    )
    assert module.raw.path == b"path"
    trigger.close()


def test_discovery_accepts_legacy_runtime_interface_zero():
    module = FakeHidModule([_entry(interface_number=0)])
    trigger = HidTrigger.find(
        hid_module=module,
        serial_number="ABC",
        initial_sequence=9,
    )
    assert module.raw.path == b"path"
    trigger.close()


def test_discovery_keeps_ambiguity_rejection_across_interface_sentinels():
    module = FakeHidModule(
        [_entry(interface_number=0), _entry(interface_number=-1)]
    )
    with pytest.raises(ProtocolError, match="multiple SkyTuner"):
        HidTrigger.find(
            hid_module=module,
            serial_number="ABC",
            initial_sequence=9,
        )


def test_discovery_not_found_suggests_shared_hid_remedy():
    with pytest.raises(DeviceNotFoundError, match="close other SkyTuner"):
        HidTrigger.find(
            hid_module=FakeHidModule([]), initial_sequence=1
        )


def test_enter_update_qualifies_control_path_before_one_shot_request():
    device = QueueDevice(
        [
            make_response(Command.INFO, 0xFFFE, INFO),
            make_response(Command.INFO, 0xFFFF, INFO),
            make_response(Command.ENTER_UPDATE, 0),
        ]
    )
    trigger = HidTrigger(device, initial_sequence=0xFFFE)
    assert trigger.get_info().firmware == "0100000000"
    trigger.enter_update()
    assert [int.from_bytes(p[8:10], "little") for p in device.writes] == [
        0xFFFE,
        0xFFFF,
        0,
    ]
    assert [packet[5] for packet in device.writes] == [
        Command.INFO,
        Command.INFO,
        Command.ENTER_UPDATE,
    ]


def test_button_commands_encode_press_and_release_states():
    device = QueueDevice(
        [
            make_response(Command.INFO, 20, INFO),
            make_response(Command.DISPLAY_BUTTON, 21),
            make_response(Command.INFO, 22, INFO),
            make_response(Command.TUNING_BUTTON, 23),
            make_response(Command.INFO, 24, INFO),
            make_response(Command.COMBO_BUTTON, 25),
            make_response(Command.COMBO_BUTTON, 26),
        ]
    )
    trigger = HidTrigger(device, initial_sequence=20)
    trigger.set_button(Button.DISPLAY, True)
    trigger.set_button(Button.TUNING, True)
    trigger.set_button(Button.COMBO, True)
    trigger.set_button(Button.COMBO, False)

    assert [packet[5] for packet in device.writes] == [
        Command.INFO,
        Command.DISPLAY_BUTTON,
        Command.INFO,
        Command.TUNING_BUTTON,
        Command.INFO,
        Command.COMBO_BUTTON,
        Command.COMBO_BUTTON,
    ]
    assert [packet[10:13] for packet in device.writes] == [
        b"\x00\x00\x00",
        b"\x01\x00\x01",
        b"\x00\x00\x00",
        b"\x01\x00\x01",
        b"\x00\x00\x00",
        b"\x01\x00\x01",
        b"\x01\x00\x00",
    ]


def test_button_press_retries_short_write_inside_failsafe_window(
    monkeypatch,
):
    settles = []
    monkeypatch.setattr(
        "skytuner_hid.hid_trigger.time.sleep", settles.append
    )
    device = ShortFirstButtonWriteDevice(
        [
            make_response(Command.INFO, 20, INFO),
            make_response(Command.DISPLAY_BUTTON, 22),
        ]
    )
    trigger = HidTrigger(device, initial_sequence=20)

    trigger.set_button(Button.DISPLAY, True)

    assert [packet[5] for packet in device.writes] == [
        Command.INFO,
        Command.DISPLAY_BUTTON,
        Command.DISPLAY_BUTTON,
    ]
    assert settles == [0.01, 0.025, 0.01]


def test_button_api_rejects_invalid_values_before_writing():
    device = QueueDevice()
    trigger = HidTrigger(device)
    with pytest.raises(ValueError, match="Button"):
        trigger.set_button(1, True)
    with pytest.raises(ValueError, match="bool"):
        trigger.set_button(Button.DISPLAY, 1)
    assert not device.writes


def test_framebuffer_capture_reads_palette_pixels_and_releases_lock():
    capture_id = 0x12345678
    device = QueueDevice(
        [
            make_response(
                Command.FRAMEBUFFER,
                30,
                _framebuffer_info(
                    FRAMEBUFFER_OP_BEGIN,
                    FRAMEBUFFER_STATE_PENDING,
                    capture_id,
                    0,
                ),
            ),
            make_response(
                Command.FRAMEBUFFER,
                31,
                _framebuffer_info(
                    FRAMEBUFFER_OP_STATUS,
                    FRAMEBUFFER_STATE_READY,
                    capture_id,
                    2,
                ),
            ),
            make_response(
                Command.FRAMEBUFFER,
                32,
                _framebuffer_chunk(
                    FRAMEBUFFER_OP_READ_PALETTE,
                    capture_id,
                    0,
                    4,
                    b"\x00\x00\xff\xff",
                ),
            ),
            make_response(
                Command.FRAMEBUFFER,
                33,
                _framebuffer_chunk(
                    FRAMEBUFFER_OP_READ_PIXELS,
                    capture_id,
                    0,
                    4,
                    b"\x00\x01\x01\x00",
                ),
            ),
            make_response(Command.FRAMEBUFFER, 34),
        ]
    )

    frame = HidTrigger(
        device, initial_sequence=30, timeout_ms=100
    ).capture_framebuffer()

    assert frame.capture_id == capture_id
    assert (frame.width, frame.height) == (2, 2)
    assert frame.palette == (0x0000, 0xFFFF)
    assert frame.pixels == b"\x00\x01\x01\x00"
    assert [packet[12] for packet in device.writes] == [
        FRAMEBUFFER_OP_BEGIN,
        FRAMEBUFFER_OP_STATUS,
        FRAMEBUFFER_OP_READ_PALETTE,
        FRAMEBUFFER_OP_READ_PIXELS,
        FRAMEBUFFER_OP_END,
    ]


def test_framebuffer_capture_retries_one_transient_short_write():
    capture_id = 0x10203040

    class OneShortWriteDevice(QueueDevice):
        def write(self, packet):
            self.writes.append(bytes(packet))
            if len(self.writes) == 1:
                return -1
            return len(packet)

    device = OneShortWriteDevice(
        [
            make_response(
                Command.FRAMEBUFFER,
                31,
                _framebuffer_info(
                    FRAMEBUFFER_OP_BEGIN,
                    FRAMEBUFFER_STATE_READY,
                    capture_id,
                    1,
                ),
            ),
            make_response(
                Command.FRAMEBUFFER,
                32,
                _framebuffer_chunk(
                    FRAMEBUFFER_OP_READ_PALETTE,
                    capture_id,
                    0,
                    2,
                    b"\x00\x00",
                ),
            ),
            make_response(
                Command.FRAMEBUFFER,
                33,
                _framebuffer_chunk(
                    FRAMEBUFFER_OP_READ_PIXELS,
                    capture_id,
                    0,
                    4,
                    b"\x00\x00\x00\x00",
                ),
            ),
            make_response(Command.FRAMEBUFFER, 34),
        ]
    )

    frame = HidTrigger(
        device, initial_sequence=30, timeout_ms=100
    ).capture_framebuffer()

    assert (frame.width, frame.height) == (2, 2)
    assert len(device.writes) == 5
    assert device.writes[0][12] == FRAMEBUFFER_OP_BEGIN
    assert device.writes[1][12] == FRAMEBUFFER_OP_BEGIN


def test_random_default_sequence_can_be_controlled(monkeypatch):
    monkeypatch.setattr(
        "skytuner_hid.hid_trigger.secrets.randbits", lambda bits: 0xBEEF
    )
    device = QueueDevice([make_response(Command.INFO, 0xBEEF, INFO)])
    HidTrigger(device).get_info()
    assert device.writes[0][8:10] == b"\xef\xbe"


def test_unmatched_valid_responses_are_skipped():
    device = QueueDevice(
        [
            make_response(Command.READ_LOG, 10, b"\x00" * 12),
            make_response(Command.INFO, 11, INFO),
            make_response(Command.INFO, 10, INFO),
        ]
    )
    assert HidTrigger(
        device, initial_sequence=10, timeout_ms=100
    ).get_info().model == "SkyTuner"
    assert len(device.read_timeouts) == 3


class TimeoutDevice(QueueDevice):
    def read(self, size, timeout_ms):
        time.sleep(timeout_ms / 1000.0)
        return b""


def test_one_write_uses_one_overall_deadline():
    device = TimeoutDevice()
    with pytest.raises(UpdateTimeoutError, match="INFO"):
        HidTrigger(
            device, initial_sequence=1, timeout_ms=5
        ).get_info()
    assert len(device.writes) == 1


class SerializedDevice(QueueDevice):
    def __init__(self):
        super().__init__()
        self.first_write = threading.Event()
        self.release = threading.Event()
        self._response = None

    def write(self, packet):
        self.writes.append(bytes(packet))
        self._response = make_response(
            packet[5], int.from_bytes(packet[8:10], "little"), INFO
        )
        if len(self.writes) == 1:
            self.first_write.set()
            assert self.release.wait(1)
        return len(packet)

    def read(self, size, timeout_ms):
        return self._response


class FailingWriteDevice(QueueDevice):
    def write(self, packet):
        self.writes.append(bytes(packet))
        return -1


class InfoDevice(QueueDevice):
    def write(self, packet):
        self.writes.append(bytes(packet))
        self._response = make_response(
            packet[5], int.from_bytes(packet[8:10], "little"), INFO
        )
        return len(packet)

    def read(self, size, timeout_ms):
        return self._response


def test_idempotent_command_survives_three_unusable_hid_handles(monkeypatch):
    monkeypatch.setattr(
        "skytuner_hid.hid_trigger.time.sleep", lambda _seconds: None
    )
    initial = FailingWriteDevice()
    reopened = [
        FailingWriteDevice(),
        FailingWriteDevice(),
        InfoDevice(),
    ]
    trigger = HidTrigger(
        initial,
        initial_sequence=1,
        reopen_device=lambda: reopened.pop(0),
    )

    info = trigger.get_info()

    assert info.firmware == "0100000000"
    assert initial.closed
    assert len(reopened) == 0


def test_transactions_are_serialized_per_instance():
    device = SerializedDevice()
    trigger = HidTrigger(device, initial_sequence=1, timeout_ms=1000)
    results = []
    threads = [
        threading.Thread(target=lambda: results.append(trigger.get_info()))
        for _ in range(2)
    ]
    threads[0].start()
    assert device.first_write.wait(1)
    threads[1].start()
    time.sleep(0.02)
    assert len(device.writes) == 1
    device.release.set()
    for thread in threads:
        thread.join(1)
    assert len(results) == 2
    assert len(device.writes) == 2


def test_context_manager_closes_device():
    device = QueueDevice()
    with HidTrigger(device, initial_sequence=1) as trigger:
        assert trigger is not None
    assert device.closed


def test_matching_error_response_is_not_retried():
    device = QueueDevice(
        [
            make_response(Command.INFO, 3, INFO),
            make_response(Command.ENTER_UPDATE, 4, status=3),
        ]
    )
    with pytest.raises(ProtocolError, match="BAD_LENGTH"):
        HidTrigger(device, initial_sequence=3).enter_update()
    assert [packet[5] for packet in device.writes] == [
        Command.INFO,
        Command.ENTER_UPDATE,
    ]
