"""SkyTuner vendor-HID codec, discovery, log reader, and MSC detach trigger."""

from __future__ import annotations

import importlib
import math
import secrets
import threading
import time
from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Callable, Optional

from .errors import DeviceNotFoundError, ProtocolError, UpdateTimeoutError
from .models import DeviceInfo, Framebuffer, LogChunk


REPORT_SIZE = 64
PAYLOAD_SIZE = 50
CRC_INPUT_SIZE = 62
LOG_METADATA_SIZE = 12
LOG_TEXT_SIZE = 38
LOG_RECORD_COUNT = 32
LOG_TAIL_CURSOR = 0xFFFFFFFF
LOG_CURSOR_LAST = 0xFFFFFFFE
MAGIC = b"PD10"
PROTOCOL_VERSION = 1
ENTER_UPDATE_TOKEN = b"UPD1"
CONTROL_SETTLE_SECONDS = 0.010
HID_OPEN_SETTLE_SECONDS = 0.050
FRAMEBUFFER_TRANSPORT_RETRIES = 5
FRAMEBUFFER_RETRY_SETTLE_SECONDS = 0.025
IDEMPOTENT_TRANSPORT_RETRIES = 5
IDEMPOTENT_REOPEN_ATTEMPTS = 5
BUTTON_PRESS_TRANSPORT_RETRIES = 3
FRAMEBUFFER_CAPTURE_RESTARTS = 3
FRAMEBUFFER_INFO_SIZE = 14
FRAMEBUFFER_READ_METADATA_SIZE = 9
FRAMEBUFFER_READ_DATA_SIZE = PAYLOAD_SIZE - FRAMEBUFFER_READ_METADATA_SIZE
FRAMEBUFFER_ENCODING_INDEX8_RGB565 = 1
FRAMEBUFFER_STATE_PENDING = 0
FRAMEBUFFER_STATE_READY = 1

FRAMEBUFFER_OP_BEGIN = 0
FRAMEBUFFER_OP_STATUS = 1
FRAMEBUFFER_OP_READ_PALETTE = 2
FRAMEBUFFER_OP_READ_PIXELS = 3
FRAMEBUFFER_OP_END = 4

# USB identifiers used by SkyTuner's application interface.
DEFAULT_VENDOR_ID = 0x301A
PROVISIONAL_PRODUCT_ID = 0x4001

HID_USAGE_PAGE = 0xFF00
HID_USAGE = 0x01
HID_INTERFACE_NUMBER = 2
HID_LEGACY_INTERFACE_NUMBER = 0
# hidapi cannot expose the interface number on macOS and reports -1.
HID_INTERFACE_UNAVAILABLE = -1


class Command(IntEnum):
    INFO = 0x01
    ENTER_UPDATE = 0x02
    READ_LOG = 0x03
    DISPLAY_BUTTON = 0x04
    TUNING_BUTTON = 0x05
    COMBO_BUTTON = 0x06
    FRAMEBUFFER = 0x07
    CUSTOM_UI = 0x08
    GAME_STATUS = 0x09


class Button(IntEnum):
    DISPLAY = 1
    TUNING = 2
    COMBO = 3


BUTTON_COMMAND = {
    Button.DISPLAY: Command.DISPLAY_BUTTON,
    Button.TUNING: Command.TUNING_BUTTON,
    Button.COMBO: Command.COMBO_BUTTON,
}


class Status(IntEnum):
    OK = 0x00
    BAD_MAGIC = 0x01
    BAD_VERSION = 0x02
    BAD_LENGTH = 0x03
    BAD_CRC = 0x04
    BAD_COMMAND = 0x05
    BUSY = 0x06
    STALE_CAPTURE = 0x07


@dataclass(frozen=True)
class _DecodedReport:
    command: int
    status: Status
    sequence: int
    payload: bytes


def crc16_ccitt_false(data: bytes) -> int:
    crc = 0xFFFF
    for octet in data:
        crc ^= octet << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


