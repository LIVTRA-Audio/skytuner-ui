from __future__ import annotations

from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import zlib

from PIL import Image
import pytest

ROOT = Path(__file__).resolve().parents[1]
import skytuner_ui as ui
from skytuner_hid.errors import ProtocolError, UpdateTimeoutError


@pytest.fixture
def sample():
    return ui.compile_script(ROOT / "examples" / "navy.py")


def script(tmp_path, source):
    path = tmp_path / "theme.py"
    path.write_text(source, encoding="utf-8")
    return path


def fix_crc(data):
    result = bytearray(data)
    struct.pack_into("<I", result, 12, zlib.crc32(result[128:]))
    struct.pack_into("<I", result, 124, zlib.crc32(result[:124]))
    return bytes(result)


def change_style(data, style):
    result = bytearray(data)
    offset = struct.unpack_from("<I", data, 56)[0]
    count = struct.unpack_from("<H", data, 60)[0]
    for i in range(count):
        if result[offset + i * 24] == 3:
            result[offset + i * 24 + 3] = style
    return fix_crc(result)


def test_sample_contains_actual_background_aligned_glyphs_and_condition(sample):
    p = ui.parse_package(sample)
    assert len(p.background) == 20480
    assert len(p.palette) <= 128
    assert set(p.glyphs) == set(ui.KEYS)
    assert len({g.height for g in p.glyphs.values()}) == 1
    assert all(any(g.mask) for g in p.glyphs.values())
    assert p.ops[0].code == 3 and p.ops[0].arg == 2 and p.ops[0].value == 50000
    assert p.ops[1].x == 8 and p.ops[1].y == 65 and p.ops[1].arg == 1
    assert p.ops[2] == ui.Op(4, arg=1, value=1)
    assert sample == ui.compile_script(ROOT / "examples" / "navy.py")


@pytest.mark.parametrize("source", [
    "import os\ndef draw(ui, state):\n    ui.rect()\n",
    "def draw(ui, state):\n    __import__('os').system('bad')\n",
    "def draw(ui, state):\n    for n in range(3):\n        ui.rect()\n",
    "def draw(ui, state):\n    if state.in_tune:\n        ui.rect()\n    else:\n        ui.rect()\n",
    "def draw(ui, state):\n    ui.text('E')\n",
    "def draw(ui, state):\n    ui.tuning_meter(state.frequency)\n",
    "def draw(ui, state):\n    ui.rect(color=state.color)\n",
    "def draw(ui, state):\n    ui.tuning_meter(state.cents, style='circle')\n",
    "def draw(ui, state):\n    ui.tuning_meter(state.cents, range_cents=0.5)\n",
    "def draw(ui, state):\n    if state.in_tune:\n        ui.image('background')\n",
    "@something\ndef draw(ui, state):\n    ui.rect()\n",
    "def draw(ui, state=1):\n    ui.rect()\n",
    "def draw(ui: int, state):\n    ui.rect()\n",
    "def draw(ui, state):\n    ui.rect(**{'x': 1})\n",
    "def draw(ui, state):\n    x = 10\n",
])
def test_restricted_ast_rejects_unsupported_source_with_line(tmp_path, source):
    with pytest.raises(ui.CustomUIError, match=r"theme.py:\d+:"):
        ui.compile_script(script(tmp_path, source))


def test_constants_nested_conditions_aliases_and_rgb(tmp_path):
    source = """FONT_SIZE=30
X=64
C=(10,20,30)
def draw(ui, state):
    if state.signal_valid:
        if state.flat:
            ui.rect(x=0,y=16,width=2,height=2,color=C)
    ui.note(state.note_name,x=X,y=60,color=ui.GREEN if state.in_tune else ui.ORANGE)
    ui.tuning_meter(value=state.cents,x=8,y=30,width=112,height=4,range_cents=25.5,style='needle')
"""
    p = ui.parse_package(ui.compile_script(script(tmp_path, source)))
    assert p.ops[0].value == 2 and p.ops[1].value == 1
    assert p.ops[-1].value == 25500 and p.ops[-1].arg == 1
    assert ui.rgb565((10, 20, 30)) in p.palette


@pytest.mark.parametrize("x,width", [(-32768, 32767), (32767, 32767), (0, 112), (64, -32768)])
def test_extreme_note_center_translation_reports_line_error_before_packing(tmp_path, x, width):
    path = script(tmp_path, f"def draw(ui,state):\n    ui.note(state.note,x={x},y=60,width={width},height=40)\n")
    with pytest.raises(ui.CustomUIError, match=r"theme.py:2: drawing box"):
        ui.compile_script(path)


