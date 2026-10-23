# Drawing API

Use Python drawing functions to customize TUNE, poly TUNE, METER, the header
and footer, and the startup animation. This guide covers the drawing primitives,
colors, images and fonts. See [page layouts and widgets](whole-ui.md) for scene
coordinates, live labels, waveform/spectrum widgets and animation.

Write a small Python drawing script on your computer, preview it, and upload
the compiled package through normal USB HID. The package includes its background
image, bitmap note glyphs and drawing instructions. Once installed it runs on
the pedal without the computer connected and survives restart. The pedal does
not execute Python or MicroPython. The compiler parses source without executing
it; imports, arbitrary function calls, loops and filesystem/audio/device access
are not part of the drawing API.

The display is 128×160 pixels. Each drawing function owns a region of the
screen; `draw_tune`, for example, covers x=0–127 and y=16–143. Drawing boxes
must stay within their scene. Background pixels are restored on each redraw,
so moving meters and note changes do not leave trails. GAME, Settings,
temporary popups and update/recovery screens use their built-in appearance.

For installation and complete commands, see the [README](../README.md).

## Drawing script

See [full_theme.py](../examples/full_theme.py). Top-level assignments accept literal
strings, numbers, `None`, RGB tuples and references to previously defined
constants. Negative numbers and built-in colors such as `ui.GREEN` are accepted.
Define the scene functions you want to customize, each with the parameters
`ui, state`. Omitted scenes use the built-in layout. A TUNE scene can use the
following calls and forward `if` statements:

```python
BACKGROUND_TUNE = "background.png"
FONT = None
FONT_SIZE = 44
METER_STYLE = "needle"

def draw_tune(ui, state):
    ui.tuning_meter(state.cents, style=METER_STYLE,
                    x=8, y=28, width=112, height=10, range_cents=50)
    ui.text(state.note, x=64, y=65, width=112, height=40,
            font="custom_note_font", align="center",
            color=ui.GREEN if state.in_tune else ui.ORANGE)
    if state.in_tune:
        ui.rect(x=48, y=118, width=32, height=2, color=ui.GREEN)
```

| API | Behavior |
| --- | --- |
| `ui.image("background", x=0, y=0)` | Restore the shared `BACKGROUND` image. Use before other drawing calls in a scene. Page-specific backgrounds such as `BACKGROUND_TUNE` are applied automatically. |
| `ui.text(state.note, ...)` | Draw the live note, including optional `#`/`b`; `state.note_name` and `ui.note` are aliases. |
| `ui.tuning_meter(state.cents, ...)` | Draw a live bar, needle or seven LED dots; `value=state.cents` is also accepted. |
| `ui.rect(x=..., y=..., width=..., height=..., color=...)` | Fill a literal rectangle. |
| `if state.signal_valid/in_tune/flat/sharp:` | Draw the contained operations if true. Nesting is allowed. No `else`, loops, local assignments or arbitrary expressions. See the [scene guide](whole-ui.md#conditions-and-boot-animation) for additional scene conditions. |

RECT and METER x/y name the top-left corner. NOTE x names the horizontal center
of its bounding box: `x=64,width=112` compiles to x=8. NOTE y names the top of a
common glyph box, not the text baseline. All ten glyphs share the same baseline
within that box; their horizontal bearings are cropped into their masks.
Every two-character note must fit its box. The default NOTE width is 112 and
height is the generated font box height. `font` must be `custom_note_font`, and
only centered note alignment is supported.

`style` supports `bar`, `needle`, and `led_dots`. Meter `range_cents` is its
positive half-range from 1 through 100; ±50 is the default. Values beyond that
range clamp to an end. A meter needs width ≥15 and height ≥3. Its green center
marker remains visible without a signal. The meter receives firmware cents and
in-tune state directly; scripts do not change pitch detection or smoothing.

Optional top-level `CLEAR_COLOR`, `FOREGROUND_COLOR`, `IN_TUNE_COLOR` and
`OFF_TUNE_COLOR` set the shared palette roles. Accept `"#RRGGBB"`, `(r,g,b)`, or
`ui.BLACK/WHITE/GREEN/ORANGE/GRAY`. Default foreground is the inactive LED color.
NOTE accepts a fixed color or `IN_TUNE_COLOR if state.in_tune else
OFF_TUNE_COLOR` (the default `ui.GREEN`/`ui.ORANGE` pair is equivalent). Meter
color sets its off-tune indicator; in-tune and center use `IN_TUNE_COLOR`.
Flat/sharp conditions require a valid signal outside the in-tune threshold.

## Single-view themes

For a theme that customizes only single-note TUNE, define `draw(ui, state)` as
shown in [minimal.py](../examples/minimal.py) and [navy.py](../examples/navy.py).
The header, footer and other pages keep their built-in appearance. This function
supports the note, tuning meter and rectangle operations, plus the four signal
conditions listed above. To use an image, set `BACKGROUND` and call
`ui.image("background", x=0, y=0)` before other drawing calls.

Use either `draw(ui, state)` or named scene functions in one script. For
page-specific backgrounds, live labels, widgets or boot animation, use the
named scene functions described in [page layouts and widgets](whole-ui.md).

## Images and fonts

PNG/JPEG and other Pillow-readable images are center-cropped and scaled to
128×160 with EXIF orientation respected. Use a subdued image for contrast.
The compiler quantizes the background after reserving drawing colors in a
shared palette of at most 128 RGB565 entries. It embeds the pixels, not a path.

`FONT` points to a TTF/OTF file relative to the script. `FONT=None` searches
Segoe UI Bold on Windows, DejaVu Sans Bold on Linux, and Arial Bold on macOS.
`--font` overrides the script using a path relative to the current directory.
The original font file is not included. Generated A–G, `#`, `b` and `-` masks
are monochrome; colors are applied at drawing time. Default `FONT_SIZE` is 44.
Check that your font license allows embedding the resulting glyphs before
distributing a theme. The navy background is generated by the included
`examples/generate_background.py`. The mosaic example uses the supplied
`examples/mosaic-background.png`. No font binary is bundled.

The compiler rejects unsupported Python with source line numbers, missing
assets/glyphs, invalid geometry, overlapping sections, CRC errors and excessive
frame work. Font glyphs are at most 64×80 pixels. Packages are at most
61440 bytes (60 KiB). Multi-scene themes allow up to 128 instructions and
2048 literal-text bytes; single-view themes allow up to 64 instructions.
Background restore plus all drawing boxes, including conditional branches,
must fit each scene's work budget: 32768 pixels, or 49152 for boot. Note
glyph-row reads are limited to 512 per scene. Keep layouts small and clear.

## Upload and restore

Connect one SkyTuner, then run:

```powershell
.\.venv\Scripts\python.exe skytuner_ui.py upload examples\navy.py
.\.venv\Scripts\python.exe skytuner_ui.py reset
```

The uploader checks your theme and reports progress. Keep USB connected until
it finishes. Your theme stays saved after restarting. If an upload is interrupted,
the previously installed theme stays active; reconnect and try again.

See [command-line usage](usb-protocol.md) for previews and sharing a theme.