def _cursor_successor(cursor: int) -> int:
    return 0 if cursor == LOG_CURSOR_LAST else cursor + 1


def _cursor_forward_distance(start: int, end: int) -> int:
    return end - start if end >= start else LOG_TAIL_CURSOR - start + end


def encode_request(
    command: Command,
    sequence: int,
    payload: bytes = b"",
) -> bytes:
    if not 0 <= int(command) <= 0xFF:
        raise ValueError("command must fit in one byte")
    if not 0 <= sequence <= 0xFFFF:
        raise ValueError("sequence must fit in two bytes")
    payload = bytes(payload)
    if len(payload) > PAYLOAD_SIZE:
        raise ValueError("payload exceeds 50 bytes")

    packet = bytearray(REPORT_SIZE)
    packet[0:4] = MAGIC
    packet[4] = PROTOCOL_VERSION
    packet[5] = int(command)
    packet[8:10] = sequence.to_bytes(2, "little")
    packet[10:12] = len(payload).to_bytes(2, "little")
    packet[12 : 12 + len(payload)] = payload
    packet[62:64] = crc16_ccitt_false(packet[:CRC_INPUT_SIZE]).to_bytes(
        2, "little"
    )
    return bytes(packet)


def _decode_packet(packet: bytes) -> _DecodedReport:
    packet = bytes(packet)
    if len(packet) != REPORT_SIZE:
        raise ProtocolError(
            f"short HID read: expected {REPORT_SIZE} bytes, got {len(packet)}"
        )
    if packet[0:4] != MAGIC:
        raise ProtocolError("response magic does not match PD10")
    if packet[4] != PROTOCOL_VERSION:
        raise ProtocolError(f"unsupported response version {packet[4]}")

    payload_len = int.from_bytes(packet[10:12], "little")
    if payload_len > PAYLOAD_SIZE:
        raise ProtocolError(f"invalid response payload length {payload_len}")

    expected_crc = crc16_ccitt_false(packet[:CRC_INPUT_SIZE])
    actual_crc = int.from_bytes(packet[62:64], "little")
    if actual_crc != expected_crc:
        raise ProtocolError(
            f"response CRC mismatch: got 0x{actual_crc:04x}, "
            f"expected 0x{expected_crc:04x}"
        )

    try:
        status = Status(packet[6])
    except ValueError as exc:
        raise ProtocolError(
            f"unknown response status 0x{packet[6]:02x}"
        ) from exc

    padding = packet[12 + payload_len : 62]
    if any(padding):
        raise ProtocolError("response payload padding is not zero")

    return _DecodedReport(
        command=packet[5],
        status=status,
        sequence=int.from_bytes(packet[8:10], "little"),
        payload=packet[12 : 12 + payload_len],
    )


def _decode_response(
    packet: bytes,
    *,
    expected_command: Command,
    expected_sequence: int,
) -> _DecodedReport:
    """Decode one exact response; retained for codec-level callers/tests."""
    response = _decode_packet(packet)
    if response.command != int(expected_command):
        raise ProtocolError(
            f"response command 0x{response.command:02x} does not match "
            f"0x{int(expected_command):02x}"
        )
    if response.sequence != expected_sequence:
        raise ProtocolError(
            f"response sequence {response.sequence} does not match "
            f"{expected_sequence}"
        )
    if response.status is not Status.OK:
        raise ProtocolError(
            f"device rejected {expected_command.name}: {response.status.name}"
        )
    return response


