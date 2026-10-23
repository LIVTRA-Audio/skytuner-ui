from pathlib import Path
import shutil
import struct
import subprocess
import sys
import zlib

import pytest

ROOT = Path(__file__).resolve().parents[1]
import skytuner_ui as ui
from test_custom_ui import AsyncDevice


@pytest.fixture
def theme():
    return ui.compile_script(ROOT / "examples/full_theme.py")




def fix_crc(data):
    data = bytearray(data)
    struct.pack_into("<I", data, 12, zlib.crc32(data[128:]))
    struct.pack_into("<I", data, 124, zlib.crc32(data[:124]))
    return bytes(data)






@pytest.mark.parametrize("scene", ["tune", "poly", "meter", "boot"])
def test_preview_each_screen(theme, scene):
    image = ui.render_preview(theme, note="A", cents=-12, scene=scene)
    assert image.size == (128, 160)
    if scene == "meter":
        background = image.getpixel((0, 90))
        assert any(image.getpixel((x, y)) != background for x in range(8, 120) for y in range(85, 129))
    if scene == "boot":
        assert ui.render_preview(theme, scene="boot", frame=0).tobytes() != ui.render_preview(theme, scene="boot", frame=23).tobytes()


def test_unsupported_firmware_rejected_before_begin(theme):
    class Legacy:
        def __init__(self): self.calls = []
        def custom_ui_request(self, payload):
            self.calls.append(payload)
            return bytes(32)
    device = Legacy()
    with pytest.raises(ui.CustomUIError, match="firmware needs an upgrade"):
        ui.Uploader(device).upload(theme)
    assert device.calls == [b"\x06"]


def test_theme_upload_keeps_transaction_and_commit_guarantees(theme):
    class NewDevice(AsyncDevice):
        def custom_ui_request(self, payload):
            if payload == b"\x06":
                return b"PUI2\x02\x06" + bytes(2) + struct.pack("<I", ui.MAX_PACKAGE) + bytes(20)
            return super().custom_ui_request(payload)
    device = NewDevice(lost_ack=True)
    status = ui.Uploader(device, poll_seconds=0).upload(theme)
    assert status.enabled and status.generation == 9 and device.chunks == theme




@pytest.mark.parametrize("malformation", ["cross_scene_if", "header_escape", "scene_overlap",
                                            "text_overflow", "text_control", "reserved", "scene_gap"])
def test_reject_crc_valid_malformed_theme(theme, malformation):
    data = bytearray(theme)
    start = struct.unpack_from("<I", data, 56)[0]
    if malformation == "cross_scene_if":
        data[start:start + 24] = ui.Op(4, value=127).pack()
    elif malformation == "header_escape":
        struct.pack_into("<h", data, start + 6, 15)
    elif malformation == "scene_overlap":
        struct.pack_into("<H", data, 72, 1)
    elif malformation == "text_overflow":
        package = ui.parse_package(theme)
        index = next(i for i, op in enumerate(package.ops) if op.code == 5 and op.arg == 0)
        struct.pack_into("<I", data, start + index * 24 + 16, 2048)
    elif malformation == "text_control":
        data[struct.unpack_from("<I", data, 100)[0]] = 0
    elif malformation == "reserved":
        data[110] = 1
    else:
        struct.pack_into("<H", data, 70, 0)
    data = fix_crc(data)
    with pytest.raises(ui.CustomUIError):
        ui.parse_package(data)



@pytest.fixture
def page_theme(tmp_path):
    from PIL import Image
    Image.new("RGB", (128, 160), "#102840").save(tmp_path / "tune.png")
    Image.new("RGB", (128, 160), "#e8c080").save(tmp_path / "meter.png")
    source = 'BACKGROUND_TUNE = "tune.png"\nBACKGROUND_METER = "meter.png"\n'
    for name, y in [("header", 3), ("footer", 146), ("tune", 30), ("poly", 30), ("meter", 30), ("boot", 30)]:
        source += f'def draw_{name}(ui,state):\n    ui.rect(x=10,y={y},width=1,height=1,color="#ffffff")\n'
    path = tmp_path / "pages.py"
    path.write_text(source, encoding="utf-8")
    return path, ui.compile_script(path)


def test_independent_backgrounds_preview_and_header_footer(page_theme):
    _, data = page_theme
    p = ui.parse_package(data)
    assert p.version == 3 and p.background_pages == (1, 1, 2, 0)
    assert len(p.background) == 40960
    assert len(p.palette) <= 128 and len(data) <= ui.MAX_PACKAGE
    for scene, color in [("tune", "#102840"), ("poly", "#102840"), ("meter", "#e8c080"), ("boot", None)]:
        image = ui.render_preview(data, scene=scene)
        expected = ui.rgb888(ui.rgb565(color)) if color else ui.rgb888(p.palette[p.clear])
        assert all(image.getpixel((0, y)) == expected for y in [0, 16, 80, 144, 159])


def test_background_fallback_none_and_deduplication(page_theme):
    path, _ = page_theme
    path.write_text(path.read_text().replace('BACKGROUND_TUNE = "tune.png"', 'BACKGROUND = "tune.png"\nBACKGROUND_TUNE = "tune.png"').replace('BACKGROUND_METER = "meter.png"','BACKGROUND_METER = None'), encoding="utf-8")
    p = ui.parse_package(ui.compile_script(path))
    assert p.background_pages == (1, 1, 0, 1) and len(p.background) == 20480


def test_three_full_backgrounds_exceed_fixed_package_limit(page_theme):
    from PIL import Image
    path, _ = page_theme
    Image.new("RGB", (128, 160), "red").save(path.parent / "boot.png")
    path.write_text('BACKGROUND_BOOT = "boot.png"\n' + path.read_text(), encoding="utf-8")
    with pytest.raises(ui.CustomUIError, match="60 KiB"):
        ui.compile_script(path)


@pytest.mark.parametrize("field,value", [(108, 3), (112, 0xffffffff), (116, 1), (36, 40959), (92, 1)])
def test_invalid_page_references_and_lengths_rejected(page_theme, field, value):
    _, data = page_theme
    data = bytearray(data)
    struct.pack_into("<I", data, field, value)
    with pytest.raises(ui.CustomUIError):
        ui.parse_package(fix_crc(data))


def test_page_theme_refused_before_begin_on_v2_only_firmware(page_theme):
    class V2:
        def __init__(self): self.calls = []
        def custom_ui_request(self, payload):
            self.calls.append(payload)
            return b"PUI2\x02\x06" + bytes(2) + struct.pack("<I", ui.MAX_PACKAGE) + bytes(20)
    d = V2()
    with pytest.raises(ui.CustomUIError, match="firmware needs an upgrade"):
        ui.Uploader(d).upload(page_theme[1])
    assert d.calls == [b"\x07"]


def test_v3_upload_with_lost_ack_confirms_durable_commit(page_theme):
    class V3(AsyncDevice):
        def custom_ui_request(self, payload):
            if payload == b"\x07":
                return b"PUI3\x03\x06" + bytes(2) + struct.pack("<I", ui.MAX_PACKAGE) + bytes(20)
            return super().custom_ui_request(payload)
    d = V3(lost_ack=True)
    status = ui.Uploader(d, poll_seconds=0).upload(page_theme[1])
    assert status.enabled and status.generation == 9 and d.chunks == page_theme[1]
