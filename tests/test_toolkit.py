from types import SimpleNamespace
from contextlib import nullcontext
from dataclasses import dataclass

import pytest
import skytuner_ui as ui
from skytuner_version import VERSION


def test_devices_lists_only_normal_vendor_hid_interfaces():
    valid = dict(vendor_id=0x301A, product_id=0x4001, usage_page=0xFF00,
                 usage=1, interface_number=0, serial_number="test-unit",
                 product_string="SkyTuner")
    entries = [valid, dict(valid, interface_number=2, serial_number="bench-unit"),
               dict(valid, interface_number=-1, serial_number="portable-unit"),
               dict(valid, product_id=0x2800), dict(valid, usage_page=1),
               dict(valid, vendor_id=0xFFFF), dict(valid, interface_number=5)]
    calls = []

    def enumerate_devices(vid, pid):
        calls.append((vid, pid))
        return entries

    result = ui.list_devices(hid_module=SimpleNamespace(enumerate=enumerate_devices))
    assert calls == [(0x301A, 0x4001)]
    assert [device["serial"] for device in result] == [
        "test-unit", "bench-unit", "portable-unit"]
    assert all(device["name"] == "SkyTuner" for device in result)


def test_devices_cli_outputs_serials_without_opening_device(monkeypatch, capsys):
    monkeypatch.setattr(ui, "list_devices", lambda pid: [{"serial": "test-unit"}])
    assert ui.main(["devices"]) == 0
    assert '"test-unit"' in capsys.readouterr().out


def test_version_cli_exits_without_device_access(monkeypatch, capsys):
    monkeypatch.setattr(ui, "list_devices", lambda *args: pytest.fail("unexpected device access"))
    with pytest.raises(SystemExit) as result:
        ui.main(["--version"])
    assert result.value.code == 0
    assert capsys.readouterr().out.strip() == f"SkyTuner UI Toolkit {VERSION}"


@pytest.mark.parametrize("command", ["upload", "status", "reset"])
@pytest.mark.parametrize("serial", [None, "chosen-unit"])
def test_theme_commands_allow_auto_detection_and_explicit_usb_id(monkeypatch, command, serial):
    from skytuner_hid.hid_trigger import HidTrigger

    @dataclass
    class Status:
        enabled: bool = True

    trigger = object()
    selections = []
    actions = []

    def find(**kwargs):
        selections.append(kwargs)
        return nullcontext(trigger)

    class Uploader:
        def __init__(self, selected, **kwargs):
            assert selected is trigger

        def upload(self, data):
            assert data == b"compiled-theme"
            actions.append("upload")
            return Status()

        def status(self):
            actions.append("status")
            return Status()

        def reset(self):
            actions.append("reset")
            return Status()

    monkeypatch.setattr(HidTrigger, "find", find)
    monkeypatch.setattr(ui, "Uploader", Uploader)
    monkeypatch.setattr(ui, "load_package", lambda *args, **kwargs: b"compiled-theme")
    args = [command] + (["theme.py"] if command == "upload" else [])
    if serial is not None:
        args += ["--serial", serial]
    assert ui.main(args) == 0
    assert selections == [{"serial_number": serial, "product_id": 0x4001}]
    assert actions == [command]