class _HidApiDevice:
    """Adapt hidapi's unnumbered report-ID framing to raw 64-byte packets."""

    def __init__(self, device: Any):
        self._device = device

    def write(self, packet: bytes) -> int:
        if len(packet) != REPORT_SIZE:
            raise ProtocolError("HID codec must write exactly 64 bytes")
        framed = b"\x00" + packet
        if hasattr(self._device, "send_feature_report"):
            written = self._device.send_feature_report(framed)
        else:
            written = self._device.write(framed)
        return REPORT_SIZE if written == REPORT_SIZE + 1 else int(written)

    def read(self, size: int, timeout_ms: int) -> bytes:
        if hasattr(self._device, "get_feature_report"):
            raw = self._device.get_feature_report(0, size + 1)
        else:
            raw = self._device.read(size, timeout_ms)
        if raw is None:
            return b""
        packet = bytes(raw)
        if len(packet) == size + 1 and packet[0] == 0:
            return packet[1:]
        return packet

    def close(self) -> None:
        self._device.close()


def _validate_trigger_options(
    initial_sequence: Optional[int], timeout_ms: int
) -> None:
    if initial_sequence is not None and (
        type(initial_sequence) is not int
        or not 0 <= initial_sequence <= 0xFFFF
    ):
        raise ValueError("initial sequence must be an integer from 0 to 65535")
    if type(timeout_ms) is not int or timeout_ms <= 0:
        raise ValueError("timeout must be a positive integer")


