"""Dependency-free PNG output for SkyTuner RGB565 framebuffer captures."""

from __future__ import annotations

import struct
import zlib
from pathlib import Path
from typing import BinaryIO, Union

from .models import Framebuffer


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    body = kind + data
    return (
        len(data).to_bytes(4, "big")
        + body
        + (zlib.crc32(body) & 0xFFFFFFFF).to_bytes(4, "big")
    )


def _rgb565_to_rgb888(color: int) -> bytes:
    red = (color >> 11) & 0x1F
    green = (color >> 5) & 0x3F
    blue = color & 0x1F
    return bytes(
        (
            (red * 255 + 15) // 31,
            (green * 255 + 31) // 63,
            (blue * 255 + 15) // 31,
        )
    )


def encode_png(framebuffer: Framebuffer) -> bytes:
    """Encode an indexed RGB565 framebuffer as a standard RGB PNG."""
    if framebuffer.width <= 0 or framebuffer.height <= 0:
        raise ValueError("framebuffer dimensions must be positive")
    if len(framebuffer.pixels) != framebuffer.width * framebuffer.height:
        raise ValueError("framebuffer pixel count does not match dimensions")
    if not 1 <= len(framebuffer.palette) <= 256:
        raise ValueError("framebuffer palette must contain 1-256 colors")
    if any(not 0 <= color <= 0xFFFF for color in framebuffer.palette):
        raise ValueError("framebuffer palette colors must be RGB565 uint16")
    if max(framebuffer.pixels, default=0) >= len(framebuffer.palette):
        raise ValueError("framebuffer contains an invalid palette index")

    palette = tuple(
        _rgb565_to_rgb888(color) for color in framebuffer.palette
    )
    scanlines = bytearray()
    for row in range(framebuffer.height):
        scanlines.append(0)
        start = row * framebuffer.width
        for index in framebuffer.pixels[start : start + framebuffer.width]:
            scanlines.extend(palette[index])

    signature = b"\x89PNG\r\n\x1a\n"
    header = struct.pack(
        ">IIBBBBB",
        framebuffer.width,
        framebuffer.height,
        8,
        2,
        0,
        0,
        0,
    )
    return (
        signature
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(bytes(scanlines), level=9))
        + _png_chunk(b"IEND", b"")
    )


def save_png(
    framebuffer: Framebuffer,
    output: Union[str, Path, BinaryIO],
) -> None:
    """Write a framebuffer PNG to a path or already-open binary stream."""
    png = encode_png(framebuffer)
    if hasattr(output, "write"):
        output.write(png)
        return
    Path(output).write_bytes(png)
