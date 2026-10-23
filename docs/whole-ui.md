# Page layouts and widgets

Use `examples/full_theme.py` as a starting point. All definitions below are
optional; omitted regions use the built-in layout. Use named scene functions
on their own, without a `draw(ui,state)` function in the same script.

| Function | Allowed box coordinates |
| --- | --- |
| `draw_header(ui,state)` | x=0–127, y=0–15 |
| `draw_footer(ui,state)` | x=0–127, y=144–159 |
| `draw_tune(ui,state)` | x=0–127, y=16–143 |
| `draw_poly(ui,state)` | x=0–127, y=16–143 |
| `draw_meter(ui,state)` | x=0–127, y=16–143 |
| `draw_boot(ui,state)` | x=0–127, y=0–159 |

Header and footer apply to TUNE, poly TUNE and METER. GAME and Settings keep
their built-in UI. Temporary status popups and update/recovery screens also
remain built in. Themes change drawing only, never detection, controls or audio.

## Drawing primitives and backgrounds

`ui.rect`, `ui.tuning_meter(state.cents,...)` and
`ui.text(state.note,font="custom_note_font",...)` use the behavior described
in the [drawing API](drawing-api.md), within the current scene's bounds.
`FONT`, `FONT_SIZE` and the palette constants are shared across the package.

Set `BACKGROUND` and call `ui.image("background",x=0,y=0)` at the start of
each customized scene to use one shared image. For independent page images,
use these settings:

```python
BACKGROUND_TUNE = "tune.png"
BACKGROUND_METER = "meter.png"
# Optional overrides:
# BACKGROUND_POLY = "poly.png"
# BACKGROUND_BOOT = "boot.png"
```

Paths are relative to the script. Set at least one page override to enable
automatic page backgrounds; no `ui.image` call is required in this mode.
TUNE, METER and boot fall back to `BACKGROUND`, or `CLEAR_COLOR` when absent.
POLY falls back to TUNE's image. Explicit `None` selects `CLEAR_COLOR` for that
page, even when a shared image is configured. Custom headers and footers follow
the current page's image. Omitted regions keep their built-in appearance.

Images are cropped/resized to 128×160 and quantized together into one shared
palette of at most 128 colors. Identical resized images are stored once.
Each distinct image uses 20 KiB, so the fixed 60 KiB package budget fits two
full images plus normal glyphs/drawings. Reuse images or clear colors on other
pages. An oversized package is rejected before upload. The uploader checks
whether the device supports page-specific backgrounds before installation.
See `examples/separate_backgrounds.py`.

## Labels and live values

```python
def draw_footer(ui, state):
    ui.text(state.path, x=2, y=146, width=40, height=7, color="#233c46")
    ui.text(state.mute, x=44, y=146, width=40, height=7, align="center")
    ui.text(state.audio, x=86, y=146, width=40, height=7, align="right")
```

Text may be a literal containing 1–31 printable ASCII characters or one of:

| Value | Meaning |
| --- | --- |
| `state.status` | Instrument, tuning and offset label |
| `state.a4` | Current A4 reference label |
| `state.path` | TRUE/BUFFER path label |
| `state.audio` | MON/BYP status |
| `state.mute` | MUTE when muted, otherwise empty |
| `state.view` | Runtime view label |
| `state.peak_db`, `state.average_db` | Current input meter values |
| `state.cents_text` | Whole cents value |
| `state.input` | INPUT or MON OFF |

Literal/live label text uses the built-in 5×7 font. `font="small"`, `"medium"`
and `"large"` select scale 1, 2 or 3. `x,y` are the box's top-left corner,
and `align="left"`, `"center"` or `"right"` aligns inside the box. Text is
truncated to fit. Height must fit seven rows times scale. Custom note text
uses its bitmap font and takes the horizontal center of its box as `x`,
as described in the [drawing API](drawing-api.md).

## Live widgets

All widget coordinates name the top-left corner. They restore their background
with the scene and read firmware values on every render.

```python
def draw_meter(ui, state):
    ui.level_meter(x=8, y=21, width=112, height=5)
    ui.waveform(x=8, y=35, width=112, height=36, color="#233c46")
    ui.spectrum(x=8, y=85, width=112, height=44)

def draw_poly(ui, state):
    ui.poly_strings(x=8, y=65, width=112, height=55, style="bars")
```

| Operation | Live behavior |
| --- | --- |
| `ui.level_meter(...)` | Input level and peak marker |
| `ui.waveform(...)` | Min/max envelope of the latest 112 waveform columns |
| `ui.spectrum(...)` | Sixteen live spectrum bars; width must be ≥16 |
| `ui.poly_strings(...,style="dots")` | String labels and fixed tuning indicators |
| `ui.poly_strings(...,style="bars")` | Per-string cents positions |
| `ui.page_dots(...)` | Three mode indicators; width must be ≥15 |
| `ui.progress(...)` | Boot progress from frame 0 to frame 23 |

All accept `x,y,width,height,color`. Height is at least 3. String labels are
shown when the widget is at least 16 pixels high. The shared foreground color
represents inactive strings; the shared in-tune color represents a detected
string within ±3 cents. `color` represents an off-tune string. METER readings
are available in METER; other pages do not turn on meter analysis themselves.

## Conditions and boot animation

Supported boolean conditions are `state.signal_valid`, `state.in_tune`,
`state.flat`, `state.sharp`, `state.muted`, `state.monitor`, `state.buffered`
and `state.input_available`. Use forward `if` blocks without `else`.

Boot additionally exposes frame 0–23. Equality and greater-than-or-equal
comparisons with a literal integer are supported:

```python
def draw_boot(ui, state):
    if state.boot_frame >= 2:
        ui.text("SKYTUNER", x=4, y=56, width=120, height=21,
                font="large", align="center")
    if state.boot_frame >= 10:
        ui.text("READY", x=8, y=94, width=112, height=7, align="center")
    ui.progress(x=16, y=123, width=96, height=4)
```

Use exact-frame conditions for discrete moving shapes. The firmware controls
the bounded frame count and timing. Themes cannot add waits, loops or arbitrary
code. Runtime scenes use boot_frame=0. A new boot theme takes effect on the next
device startup, while other scenes update after a successful upload.

## Preview and package limits

```powershell
.\.venv\Scripts\python.exe skytuner_ui.py preview examples\full_theme.py --scene tune --output output\tune.png
.\.venv\Scripts\python.exe skytuner_ui.py preview examples\full_theme.py --scene poly --output output\poly.png
.\.venv\Scripts\python.exe skytuner_ui.py preview examples\full_theme.py --scene meter --output output\meter.png
.\.venv\Scripts\python.exe skytuner_ui.py preview examples\full_theme.py --animate-boot --output output\boot.gif
```

The GUI has a Preview screen selector. Live readings in previews are simulated,
and small built-in text is illustrative; note masks use the compiled glyphs.
Omitted built-in regions are not simulated. Validate readability on the device.

The package is at most 60 KiB, with ≤128 total instructions and ≤2048
literal-text bytes. Each scene has its own work budget: 32768 pixels including
background, or 49152 for boot; note rows are bounded to 512 per scene.
All conditional branches count toward validation even when not drawn.

The uploader checks whether the device supports your theme before installation.
If an update is needed, use ToneCommand to install the latest SkyTuner firmware.
