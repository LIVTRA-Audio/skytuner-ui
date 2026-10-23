# SkyTuner UI Toolkit 1.00

Create your own SkyTuner theme with Python. Customize backgrounds, note fonts,
meters, the top and bottom rows, and the startup animation.

## Get started

Install **Python 3.10 or newer**, then open a terminal in this folder.
On Windows:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe skytuner_gui.py
```

1. Connect **one SkyTuner** with a USB data cable. It is detected automatically.
2. Choose an example from `examples/`, such as `full_theme.py`.
3. Click **Preview**, then **Upload**.

The theme stays saved after restarting. **Restore Default** brings back the
original appearance. Use the latest SkyTuner firmware available in ToneCommand
if the app asks you to update.

On macOS/Linux, use `python3` to create the environment and
`.venv/bin/python` for the remaining commands. The desktop app also needs Tkinter.
Windows is the tested platform; USB permissions may need setup on other systems.

## Make it yours

| Example | What it shows |
| --- | --- |
| `examples/minimal.py` | Simple theme without image files |
| `examples/navy.py` | Dark background and custom note display |
| `examples/mosaic.py` | Image background |
| `examples/full_theme.py` | TUNE, poly, METER, header, footer and startup animation |
| `examples/separate_backgrounds.py` | Different images for different pages |

Copy an example and edit it in your text editor. Keep its images and fonts
beside the script. GAME and Settings retain their built-in appearance.

- [Drawing API](docs/drawing-api.md)
- [Page layouts, backgrounds and animation](docs/whole-ui.md)
- [Command-line usage and sharing themes](docs/usb-protocol.md)
- [Release notes](CHANGELOG.md)
