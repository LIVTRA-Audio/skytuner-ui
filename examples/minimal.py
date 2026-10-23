"""A light theme with no external image or font file required."""
FONT = None
FONT_SIZE = 44
CLEAR_COLOR = "#ffffff"
FOREGROUND_COLOR = "#64748b"
IN_TUNE_COLOR = "#17804b"
OFF_TUNE_COLOR = "#d85c20"


def draw(ui, state):
    ui.tuning_meter(state.cents, style="bar",
                    x=8, y=28, width=112, height=10, range_cents=50)
    ui.text(state.note, x=64, y=65, width=112, height=40,
            font="custom_note_font", align="center",
            color=IN_TUNE_COLOR if state.in_tune else OFF_TUNE_COLOR)
