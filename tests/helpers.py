from collections import deque

from skytuner_hid.hid_trigger import (
    CRC_INPUT_SIZE,
    MAGIC,
    PROTOCOL_VERSION,
    REPORT_SIZE,
    Status,
    crc16_ccitt_false,
)


def make_response(command, sequence, payload=b"", status=Status.OK):
    packet = bytearray(REPORT_SIZE)
    packet[:4] = MAGIC
    packet[4] = PROTOCOL_VERSION
    packet[5] = int(command)
    packet[6] = int(status)
    packet[8:10] = int(sequence).to_bytes(2, "little")
    packet[10:12] = len(payload).to_bytes(2, "little")
    packet[12 : 12 + len(payload)] = payload
    packet[62:64] = crc16_ccitt_false(
        packet[:CRC_INPUT_SIZE]
    ).to_bytes(2, "little")
    return bytes(packet)


class QueueDevice:
    def __init__(self, responses=()):
        self.responses = deque(responses)
        self.writes = []
        self.closed = False
        self.read_timeouts = []

    def write(self, packet):
        self.writes.append(bytes(packet))
        return len(packet)

    def read(self, size, timeout_ms):
        self.read_timeouts.append(timeout_ms)
        return self.responses.popleft() if self.responses else b""

    def close(self):
        self.closed = True
