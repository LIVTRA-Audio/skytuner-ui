"""One theme for all tuner screens. GAME and Settings remain built in."""
FONT = None
FONT_SIZE = 44
CLEAR_COLOR = "#f7f4eb"
FOREGROUND_COLOR = "#c5c9c5"
IN_TUNE_COLOR = "#248a61"
OFF_TUNE_COLOR = "#e26d39"
INK = "#233c46"


def draw_header(ui, state):
    ui.text(state.status, x=2, y=3, width=54, height=7, color=INK)
    ui.page_dots(x=56, y=5, width=16, height=3, color=INK)
    ui.text(state.a4, x=94, y=3, width=32, height=7, align="right", color=INK)


def draw_footer(ui, state):
    ui.text(state.path, x=2, y=146, width=40, height=7, color=INK)
    ui.text(state.mute, x=44, y=146, width=40, height=7, align="center", color=INK)
    ui.text(state.audio, x=86, y=146, width=40, height=7, align="right", color=INK)


def draw_tune(ui, state):
    ui.tuning_meter(state.cents, x=8, y=30, width=112, height=8, style="needle")
    ui.text(state.note, x=64, y=65, width=112, height=40,
            font="custom_note_font", align="center",
            color=IN_TUNE_COLOR if state.in_tune else OFF_TUNE_COLOR)
    ui.text(state.cents_text, x=40, y=118, width=48, height=14,
            font="medium", align="center", color=INK)


def draw_poly(ui, state):
    ui.text("STRINGS", x=10, y=32, width=108, height=14,
            font="medium", align="center", color=INK)
    ui.poly_strings(x=8, y=65, width=112, height=55, style="bars")


def draw_meter(ui, state):
    ui.level_meter(x=8, y=21, width=112, height=5)
    ui.waveform(x=8, y=35, width=112, height=36, color=INK)
    ui.spectrum(x=8, y=85, width=112, height=44)
    ui.text(state.peak_db, x=8, y=133, width=48, height=7, color=INK)
    ui.text(state.input, x=58, y=133, width=62, height=7, align="right", color=INK)


def draw_boot(ui, state):
    if state.boot_frame >= 2:
        ui.text("SKYTUNER", x=4, y=56, width=120, height=21,
                font="large", align="center", color=INK)
    if state.boot_frame >= 10:
        ui.text("READY TO PLAY", x=8, y=94, width=112, height=7,
                align="center", color=INK)
    ui.progress(x=16, y=123, width=96, height=4)
