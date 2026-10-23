# Command line and theme sharing

Run these commands from the toolkit folder after installing the dependencies.
Examples use Windows paths; on macOS/Linux use `.venv/bin/python` instead.
Connect one SkyTuner to let the uploader detect it automatically.

## Preview and upload

```powershell
New-Item -ItemType Directory -Force output | Out-Null
.\.venv\Scripts\python.exe skytuner_ui.py preview examples\full_theme.py --output output\tune.png
.\.venv\Scripts\python.exe skytuner_ui.py upload examples\full_theme.py
```

Use `--scene poly`, `--scene meter` or `--scene boot` to preview another screen.
For an animated startup preview:

```powershell
.\.venv\Scripts\python.exe skytuner_ui.py preview examples\full_theme.py --animate-boot --output output\boot.gif
```

## Share one theme file

```powershell
.\.venv\Scripts\python.exe skytuner_ui.py build examples\full_theme.py --output output\my-theme.pdui
.\.venv\Scripts\python.exe skytuner_ui.py upload output\my-theme.pdui
```

The `.pdui` file includes the theme's images, note glyphs and drawings.
Share it with another user of this toolkit. When sharing Python source instead,
include its referenced images and fonts too.

## Restore the original appearance

```powershell
.\.venv\Scripts\python.exe skytuner_ui.py reset
```

## Optional device selection

Normally no device ID is needed. If several SkyTuners are connected, disconnect
the others, or use `devices` and select one with `--serial`:

```powershell
.\.venv\Scripts\python.exe skytuner_ui.py devices
.\.venv\Scripts\python.exe skytuner_ui.py upload examples\full_theme.py --serial YOUR_USB_ID
```

This ID is reported over USB; it does not need to be printed on the device.
If the devices have the same ID, connect only one at a time.

## Device requirements

Python scripts are compiled on your computer. The device displays the compiled
theme. Upload uses normal USB HID with the device powered on as usual.
The uploader checks theme compatibility and reports errors before installation.
If a firmware update is required, install the latest SkyTuner update with ToneCommand.

Keep the USB cable connected until upload finishes. The previous theme remains
active if an upload is interrupted; reconnect and upload again.
