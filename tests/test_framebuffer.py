import struct
import zlib

from skytuner_hid import Framebuffer, encode_png


def _chunks(png):
    offset = 8
    while offset < len(png):
        size = int.from_bytes(png[offset : offset + 4], "big")
        kind = png[offset + 4 : offset + 8]
        data = png[offset + 8 : offset + 8 + size]
        yield kind, data
        offset += 12 + size


def test_png_encoder_converts_rgb565_without_external_dependencies():
    frame = Framebuffer(
        capture_id=7,
        width=2,
        height=1,
        palette=(0xF800, 0x07E0),
        pixels=b"\x00\x01",
    )
    png = encode_png(frame)
    chunks = list(_chunks(png))

    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert chunks[0][0] == b"IHDR"
    assert struct.unpack(">II", chunks[0][1][:8]) == (2, 1)
    compressed = b"".join(data for kind, data in chunks if kind == b"IDAT")
    assert zlib.decompress(compressed) == b"\x00\xff\x00\x00\x00\xff\x00"