@pytest.mark.parametrize("extra,body,match", [
    ("FONT_SIZE=81", "ui.text(state.note)", "FONT_SIZE"),
    ("FONT_SIZE=80", "ui.text(state.note,width=30)", "too small|metrics"),
    ("FONT='missing.ttf'", "ui.text(state.note)", "cannot load font"),
    ("BACKGROUND='missing.png'", "ui.image('background')\n    ui.rect(x=1,y=20,width=1,height=1)", "BACKGROUND"),
    ("", "ui.rect(x=-1,y=16,width=10,height=10)", "drawing box"),
    ("", "ui.rect(x=0,y=0,width=10,height=10)", "drawing box"),
    ("", "ui.rect(x=0,y=16,width=128,height=128)\n    ui.rect(x=0,y=16,width=1,height=1)", "budget"),
    ("", "\n    ".join("ui.text(state.note,x=64,y=60,width=80,height=40)" for _ in range(7)), "budget"),
    ("", "\n    ".join("ui.rect(x=0,y=16,width=1,height=1)" for _ in range(65)), "count"),
    ("", "ui.rect(x=0,y=16,width=1,height=1,color='#zzzzzz')", "hex color"),
])
def test_invalid_assets_geometry_font_and_work_budget(tmp_path, extra, body, match):
    with pytest.raises(ui.CustomUIError, match=match):
        ui.compile_script(script(tmp_path, extra + "\ndef draw(ui, state):\n    " + body + "\n"))


def test_invalid_image_and_missing_glyph(tmp_path, monkeypatch):
    (tmp_path / "bad.png").write_text("not an image")
    with pytest.raises(ui.CustomUIError, match="BACKGROUND"):
        ui.compile_script(script(tmp_path, "BACKGROUND='bad.png'\ndef draw(ui,state):\n    ui.image('background')\n    ui.rect()"))
    class MissingFont:
        def getbbox(self, *args, **kwargs):
            return (0, -10, 10, 0)
        def getmask(self, *args, **kwargs):
            return bytes([255] * 100)
        def getlength(self, *args):
            return 10
    monkeypatch.setattr(ui.ImageFont, "truetype", lambda *a, **k: MissingFont())
    with pytest.raises(ui.CustomUIError, match="required glyph"):
        ui.raster_font("fake.ttf", 30)


def test_image_crop_resize_and_font_override(tmp_path):
    Image.new("RGB", (256, 200), "#123456").save(tmp_path / "bg.jpg")
    path = script(tmp_path, "BACKGROUND='bg.jpg'\nFONT='missing.ttf'\ndef draw(ui,state):\n    ui.image('background')\n    ui.note(state.note)")
    p = ui.parse_package(ui.compile_script(path, font=ui.default_font()))
    assert len(p.background) == 20480 and len(set(p.background)) == 1