class HidTrigger:
    """One serialized connection to the SkyTuner normal-mode vendor HID."""

    def __init__(
        self,
        device: Any,
        *,
        initial_sequence: Optional[int] = None,
        timeout_ms: int = 1000,
        reopen_device: Optional[Callable[[], Any]] = None,
    ):
        _validate_trigger_options(initial_sequence, timeout_ms)
        self._device = device
        self._sequence = (
            secrets.randbits(16)
            if initial_sequence is None
            else initial_sequence
        )
        self._timeout_ms = timeout_ms
        self._reopen_device = reopen_device
        self._transaction_lock = threading.Lock()

    @classmethod
    def find(
        cls,
        *,
        hid_module: Optional[Any] = None,
        vendor_id: int = DEFAULT_VENDOR_ID,
        product_id: int = PROVISIONAL_PRODUCT_ID,
        serial_number: Optional[str] = None,
        initial_sequence: Optional[int] = None,
        timeout_ms: int = 1000,
    ) -> "HidTrigger":
        """Open one exact SkyTuner vendor-HID application collection."""
        _validate_trigger_options(initial_sequence, timeout_ms)
        if not 0 <= vendor_id <= 0xFFFF or not 0 <= product_id <= 0xFFFF:
            raise ValueError("USB VID and PID must each fit in two bytes")
        if hid_module is None:
            try:
                hid_module = importlib.import_module("hid")
            except ImportError as exc:
                raise ProtocolError(
                    "hidapi is required to discover SkyTuner"
                ) from exc

        def discover_matches() -> list[Any]:
            try:
                entries = list(hid_module.enumerate(vendor_id, product_id))
            except Exception as exc:
                raise ProtocolError(
                    "failed to enumerate SkyTuner HID devices; close other "
                    "SkyTuner software and try again"
                ) from exc
            found = []
            for entry in entries:
                if (
                    entry.get("vendor_id") != vendor_id
                    or entry.get("product_id") != product_id
                    or entry.get("usage_page") != HID_USAGE_PAGE
                    or entry.get("usage") != HID_USAGE
                    or entry.get("interface_number")
                    not in (
                        HID_INTERFACE_NUMBER,
                        HID_LEGACY_INTERFACE_NUMBER,
                        HID_INTERFACE_UNAVAILABLE,
                    )
                ):
                    continue
                if (
                    serial_number is not None
                    and entry.get("serial_number") != serial_number
                ):
                    continue
                if entry.get("path") is not None:
                    found.append(entry)
            return found

        matches = discover_matches()

        if not matches:
            raise DeviceNotFoundError(
                "SkyTuner normal-mode HID not found for "
                f"VID:PID {vendor_id:04x}:{product_id:04x}; reconnect normal "
                "firmware and close other SkyTuner software"
            )
        if len(matches) > 1:
            raise ProtocolError(
                "multiple SkyTuner devices match; provide an exact serial number"
            )

        path = matches[0]["path"]
        def open_selected_path() -> _HidApiDevice:
            reopened_matches = discover_matches()
            if len(reopened_matches) != 1:
                raise DeviceNotFoundError(
                    "SkyTuner HID path is not uniquely available during reopen"
                )
            current_path = reopened_matches[0]["path"]
            if hasattr(hid_module, "device"):
                raw_device = hid_module.device()
                raw_device.open_path(current_path)
            elif hasattr(hid_module, "Device"):
                raw_device = hid_module.Device(path=current_path)
            else:
                raise AttributeError("hid module exposes no device constructor")
            # Windows can publish this collection before the USB device's
            # previous handle close has fully retired.  A Feature
            # write in that short window returns -1 even though the device is
            # still healthy.  Settle before the first EP0 transaction; doing
            # this here keeps it outside virtual-button hold timing.
            time.sleep(HID_OPEN_SETTLE_SECONDS)
            return _HidApiDevice(raw_device)

        try:
            device = open_selected_path()
        except Exception as exc:
            raise ProtocolError(
                f"failed to open SkyTuner HID path {path!r}; close other SkyTuner "
                "software and try again"
            ) from exc

        return cls(
            device,
            initial_sequence=initial_sequence,
            timeout_ms=timeout_ms,
            reopen_device=open_selected_path,
        )

    def __enter__(self) -> "HidTrigger":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    def get_info(self) -> DeviceInfo:
        """Return validated model, hardware, firmware, and protocol identity."""
        response = self._idempotent_transact(Command.INFO)
        if len(response.payload) != 19:
            raise ProtocolError(
                f"INFO payload must be 19 bytes, got {len(response.payload)}"
            )
        try:
            model = response.payload[0:5].decode("ascii")
            hardware = response.payload[5:8].decode("ascii")
            firmware = response.payload[8:18].decode("ascii")
        except UnicodeDecodeError as exc:
            raise ProtocolError("INFO identity is not ASCII") from exc
        protocol = response.payload[18]

        # Firmware's fixed five-byte wire identity is unchanged by host branding.
        if response.payload[:5] != bytes((0x50, 0x44, 0x31, 0x30, 0x30)):
            raise ProtocolError(f"unexpected INFO model {model!r}")
        if (
            len(hardware) != 3
            or not hardware.startswith("V")
            or not hardware[1:].isdigit()
        ):
            raise ProtocolError(f"invalid INFO hardware revision {hardware!r}")
        if len(firmware) != 10 or not firmware.isdigit():
            raise ProtocolError(f"invalid INFO firmware build {firmware!r}")
        if protocol != PROTOCOL_VERSION:
            raise ProtocolError(f"unsupported INFO protocol {protocol}")

        return DeviceInfo("SkyTuner", hardware, firmware, protocol)

    def read_log(self, cursor: Optional[int] = None) -> LogChunk:
        """Pull one non-destructive RAM-log record at an optional cursor."""
        if cursor is not None and (
            type(cursor) is not int or not 0 <= cursor <= 0xFFFFFFFF
        ):
            raise ValueError("log cursor must be None or a uint32")
        payload = b"" if cursor is None else cursor.to_bytes(4, "little")
        response = self._idempotent_transact(Command.READ_LOG, payload)
        if not LOG_METADATA_SIZE <= len(response.payload) <= PAYLOAD_SIZE:
            raise ProtocolError(
                "READ_LOG payload must contain 12-byte metadata and at most "
                "38 text bytes"
            )

        start = int.from_bytes(response.payload[0:4], "little")
        next_cursor = int.from_bytes(response.payload[4:8], "little")
        tail = int.from_bytes(response.payload[8:12], "little")
        data_bytes = response.payload[12:]

        if LOG_TAIL_CURSOR in (start, next_cursor, tail):
            raise ProtocolError(
                "READ_LOG response metadata used the reserved from-now "
                "sentinel"
            )
        retained_distance = _cursor_forward_distance(start, tail)
        if retained_distance > LOG_RECORD_COUNT:
            raise ProtocolError(
                "READ_LOG start is outside the 32-record retained window"
            )
        if data_bytes:
            if retained_distance == 0:
                raise ProtocolError("READ_LOG returned data at the tail")
            if next_cursor != _cursor_successor(start):
                raise ProtocolError(
                    "READ_LOG record next cursor must equal start + 1"
                )
        elif next_cursor != start:
            raise ProtocolError(
                "empty READ_LOG response must leave the cursor unchanged"
            )
        try:
            data = data_bytes.decode("ascii")
        except UnicodeDecodeError as exc:
            raise ProtocolError("READ_LOG record is not ASCII") from exc

        gap = (
            cursor is not None
            and cursor != LOG_TAIL_CURSOR
            and start != cursor
        )
        return LogChunk(start, next_cursor, tail, data, gap)

    def enter_update(self) -> None:
        """Send one ACK-first request to restart into the MSC loader."""
        self._qualify_control_path()
        response = self._transact(
            Command.ENTER_UPDATE,
            ENTER_UPDATE_TOKEN,
        )
        if response.payload:
            raise ProtocolError(
                f"ENTER_UPDATE ACK payload must be empty, got "
                f"{len(response.payload)} bytes"
            )

    def set_button(self, button: Button, pressed: bool) -> None:
        """Set one logical button state; firmware applies physical timing."""
        if not isinstance(button, Button):
            raise ValueError("button must be a Button value")
        if type(pressed) is not bool:
            raise ValueError("pressed must be a bool")
        command = BUTTON_COMMAND[button]
        if pressed:
            self._qualify_control_path()
            # A short write can still have delivered the Feature report before
            # Windows loses the status stage. Retrying the same asserted state
            # immediately is safe while the firmware's 300 ms abandoned-press
            # timer is still active: it refreshes one hold and cannot create a
            # second edge. Do not reopen or retry timeouts here, because either
            # could cross that bounded window and become a second press.
            response = self._idempotent_transact(
                command,
                b"\x01",
                retries=BUTTON_PRESS_TRANSPORT_RETRIES,
                retry_timeouts=False,
                reopen_attempts=0,
            )
        else:
            response = self._idempotent_transact(command, b"\x00")
        if response.payload:
            raise ProtocolError(
                f"{command.name} ACK payload must be empty, "
                f"got {len(response.payload)} bytes"
            )

    def _qualify_control_path(self) -> None:
        """Prove a reopened/resumed EP0 path before a one-shot side effect.

        INFO is safe to retry and reopen.  Button presses and loader detach
        are not: retrying either after a lost ACK could apply the action
        twice.  A successful INFO transaction first turns a stale Windows HID
        handle or a just-resumed control endpoint into a recoverable failure
        before the one-shot request is sent.
        """
        response = self._idempotent_transact(Command.INFO)
        if len(response.payload) != 19:
            raise ProtocolError(
                f"INFO payload must be 19 bytes, got {len(response.payload)}"
            )

    def _transact_after_control_qualification(
        self, command: Command, payload: bytes
    ) -> _DecodedReport:
        self._qualify_control_path()
        return self._transact(command, payload)

    def capture_framebuffer(self) -> Framebuffer:
        """Read one stable 128x160 LCD frame without stopping the tuner."""
        last_error: ProtocolError | UpdateTimeoutError | None = None
        for attempt in range(FRAMEBUFFER_CAPTURE_RESTARTS):
            try:
                return self._capture_framebuffer_once()
            except UpdateTimeoutError as exc:
                last_error = exc
            except ProtocolError as exc:
                if not any(
                    marker in str(exc)
                    for marker in (
                        "STALE_CAPTURE",
                        "FRAMEBUFFER: BUSY",
                        "short HID write",
                        "HID write failed",
                        "HID read failed",
                    )
                ):
                    raise
                last_error = exc
            if attempt + 1 < FRAMEBUFFER_CAPTURE_RESTARTS:
                time.sleep(0.1)
        assert last_error is not None
        raise last_error

    def _capture_framebuffer_once(self) -> Framebuffer:
        begin = self._framebuffer_transact(bytes((FRAMEBUFFER_OP_BEGIN,)))
        capture_id, width, height, palette_count, state = (
            self._decode_framebuffer_info(begin.payload, FRAMEBUFFER_OP_BEGIN)
        )

        try:
            deadline = time.monotonic() + self._timeout_ms / 1000.0
            while state != FRAMEBUFFER_STATE_READY:
                if state != FRAMEBUFFER_STATE_PENDING:
                    raise ProtocolError(
                        f"invalid framebuffer capture state {state}"
                    )
                if time.monotonic() >= deadline:
                    raise UpdateTimeoutError(
                        "timed out waiting for FRAMEBUFFER capture"
                    )
                status = self._framebuffer_transact(
                    bytes((FRAMEBUFFER_OP_STATUS,))
                    + capture_id.to_bytes(4, "little"),
                )
                (
                    status_id,
                    status_width,
                    status_height,
                    palette_count,
                    state,
                ) = self._decode_framebuffer_info(
                    status.payload, FRAMEBUFFER_OP_STATUS
                )
                if (
                    status_id != capture_id
                    or status_width != width
                    or status_height != height
                ):
                    raise ProtocolError(
                        "framebuffer STATUS identity changed during capture"
                    )

            if not 1 <= palette_count <= 256:
                raise ProtocolError(
                    f"invalid framebuffer palette size {palette_count}"
                )
            palette_bytes = self._read_framebuffer_region(
                capture_id,
                FRAMEBUFFER_OP_READ_PALETTE,
                palette_count * 2,
            )
            pixels = self._read_framebuffer_region(
                capture_id,
                FRAMEBUFFER_OP_READ_PIXELS,
                width * height,
            )
            palette = tuple(
                int.from_bytes(palette_bytes[i : i + 2], "little")
                for i in range(0, len(palette_bytes), 2)
            )
            if max(pixels, default=0) >= len(palette):
                raise ProtocolError(
                    "framebuffer contains a palette index outside its palette"
                )
            return Framebuffer(
                capture_id, width, height, palette, pixels
            )
        finally:
            try:
                self._framebuffer_transact(
                    bytes((FRAMEBUFFER_OP_END,))
                    + capture_id.to_bytes(4, "little"),
                )
            except (ProtocolError, UpdateTimeoutError):
                # Firmware also has a bounded auto-release, so a disconnect or
                # stale END cannot leave the UI frozen.
                pass

    @staticmethod
    def _decode_framebuffer_info(
        payload: bytes, expected_operation: int
    ) -> tuple[int, int, int, int, int]:
        if len(payload) != FRAMEBUFFER_INFO_SIZE:
            raise ProtocolError(
                "FRAMEBUFFER info payload must be 14 bytes"
            )
        if payload[0] != expected_operation:
            raise ProtocolError("FRAMEBUFFER info operation does not match")
        if payload[2] != FRAMEBUFFER_ENCODING_INDEX8_RGB565:
            raise ProtocolError(
                f"unsupported framebuffer encoding {payload[2]}"
            )
        if payload[3] != 0:
            raise ProtocolError("FRAMEBUFFER info reserved byte is nonzero")
        width = int.from_bytes(payload[4:6], "little")
        height = int.from_bytes(payload[6:8], "little")
        palette_count = int.from_bytes(payload[8:10], "little")
        capture_id = int.from_bytes(payload[10:14], "little")
        if capture_id == 0 or width == 0 or height == 0:
            raise ProtocolError("FRAMEBUFFER info identity is invalid")
        return capture_id, width, height, palette_count, payload[1]

    def _read_framebuffer_region(
        self, capture_id: int, operation: int, expected_size: int
    ) -> bytes:
        data = bytearray()
        while len(data) < expected_size:
            offset = len(data)
            response = self._framebuffer_transact(
                bytes((operation,))
                + capture_id.to_bytes(4, "little")
                + offset.to_bytes(2, "little"),
            )
            payload = response.payload
            if len(payload) <= FRAMEBUFFER_READ_METADATA_SIZE:
                raise ProtocolError(
                    "FRAMEBUFFER read returned no pixel data"
                )
            if payload[0] != operation:
                raise ProtocolError(
                    "FRAMEBUFFER read operation does not match"
                )
            if int.from_bytes(payload[1:5], "little") != capture_id:
                raise ProtocolError("FRAMEBUFFER capture ID changed")
            if int.from_bytes(payload[5:7], "little") != offset:
                raise ProtocolError("FRAMEBUFFER read offset changed")
            total = int.from_bytes(payload[7:9], "little")
            if total != expected_size:
                raise ProtocolError(
                    f"FRAMEBUFFER region size changed: {total} != "
                    f"{expected_size}"
                )
            chunk = payload[FRAMEBUFFER_READ_METADATA_SIZE:]
            if len(chunk) > FRAMEBUFFER_READ_DATA_SIZE:
                raise ProtocolError("FRAMEBUFFER read chunk is too large")
            if len(data) + len(chunk) > expected_size:
                raise ProtocolError("FRAMEBUFFER read exceeded region size")
            data.extend(chunk)
        return bytes(data)

    def game_status(self) -> bytes:
        """Read the task-published 48-byte game/input diagnostic snapshot."""
        for attempt in range(IDEMPOTENT_TRANSPORT_RETRIES):
            try:
                payload = self._idempotent_transact(
                    Command.GAME_STATUS, retry_timeouts=True,
                ).payload
                break
            except ProtocolError as exc:
                if "GAME_STATUS: BUSY" not in str(exc) or attempt + 1 == IDEMPOTENT_TRANSPORT_RETRIES:
                    raise
                time.sleep(0.01)
        if len(payload) != 48 or payload[0] != 1:
            raise ProtocolError("unsupported GAME_STATUS payload")
        return payload

    def custom_ui_request(self, payload: bytes) -> bytes:
        """Send one idempotent, session-tagged custom UI operation.

        The UI protocol reports its asynchronous state and detailed errors in
        the returned payload. Only STATUS confirms completion of a flash job.
        """
        if not isinstance(payload, bytes) or not 1 <= len(payload) <= PAYLOAD_SIZE:
            raise ValueError("custom UI payload must contain 1..50 bytes")
        return self._idempotent_transact(
            Command.CUSTOM_UI, payload, retry_timeouts=True,
        ).payload

    def _framebuffer_transact(self, payload: bytes) -> _DecodedReport:
        """Retry only idempotent framebuffer transport operations.

        A 128x160 indexed capture requires hundreds of Feature transfers.
        Windows can transiently return a short write/read while another USB
        audio device is streaming. Framebuffer BEGIN/STATUS/READ are safe to
        repeat; loader detach intentionally remains one-shot.
        """
        return self._idempotent_transact(
            Command.FRAMEBUFFER,
            payload,
            retries=FRAMEBUFFER_TRANSPORT_RETRIES,
            retry_timeouts=True,
        )

    def _idempotent_transact(
        self,
        command: Command,
        payload: bytes = b"",
        *,
        retries: int = IDEMPOTENT_TRANSPORT_RETRIES,
        retry_timeouts: bool = False,
        reopen_attempts: int = IDEMPOTENT_REOPEN_ATTEMPTS,
    ) -> _DecodedReport:
        """Retry transport failures for a safely repeatable request.

        The vendor EP0 path can transiently reject a Feature transfer while
        remaining enumerated. ENTER_UPDATE deliberately bypasses this helper:
        repeating a successful detach after a lost ACK would be unsafe.
        """
        last_error: ProtocolError | UpdateTimeoutError | None = None

        for attempt in range(retries):
            try:
                return self._transact(command, payload)
            except UpdateTimeoutError as exc:
                if not retry_timeouts:
                    raise
                last_error = exc
            except ProtocolError as exc:
                message = str(exc)
                if not any(
                    marker in message
                    for marker in (
                        "short HID write",
                        "HID write failed",
                        "HID read failed",
                    )
                ):
                    raise
                last_error = exc
            if attempt + 1 < retries:
                time.sleep(FRAMEBUFFER_RETRY_SETTLE_SECONDS)

        assert last_error is not None
        if reopen_attempts > 0 and self._reopen_device is not None:
            self._reopen_after_transport_reset()
            return self._idempotent_transact(
                command,
                payload,
                retries=retries,
                retry_timeouts=retry_timeouts,
                reopen_attempts=reopen_attempts - 1,
            )
        raise last_error

    def _reopen_after_transport_reset(self) -> None:
        assert self._reopen_device is not None
        deadline = time.monotonic() + 3.0
        last_error: Optional[Exception] = None

        with self._transaction_lock:
            try:
                self._device.close()
            except Exception:
                pass
            # Windows keeps the old HID collection path visible briefly while
            # the USB device finishes its reset. Opening it immediately
            # succeeds but produces another unusable handle.
            time.sleep(0.75)
            while time.monotonic() < deadline:
                try:
                    self._device = self._reopen_device()
                    return
                except Exception as exc:
                    last_error = exc
                    time.sleep(0.1)
        raise ProtocolError(
            "SkyTuner HID did not re-enumerate after a transport reset"
        ) from last_error

    def close(self) -> None:
        with self._transaction_lock:
            self._device.close()

    def _transact(
        self,
        command: Command,
        payload: bytes = b"",
    ) -> _DecodedReport:
        with self._transaction_lock:
            sequence = self._sequence
            self._sequence = (self._sequence + 1) & 0xFFFF
            request = encode_request(command, sequence, payload)
            deadline = time.monotonic() + self._timeout_ms / 1000.0

            try:
                written = self._device.write(request)
            except TimeoutError as exc:
                raise UpdateTimeoutError(
                    f"timed out writing {command.name}"
                ) from exc
            except Exception as exc:
                raise ProtocolError(
                    f"HID write failed for {command.name}"
                ) from exc
            if written != REPORT_SIZE:
                raise ProtocolError(
                    f"short HID write: expected {REPORT_SIZE} bytes, "
                    f"got {written}"
                )

            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise UpdateTimeoutError(
                        f"timed out waiting for {command.name}"
                    )
                read_timeout_ms = max(1, math.ceil(remaining * 1000.0))
                try:
                    packet = self._device.read(
                        REPORT_SIZE, read_timeout_ms
                    )
                except TimeoutError as exc:
                    raise UpdateTimeoutError(
                        f"timed out waiting for {command.name}"
                    ) from exc
                except Exception as exc:
                    raise ProtocolError(
                        f"HID read failed for {command.name}"
                    ) from exc
                if packet is None or len(packet) == 0:
                    if time.monotonic() >= deadline:
                        raise UpdateTimeoutError(
                            f"timed out waiting for {command.name}"
                        )
                    continue

                response = _decode_packet(bytes(packet))
                if (
                    response.command != int(command)
                    or response.sequence != sequence
                ):
                    continue
                # Allow the EP0 status stage and the firmware bottom half to
                # retire before issuing another Feature SET_REPORT.
                time.sleep(CONTROL_SETTLE_SECONDS)
                if response.status is not Status.OK:
                    raise ProtocolError(
                        f"device rejected {command.name}: "
                        f"{response.status.name}"
                    )
                return response
