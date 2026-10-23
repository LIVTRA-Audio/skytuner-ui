"""Host API for SkyTuner normal-mode HID diagnostics and safe MSC detach."""

from .errors import (
    DeviceNotFoundError,
    SkyTunerError,
    ProtocolError,
    UpdateTimeoutError,
)
from .hid_trigger import (
    Button,
    DEFAULT_VENDOR_ID,
    LOG_TAIL_CURSOR,
    PROVISIONAL_PRODUCT_ID,
    Command,
    HidTrigger,
    Status,
)
from .framebuffer import encode_png, save_png
from .models import DeviceInfo, Framebuffer, LogChunk

__all__ = [
    "Button",
    "Command",
    "DEFAULT_VENDOR_ID",
    "DeviceInfo",
    "DeviceNotFoundError",
    "Framebuffer",
    "HidTrigger",
    "LOG_TAIL_CURSOR",
    "LogChunk",
    "SkyTunerError",
    "ProtocolError",
    "PROVISIONAL_PRODUCT_ID",
    "Status",
    "UpdateTimeoutError",
    "encode_png",
    "save_png",
]