def test_busy_palette_keeps_ui_colors_exact_and_background_indices_bounded(tmp_path):
    image = Image.new("RGB", (128, 160))
    image.putdata([((i * 37) % 256, (i * 73 + i // 128) % 256, (i * 109 + i // 17) % 256)
                   for i in range(128 * 160)])
    image.save(tmp_path / "busy.png")
    source = """BACKGROUND='busy.png'
IN_TUNE_COLOR='#01ff07'
OFF_TUNE_COLOR='#ff03f2'
def draw(ui,state):
    ui.image('background')
    ui.note(state.note,color=IN_TUNE_COLOR if state.in_tune else OFF_TUNE_COLOR)
    ui.rect(x=0,y=16,width=1,height=1,color='#fcffff')
"""
    p = ui.parse_package(ui.compile_script(script(tmp_path, source)))
    assert len(p.palette) <= 128 and max(p.background) < len(p.palette)
    assert p.palette[p.good] == ui.rgb565('#01ff07')
    assert p.palette[p.bad] == ui.rgb565('#ff03f2')
    assert ui.rgb565('#fcffff') in p.palette


@pytest.mark.parametrize("mutation", ["header_crc", "payload_crc", "reserved", "overlap", "bg_index", "glyph_padding", "missing_key", "op_reserved", "if_skip", "meter_range", "note_width"])
def test_rejects_malformed_packages(sample, mutation):
    b = bytearray(sample)
    bg = struct.unpack_from("<I", b, 32)[0]
    gd = struct.unpack_from("<I", b, 40)[0]
    masks = struct.unpack_from("<I", b, 48)[0]
    op = struct.unpack_from("<I", b, 56)[0]
    if mutation == "header_crc":
        b[64] ^= 1
    elif mutation == "payload_crc":
        b[-1] ^= 1
    elif mutation == "reserved":
        b[68] = 1
    elif mutation == "overlap":
        struct.pack_into("<I", b, 32, 128)
    elif mutation == "bg_index":
        b[bg] = 255
    elif mutation == "glyph_padding":
        for n in range(10):
            width = b[gd + n * 16 + 2]
            if width % 8:
                offset = struct.unpack_from("<I", b, gd + n * 16 + 8)[0]
                b[masks + offset + (width + 7) // 8 - 1] |= 1
                break
    elif mutation == "missing_key":
        b[gd] = ord("Z")
    elif mutation == "op_reserved":
        b[op + 16] = 1
    elif mutation == "if_skip":
        struct.pack_into("<i", b, op + 2 * 24 + 12, 20)
    elif mutation == "meter_range":
        struct.pack_into("<i", b, op + 12, 0)
    elif mutation == "note_width":
        struct.pack_into("<h", b, op + 24 + 8, 2)
    if mutation not in ("header_crc", "payload_crc"):
        b = fix_crc(b)
    with pytest.raises(ui.CustomUIError, match="invalid UI package"):
        ui.parse_package(bytes(b))


@pytest.mark.parametrize("style", [0, 1, 2])
def test_preview_three_meters_range_no_signal_and_conditional_color(sample, style):
    data = change_style(sample, style)
    p = ui.parse_package(data)
    good, bad = ui.rgb888(p.palette[p.good]), ui.rgb888(p.palette[p.bad])
    assert ui.render_preview(data, cents=100).tobytes() == ui.render_preview(data, cents=50).tobytes()
    assert ui.render_preview(data, cents=-100).tobytes() == ui.render_preview(data, cents=-50).tobytes()
    silent = ui.render_preview(data, signal_valid=False)
    assert silent.getpixel((64, 28)) == good
    assert silent.tobytes() == ui.render_preview(data, signal_valid=False, cents=-200).tobytes()
    tune = ui.render_preview(data, cents=0)
    assert tune.getpixel((48, 118)) == good
    off = ui.render_preview(data, cents=12)
    assert off.getpixel((48, 118)) != good
    assert bad in off.crop((8, 65, 120, 105)).get_flattened_data()
    assert good in tune.crop((8, 65, 120, 105)).get_flattened_data()
    assert ui.render_preview(data, note="Gb").tobytes() != ui.render_preview(data, note="G#").tobytes()


def status_bytes(state=0, result=0, session=0, received=0, expected=0, generation=8, enabled=1, synthetic=False):
    b = bytearray(32)
    b[:4] = bytes((1, 15, state, result))
    struct.pack_into("<IIIII", b, 4, ui.MAX_PACKAGE, session, received, expected, generation)
    b[24], b[25] = enabled, 1 if synthetic else int(state in (1, 3, 4, 5))
    struct.pack_into("<I", b, 28, received)
    return bytes(b)


class AsyncDevice:
    def __init__(self, *, lost_ack=False, fail=None, synthetic_busy=True, permanent_busy=False):
        self.state, self.session, self.received, self.expected = 0, 0, 0, 0
        self.generation, self.enabled = 8, 1
        self.result = 0
        self.pending = None
        self.requests = []
        self.lost_ack = lost_ack
        self.lost_once = set()
        self.synthetic_busy, self.busy_once = synthetic_busy, set()
        self.fail = fail
        self.permanent_busy = permanent_busy
        self.chunks = bytearray()
        self.commit_seen = False
        self.commit_poll_count = 0

    def response(self):
        return status_bytes(self.state, self.result, self.session, self.received,
                            self.expected, self.generation, self.enabled)

    def custom_ui_request(self, payload):
        self.requests.append(payload)
        op = payload[0]
        if self.permanent_busy or (self.synthetic_busy and payload not in self.busy_once):
            self.busy_once.add(payload)
            return status_bytes(result=2, synthetic=True)  # Explicit zero session/offsets.
        if op == 0:
            if self.pending:
                kind, value = self.pending
                if self.fail == kind:
                    self.state, self.result, self.pending = 6, 5, None
                    return self.response()
                if kind == "erase":
                    self.state, self.pending = 2, None
                elif kind == "write":
                    self.received += len(value)
                    self.chunks.extend(value)
                    self.state, self.pending = 2, None
                elif kind == "validate":
                    self.commit_poll_count += 1
                    self.state, self.pending = 5, ("commit", None)
                elif kind == "commit":
                    self.commit_poll_count += 1
                    self.generation += 1
                    self.state, self.pending = 0, None
                elif kind == "reset":
                    self.generation += 1
                    self.enabled = 0
                    self.state, self.pending = 0, None
            return self.response()
        session = struct.unpack_from("<I", payload, 1)[0]
        if op == 1 and session != self.session:
            self.session = session
            self.expected = struct.unpack_from("<I", payload, 5)[0]
            self.state, self.pending = 1, ("erase", None)
        elif op == 2:
            offset = struct.unpack_from("<I", payload, 5)[0]
            if self.state == 2 and offset == self.received:
                self.state, self.pending = 3, ("write", payload[9:])
            elif self.state == 3 and offset == self.received and self.pending[1] == payload[9:]:
                pass  # Lost acknowledgement retries the same queued chunk.
            elif offset + len(payload[9:]) == self.received:
                pass  # Last chunk already programmed.
            else:
                return status_bytes(self.state, 4, self.session, self.received, self.expected)
        elif op == 3 and not self.commit_seen:
            self.commit_seen = True
            self.state, self.pending = 4, ("validate", None)
        elif op == 4:
            self.state, self.pending = 0, None
            self.result = 0
        elif op == 5 and session != self.session:
            self.session, self.received, self.expected = session, 0, 0
            self.state, self.pending = 5, ("reset", None)
        if self.lost_ack and op in (1, 2, 3, 5) and payload not in self.lost_once:
            self.lost_once.add(payload)
            raise UpdateTimeoutError("lost acknowledgement after queueing")
        return self.response()


def test_async_upload_lost_ack_synthetic_busy_and_actual_completion(sample):
    device = AsyncDevice(lost_ack=True)
    states = []
    final = ui.Uploader(device, poll_seconds=0, progress=lambda s: states.append(s.state)).upload(sample)
    assert final.generation == 9 and final.enabled and final.received == len(sample)
    assert device.chunks == sample
    assert device.commit_poll_count == 2 and 5 in states
    assert any(p[0] == 2 and len(p) == 49 for p in device.requests)
    assert not any(p[0] == 4 for p in device.requests)


@pytest.mark.parametrize("phase", ["erase", "write", "validate", "commit"])
def test_device_failure_aborts_owned_session_preserving_old_ui(sample, phase):
    device = AsyncDevice(fail=phase)
    with pytest.raises(ui.CustomUIError, match="IO_ERROR"):
        ui.Uploader(device, poll_seconds=0).upload(sample)
    assert device.enabled and device.generation == 8
    assert device.requests[-1] == b"\x00"  # FAILED is already inactive; no abort needed.


def test_invalid_package_never_starts_device_mutation(sample):
    device = AsyncDevice()
    with pytest.raises(ui.CustomUIError):
        ui.Uploader(device).upload(sample[:-1])
    assert device.requests == []


def test_async_reset_waits_new_disabled_generation():
    device = AsyncDevice(lost_ack=True)
    final = ui.Uploader(device, poll_seconds=0).reset()
    assert final.generation == 9 and not final.enabled and final.state == 0
    assert final.expected == final.received == 0


def test_timeout_and_unsupported_old_firmware(sample):
    with pytest.raises(ui.CustomUIError, match="timed out"):
        ui.Uploader(AsyncDevice(permanent_busy=True), timeout=0.01, poll_seconds=0).upload(sample)
    class OldDevice:
        def custom_ui_request(self, payload):
            raise ProtocolError("device rejected CUSTOM_UI: BAD_COMMAND")
    with pytest.raises(ui.CustomUIError, match="upgrade firmware"):
        ui.Uploader(OldDevice(), poll_seconds=0).upload(sample)


def test_another_active_session_is_not_aborted(sample):
    device = AsyncDevice(synthetic_busy=False)
    device.state, device.session = 2, 99
    with pytest.raises(ui.CustomUIError, match="another"):
        ui.Uploader(device).upload(sample)
    assert device.requests == [b"\x00"]


def test_status_decoder_rejects_malformed_but_accepts_synthetic_busy():
    assert ui.UIStatus.decode(status_bytes(result=2, synthetic=True)).result == 2
    for offset, value in [(0, 2), (1, 0), (2, 7), (3, 8), (24, 2), (25, 2), (26, 1)]:
        b = bytearray(status_bytes())
        b[offset] = value
        with pytest.raises(ui.CustomUIError):
            ui.UIStatus.decode(b)
    with pytest.raises(ui.CustomUIError):
        ui.UIStatus.decode(status_bytes()[:-1])


def test_physical_flash_package_and_erase_progress_limits():
    assert ui.MAX_PACKAGE == 61440
    with pytest.raises(ui.CustomUIError, match="size"):
        ui.parse_package(b"\x00" * 61441)
    b = bytearray(status_bytes(state=1, session=123))
    struct.pack_into("<I", b, 28, 65536)
    assert ui.UIStatus.decode(b).progress == 65536
    struct.pack_into("<I", b, 28, 65537)
    with pytest.raises(ui.CustomUIError, match="bounds"):
        ui.UIStatus.decode(b)
    struct.pack_into("<I", b, 28, 0)
    struct.pack_into("<I", b, 4, 126976)
    with pytest.raises(ui.CustomUIError, match="bounds"):
        ui.UIStatus.decode(b)










def test_cli_build_preview_and_rejection(tmp_path):
    package, preview = tmp_path / "theme.pdui", tmp_path / "preview.png"
    assert ui.main(["build", str(ROOT / "examples" / "navy.py"), "--output", str(package)]) == 0
    assert ui.main(["preview", str(package), "--output", str(preview), "--note", "Gb", "--cents", "-12"]) == 0
    assert Image.open(preview).size == (128, 160)
    assert ui.main(["preview", str(package), "--output", str(preview), "--note", "Q"]) == 1


def test_gui_custom_ui_async_upload_reset_and_progress(sample, monkeypatch):
    gui = pytest.importorskip("skytuner_gui")
    import queue
    from types import SimpleNamespace
    class String:
        def __init__(self, value):
            self.value = value
        def get(self):
            return self.value
        def set(self, value):
            self.value = value
    class Device(AsyncDevice):
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
    device = Device()
    OriginalUploader = ui.Uploader
    monkeypatch.setattr(ui, "load_package", lambda path: sample)
    monkeypatch.setattr(ui, "Uploader", lambda dev, **kwargs: OriginalUploader(dev, poll_seconds=0, **kwargs))
    messages = queue.Queue()
    results = []
    remote = SimpleNamespace(serial=String("exact-unit"), custom_ui_path=String("theme.py"),
                             _messages=messages, _find_device=lambda serial: device,
                             _run_async=lambda label, operation: results.append(operation()))
    gui.SkyTunerRemote.upload_custom_ui(remote)
    assert "generation 9" in results[-1] and f"{len(sample)} bytes" in results[-1]
    assert messages.get()[0] == "progress"
    gui.SkyTunerRemote.reset_custom_ui(remote)
    assert "generation 10" in results[-1] and not device.enabled


def test_abort_retries_busy_and_lost_ack_on_its_own_cleanup_deadline():
    class CleanupDevice:
        def __init__(self):
            self.requests = []
            self.aborts = 0
            self.idle = False
        def custom_ui_request(self, payload):
            self.requests.append(payload)
            if payload[0] == 0:
                if len(self.requests) == 1:
                    return status_bytes(result=2, synthetic=True)
                return status_bytes(state=0 if self.idle else 2, session=123)
            self.aborts += 1
            if self.aborts == 1:
                return status_bytes(result=2, synthetic=True)
            self.idle = True
            raise UpdateTimeoutError("lost ABORT acknowledgement")
    device = CleanupDevice()
    uploader = ui.Uploader(device, poll_seconds=0)
    uploader.deadline = 0  # The upload deadline has already expired.
    uploader.abort(123)
    assert device.aborts == 2 and device.idle
    assert device.requests[-1] == b"\x00"
    assert uploader.deadline == 0


@pytest.mark.parametrize("state,session", [(0, 123), (6, 123), (2, 999)])
def test_abort_stops_when_inactive_or_owned_by_another_host(state, session):
    class OtherDevice:
        requests = []
        def custom_ui_request(self, payload):
            self.requests.append(payload)
            return status_bytes(state=state, session=session)
    device = OtherDevice()
    ui.Uploader(device, poll_seconds=0).abort(123)
    assert device.requests == [b"\x00"]


def test_abort_busy_is_bounded_without_aborting_unknown_session():
    device = AsyncDevice(permanent_busy=True)
    ui.Uploader(device, poll_seconds=0).abort(123, cleanup_timeout=0.025)
    assert device.requests and all(p == b"\x00" for p in device.requests)
