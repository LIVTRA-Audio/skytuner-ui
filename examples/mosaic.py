"""User-provided mosaic background, compiled and uploaded with the Python tool."""
BACKGROUND = "mosaic-background.png"
FONT = None
FONT_SIZE = 44
CLEAR_COLOR = "#081220"
FOREGROUND_COLOR = "#30495f"
IN_TUNE_COLOR = "#63ff9b"
OFF_TUNE_COLOR = "#ffab45"
METER_STYLE = "led_dots"


def draw(ui, state):
    ui.image("background", x=0, y=0)
    ui.tuning_meter(value=state.cents, style=METER_STYLE,
                    x=8, y=28, width=112, height=10, range_cents=50)
    ui.text(state.note, x=64, y=65, width=112, height=40,
            font="custom_note_font", align="center",
            color=IN_TUNE_COLOR if state.in_tune else OFF_TUNE_COLOR)
    if state.in_tune:
        ui.rect(x=48, y=118, width=32, height=2, color=IN_TUNE_COLOR)
