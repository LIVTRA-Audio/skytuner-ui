from io import StringIO

from skytuner_hid import (
    Button,
    DeviceInfo,
    DeviceNotFoundError,
    Framebuffer,
    LogChunk,
)
from skytuner_hid.cli import main


class FakeDevice:
    def __init__(self, chunks=()):
        self.chunks = list(chunks)
        self.cursors = []
        self.updated = False
        self.buttons = []
        self.framebuffer = Framebuffer(
            9, 2, 1, (0x0000, 0xFFFF), b"\x00\x01"
        )
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def get_info(self):
        return DeviceInfo("SkyTuner", "V05", "0100000000", 1)

    def read_log(self, cursor):
        self.cursors.append(cursor)
        return self.chunks.pop(0)

    def enter_update(self):
        self.updated = True

    def set_button(self, button, pressed):
        self.buttons.append((button, pressed))

    def capture_framebuffer(self):
        return self.framebuffer


class FlakyReleaseDevice(FakeDevice):
    def __init__(self):
        super().__init__()
        self.release_failures = 1

    def set_button(self, button, pressed):
        super().set_button(button, pressed)
        if not pressed and self.release_failures:
            self.release_failures -= 1
            raise RuntimeError("temporary HID release failure")


class LostInitialPressDevice(FakeDevice):
    def __init__(self):
        super().__init__()
        self.press_failures = 1

    def set_button(self, button, pressed):
        super().set_button(button, pressed)
        if pressed and self.press_failures:
            self.press_failures -= 1
            raise OSError("lost HID press status stage")


def _run(argv, device):
    stdout = StringIO()
    stderr = StringIO()
    captured = {}

    def finder(**kwargs):
        captured.update(kwargs)
        return device

    code = main(
        argv, finder=finder, stdout=stdout, stderr=stderr,
        sleep=lambda seconds: None,
    )
    return code, stdout.getvalue(), stderr.getvalue(), captured


def test_cli_info_and_global_selection():
    device = FakeDevice()
    code, out, err, args = _run(
        [
            "--vid", "0x301a", "--pid", "0x4000",
            "--serial", "ABC", "--timeout-ms", "250", "info",
        ],
        device,
    )
    assert code == 0 and not err
    assert out == "SkyTuner V05 firmware 0100000000 protocol 1\n"
    assert args["serial_number"] == "ABC"
    assert args["timeout_ms"] == 250
    assert device.closed


def test_cli_logs_drains_history_and_warns_on_gap():
    device = FakeDevice(
        [
            LogChunk(2, 3, 4, "first", False),
            LogChunk(5, 6, 6, "after gap", True),
        ]
    )
    code, out, err, _ = _run(["logs"], device)
    assert code == 0
    assert out == "first\nafter gap\n"
    assert "RAM log gap" in err
    assert device.cursors == [None, 3]


def test_cli_continues_with_zero_after_cursor_wrap():
    device = FakeDevice(
        [
            LogChunk(0xFFFFFFFE, 0, 1, "last", False),
            LogChunk(0, 1, 1, "zero", False),
        ]
    )
    code, out, err, _ = _run(["logs"], device)
    assert code == 0 and not err
    assert out == "last\nzero\n"
    assert device.cursors == [None, 0]


def test_cli_from_now_discards_history():
    device = FakeDevice([LogChunk(10, 10, 10, "", False)])
    code, out, err, _ = _run(["logs", "--from-now"], device)
    assert code == 0 and not out and not err
    assert device.cursors == [0xFFFFFFFF]


def test_cli_follow_stops_cleanly_on_ctrl_c():
    device = FakeDevice([LogChunk(4, 4, 4, "", False)])
    stdout = StringIO()
    stderr = StringIO()

    def interrupt(_seconds):
        raise KeyboardInterrupt

    code = main(
        ["logs", "--follow"],
        finder=lambda **kwargs: device,
        stdout=stdout,
        stderr=stderr,
        sleep=interrupt,
    )
    assert code == 0
    assert device.closed


def test_cli_enter_update_reports_ack():
    device = FakeDevice()
    settle_observations = []
    stdout = StringIO()
    stderr = StringIO()

    code = main(
        ["enter-update"],
        finder=lambda **kwargs: device,
        stdout=stdout,
        stderr=stderr,
        sleep=lambda seconds: settle_observations.append(
            (seconds, device.updated, device.closed)
        ),
    )
    out = stdout.getvalue()
    err = stderr.getvalue()
    assert code == 0 and device.updated and not err
    assert "MSC upgrade mode" in out
    assert settle_observations == [(0.5, True, False)]
    assert device.closed


def test_cli_button_tap_and_combo_long_use_press_release_pairs():
    device = FakeDevice()
    code, out, err, _ = _run(
        ["button", "display", "tap", "--hold-ms", "75"], device
    )
    assert code == 0 and not err
    assert device.buttons == [
        (Button.DISPLAY, True), (Button.DISPLAY, False)
    ]
    assert "75 ms" in out

    device = FakeDevice()
    code, out, err, _ = _run(
        ["button", "combo", "long"], device
    )
    assert code == 0 and not err
    assert device.buttons == [
        (Button.COMBO, True),
        (Button.COMBO, True),
        (Button.COMBO, True),
        (Button.COMBO, True),
        (Button.COMBO, True),
        (Button.COMBO, True),
        (Button.COMBO, False),
    ]
    assert "900 ms" in out


def test_cli_button_retries_idempotent_release():
    device = FlakyReleaseDevice()
    code, out, err, _ = _run(
        ["button", "display", "tap", "--hold-ms", "75"], device
    )
    assert code == 0 and not err
    assert "75 ms" in out
    assert device.buttons == [
        (Button.DISPLAY, True),
        (Button.DISPLAY, False),
        (Button.DISPLAY, False),
    ]


def test_cli_button_releases_after_initial_press_write_fails():
    device = LostInitialPressDevice()
    code, out, err, _ = _run(
        ["button", "display", "tap", "--hold-ms", "75"], device
    )
    assert code == 2 and not out
    assert "lost HID press status stage" in err
    assert device.buttons == [
        (Button.DISPLAY, True),
        (Button.DISPLAY, False),
    ]


def test_cli_button_press_reports_failsafe():
    device = FakeDevice()
    code, out, err, _ = _run(
        ["button", "tuning", "press"], device
    )
    assert code == 0 and not err
    assert device.buttons == [(Button.TUNING, True)]
    assert "failsafe" in out


def test_cli_screenshot_writes_png(tmp_path):
    output = tmp_path / "screen.png"
    code, out, err, _ = _run(["screenshot", str(output)], FakeDevice())
    assert code == 0 and not err
    assert output.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert "2x1 framebuffer capture 9" in out


def test_cli_expected_error_is_clear_and_nonzero():
    stdout = StringIO()
    stderr = StringIO()

    def missing(**kwargs):
        raise DeviceNotFoundError(
            "normal-mode HID absent; close other SkyTuner software"
        )

    assert main(
        ["info"], finder=missing, stdout=stdout, stderr=stderr
    ) == 2
    assert "close other SkyTuner software" in stderr.getvalue()
