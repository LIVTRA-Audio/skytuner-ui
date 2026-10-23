from dataclasses import dataclass


@dataclass(frozen=True)
class DeviceInfo:
    model: str
    hardware: str
    firmware: str
    protocol: int


@dataclass(frozen=True)
class LogChunk:
    """One immutable READ_LOG snapshot from the device's RAM ring."""

    start: int
    next: int
    tail: int
    data: str
    gap: bool


@dataclass(frozen=True)
class Framebuffer:
    """One consistent indexed-color capture of the SkyTuner LCD."""

    capture_id: int
    width: int
    height: int
    palette: tuple[int, ...]
    pixels: bytes
