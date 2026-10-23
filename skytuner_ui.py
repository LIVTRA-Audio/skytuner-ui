"""SkyTuner UI compiler, preview and USB HID uploader.

Source is parsed, never executed. See docs/drawing-api.md for the supported API.
"""
from __future__ import annotations

import argparse
import ast
from dataclasses import asdict, dataclass
import json
import math
from pathlib import Path
import secrets
import struct
import sys
import time
import zlib

from PIL import Image, ImageDraw, ImageFont, ImageOps
from skytuner_version import VERSION

MAX_PACKAGE = 61440
KEYS = "ABCDEFG#b-"
COLORS = {"BLACK": "#081220", "WHITE": "#dde9f1", "GREEN": "#63ff9b",
          "ORANGE": "#ffab45", "GRAY": "#40556a"}
STYLE = {"bar": 0, "needle": 1, "led_dots": 2}
CONDITION = {"signal_valid": 0, "in_tune": 1, "flat": 2, "sharp": 3}


class CustomUIError(ValueError):
    """Actionable authoring, package or device error."""


def rgb565(color):
    if isinstance(color, str):
        if len(color) != 7 or color[0] != "#":
            raise CustomUIError("colors must be #RRGGBB strings or RGB triples")
        try:
            color = tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))
        except ValueError as exc:
            raise CustomUIError("invalid hex color") from exc
    if not isinstance(color, (tuple, list)) or len(color) != 3 or any(
            type(c) is not int or not 0 <= c <= 255 for c in color):
        raise CustomUIError("colors must be #RRGGBB strings or RGB triples")
    r, g, b = color
    return ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)


def rgb888(c):
    r, g, b = c >> 11, (c >> 5) & 63, c & 31
    return (r * 255 // 31, g * 255 // 63, b * 255 // 31)


@dataclass(frozen=True)
class Glyph:
    key: str
    width: int
    height: int
    advance: int
    mask: bytes


@dataclass(frozen=True)
class Op:
    code: int
    color: int = 0
    arg: int = 0
    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0
    value: int = 0
    extra: int = 0
    extra2: int = 0

    def pack(self):
        return struct.pack("<BBBBhhhhiII", self.code, 0, self.color, self.arg,
                           self.x, self.y, self.width, self.height, self.value, self.extra, self.extra2)


@dataclass(frozen=True)
class Package:
    palette: tuple
    background: bytes
    glyphs: dict
    ops: tuple
    clear: int
    foreground: int
    good: int
    bad: int
    scenes: tuple = ()
    text: bytes = b""
    version: int = 1
    background_pages: tuple = ()


def parse_package(data: bytes) -> Package:
    """Validate a legacy or multi-scene package before any device mutation."""
    if len(data) >= 128 and data[:4] == b"PUI1" and data[4:6] in (b"\x02\x00", b"\x03\x00"):
        from skytuner_theme import parse_theme
        return parse_theme(data, api=sys.modules[__name__])
    def require(ok, message):
        if not ok:
            raise CustomUIError("invalid UI package: " + message)
    require(128 <= len(data) <= MAX_PACKAGE, "size")
    h = data[:128]
    u16 = lambda n: struct.unpack_from("<H", h, n)[0]
    u32 = lambda n: struct.unpack_from("<I", h, n)[0]
    require(h[:4] == b"PUI1" and u16(4) == 1 and u16(6) == 128, "format")
    require(u32(8) == len(data), "length")
    require(zlib.crc32(h[:124]) == u32(124), "header CRC")
    require(zlib.crc32(data[128:]) == u32(12), "payload CRC")
    require(u32(16) <= 3 and u16(20) == 128 and u16(22) == 160 and
            not any(h[68:124]), "canvas/flags/reserved bytes")
    count, ng, no = u16(28), u16(30), u16(60)
    require(1 <= count <= 128 and ng <= 16 and 1 <= no <= 64 and u16(62) == 24,
            "palette/glyph/instruction count")
    require(all(c < count for c in h[64:68]), "header colors")
    require(bool(u32(16) & 1) == bool(u32(36)) and u32(36) in (0, 20480), "background flag/size")
    require(bool(u32(16) & 2) == bool(ng) and u32(44) == ng * 16 and
            bool(u32(52)) == bool(ng), "font flag/size")
    sections = [(u32(24), count * 2), (u32(32), u32(36)),
                (u32(40), u32(44)), (u32(48), u32(52)), (u32(56), no * 24)]
    for i, (offset, length) in enumerate(sections):
        require((128 <= offset <= len(data) - length) if length else offset == 0, "section bounds")
        for other, size in sections[:i]:
            require(not (length and size and offset < other + size and other < offset + length),
                    "overlapping sections")
    palette = struct.unpack_from("<" + "H" * count, data, u32(24))
    bg = data[u32(32):u32(32) + u32(36)]
    require(not bg or max(bg) < count, "background palette index")
    glyphs, mask_ranges = {}, []
    for i in range(ng):
        b = data[u32(40) + 16 * i:u32(40) + 16 * (i + 1)]
        key, w, height, advance = chr(b[0]), b[2], b[3], b[4]
        offset, length = struct.unpack_from("<II", b, 8)
        require(key in KEYS and key not in glyphs and not b[1] and not any(b[5:8]), "glyph key/reserved bytes")
        stride = (w + 7) // 8
        require(1 <= w <= 64 and 1 <= height <= 80 and w <= advance <= 64 and
                length == stride * height and offset + length <= u32(52), "glyph metrics/mask bounds")
        require(all(not (offset < o + n and o < offset + length) for o, n in mask_ranges), "overlapping masks")
        mask_ranges.append((offset, length))
        mask = data[u32(48) + offset:u32(48) + offset + length]
        if w % 8:
            require(all(not mask[row * stride + stride - 1] & ((1 << (8 - w % 8)) - 1)
                        for row in range(height)), "glyph padding bits")
        glyphs[key] = Glyph(key, w, height, advance, mask)
    ops, pixels, rows = [], 16384, 0
    for i in range(no):
        b = data[u32(56) + 24 * i:u32(56) + 24 * (i + 1)]
        code, flags, color, arg, x, y, w, height, value, r1, r2 = struct.unpack("<BBBBhhhhiII", b)
        require(not (flags or r1 or r2) and color < count, "operation reserved bytes/color")
        if code == 4:
            require(color == 0 and arg <= 3 and not (x or y or w or height) and 1 <= value < no - i, "IF branch")
        else:
            require(x >= 0 and y >= 16 and w > 0 and height > 0 and x + w <= 128 and y + height <= 144,
                    "drawing box must fit x=0..127, y=16..143")
            if code == 1:
                require(not arg and not value, "RECT arguments")
            elif code == 2:
                require(arg <= 1 and not value and all(k in glyphs for k in KEYS), "NOTE font/arguments")
                require(all(g.width <= w and g.height <= height for g in glyphs.values()) and
                        all(glyphs[k].advance + glyphs[s].width <= w for k in "ABCDEFG" for s in "#b"),
                        "NOTE box too small for font or two-character note")
                rows += 2 * max(g.height for g in glyphs.values())
            elif code == 3:
                require(arg <= 2 and w >= 15 and height >= 3 and 1000 <= value <= 100000, "METER arguments")
            else:
                require(False, "unknown opcode")
            pixels += (max(w, 22) if code == 3 else w) * height
        ops.append(Op(code, color, arg, x, y, w, height, value))
    require(pixels <= 32768 and rows <= 512, "frame work budget exceeded")
    return Package(palette, bg, glyphs, tuple(ops), *h[64:68])


def default_font():
    candidates = ["C:/Windows/Fonts/segoeuib.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                  "/System/Library/Fonts/Supplemental/Arial Bold.ttf", "DejaVuSans-Bold.ttf"]
    for name in candidates:
        try:
            ImageFont.truetype(name, 12)
            return name
        except OSError:
            pass
    raise CustomUIError("no supported system font found; set FONT to a TTF/OTF path or use --font")


def raster_font(name, size):
    if type(size) is not int or not 1 <= size <= 80:
        raise CustomUIError("FONT_SIZE must be an integer from 1 to 80")
    try:
        font = ImageFont.truetype(name, size)
    except OSError as exc:
        raise CustomUIError(f"cannot load font {name}: {exc}") from exc
    boxes = {k: font.getbbox(k, anchor="ls") for k in KEYS}
    top = min(b[1] for b in boxes.values())
    height = max(b[3] for b in boxes.values()) - top
    glyphs = []
    missing = bytes(font.getmask("\uffff"))
    for k, (left, _, right, _) in boxes.items():
        width = right - left
        advance = max(width, math.ceil(font.getlength(k)))
        if bytes(font.getmask(k)) == missing:
            raise CustomUIError(f"font does not supply required glyph {k!r}")
        if not 1 <= width <= advance <= 64 or not 1 <= height <= 80:
            raise CustomUIError(f"font glyph {k!r} exceeds device metrics (64×80 maximum)")
        image = Image.new("1", (width, height))
        ImageDraw.Draw(image).text((-left, -top), k, font=font, anchor="ls", fill=1)
        glyphs.append(Glyph(k, width, height, advance, image.tobytes()))
    return glyphs


class Compiler:
    def __init__(self, path, font_override=None):
        self.path = Path(path)
        self.font_override = font_override
        self.globals = {}
        self.palette = []
        self.ops = []
        self.glyphs = []
        self.background_requested = False
        self.depth = 0
        self.bounds = (16, 144)

    def fail(self, node, message):
        raise CustomUIError(f"{self.path.name}:{getattr(node, 'lineno', 1)}: {message}")

    def literal(self, n):
        if isinstance(n, ast.Constant) and type(n.value) in (str, int, float, type(None)):
            return n.value
        if isinstance(n, ast.Name) and n.id in self.globals:
            return self.globals[n.id]
        if isinstance(n, (ast.Tuple, ast.List)):
            return tuple(self.literal(i) for i in n.elts)
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "ui" and n.attr in COLORS:
            return COLORS[n.attr]
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, (ast.USub, ast.UAdd)):
            value = self.literal(n.operand)
            if type(value) in (int, float):
                return -value if isinstance(n.op, ast.USub) else value
        self.fail(n, "expected a literal, configured constant, or ui color")

    def index(self, color):
        packed = rgb565(color)
        if packed not in self.palette:
            if len(self.palette) == 128:
                raise CustomUIError("too many colors")
            self.palette.append(packed)
        return self.palette.index(packed)

    def state(self, n, names):
        return isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "state" and n.attr in names

    def block(self, statements):
        for n in statements:
            if isinstance(n, ast.If):
                if n.orelse or not self.state(n.test, CONDITION):
                    self.fail(n, "if supports state.signal_valid/in_tune/flat/sharp without else")
                start = len(self.ops)
                self.ops.append(Op(4, arg=CONDITION[n.test.attr]))
                self.depth += 1
                self.block(n.body)
                self.depth -= 1
                skip = len(self.ops) - start - 1
                if not skip:
                    self.fail(n, "if must contain at least one drawing operation")
                self.ops[start] = Op(4, arg=CONDITION[n.test.attr], value=skip)
            elif isinstance(n, ast.Expr) and isinstance(n.value, ast.Call):
                self.call(n.value)
            else:
                self.fail(n, "only ui drawing calls and supported if blocks are allowed in draw")

    def call(self, n):
        if not isinstance(n.func, ast.Attribute) or not isinstance(n.func.value, ast.Name) or n.func.value.id != "ui":
            self.fail(n, "only supported ui methods are callable")
        method = n.func.attr
        if method not in ("image", "text", "note", "tuning_meter", "rect"):
            self.fail(n, f"unsupported ui method {method!r}")
        kwargs = {}
        for k in n.keywords:
            if k.arg is None or k.arg in kwargs:
                self.fail(n, "expanded/duplicate arguments are unsupported")
            kwargs[k.arg] = k.value
        args = list(n.args)
        value = None
        if method in ("image", "text", "note", "tuning_meter"):
            if args:
                value = args.pop(0)
            if "value" in kwargs:
                if value is not None or method != "tuning_meter":
                    self.fail(n, "value is only a meter keyword; do not supply it twice")
                value = kwargs.pop("value")
        if args:
            self.fail(n, "geometry must use named arguments")
        if method == "image":
            if self.depth or self.ops:
                self.fail(n, "background image must be unconditional and precede drawing operations")
            if value is None or self.literal(value) != "background" or self.background_requested:
                self.fail(n, "use ui.image('background', x=0, y=0) once")
            if any(k not in ("x", "y") for k in kwargs) or any(self.literal(v) != 0 for v in kwargs.values()):
                self.fail(n, "background image must be at x=0, y=0")
            # Background restore is unconditional, so conditional image calls would be misleading.
            self.background_requested = True
            return
        allowed = {"x", "y", "width", "height", "color"}
        if method in ("text", "note"):
            allowed |= {"font", "align"}
        if method == "tuning_meter":
            allowed |= {"style", "range_cents"}
        if set(kwargs) - allowed:
            self.fail(n, "unsupported arguments: " + ", ".join(sorted(set(kwargs) - allowed)))
        def get(key, default):
            return self.literal(kwargs[key]) if key in kwargs else default
        x, y = get("x", 64 if method in ("text", "note") else 8), get("y", 64)
        width, height = get("width", 112), get("height", max((g.height for g in self.glyphs), default=1) if method in ("text", "note") else 12)
        if any(type(v) is not int for v in (x, y, width, height)) or not all(-32768 <= v <= 32767 for v in (x, y, width, height)):
            self.fail(n, "geometry must be integer coordinates in device range")
        final_x = x - width // 2 if method in ("text", "note") else x
        if final_x < 0 or y < self.bounds[0] or width <= 0 or height <= 0 or final_x + width > 128 or y + height > self.bounds[1]:
            self.fail(n, "drawing box must fit x=0..127, y=16..143 after center alignment")
        color_node = kwargs.get("color")
        dynamic = False
        if isinstance(color_node, ast.IfExp):
            dynamic = self.state(color_node.test, ("in_tune",))
            if not dynamic or self.literal(color_node.body) != self.globals.get("IN_TUNE_COLOR", COLORS["GREEN"]) or self.literal(color_node.orelse) != self.globals.get("OFF_TUNE_COLOR", COLORS["ORANGE"]):
                self.fail(color_node, "conditional color must use IN_TUNE_COLOR/ui.GREEN if state.in_tune else OFF_TUNE_COLOR/ui.ORANGE")
            if method not in ("text", "note"):
                self.fail(n, "conditional color is supported only for the note")
        color = self.index(get("color", self.globals.get("OFF_TUNE_COLOR", COLORS["ORANGE"])) if not dynamic else self.globals.get("OFF_TUNE_COLOR", COLORS["ORANGE"]))
        if method in ("text", "note"):
            if value is None or not self.state(value, ("note", "note_name")):
                self.fail(n, "note must bind to state.note or state.note_name")
            if get("font", "custom_note_font") != "custom_note_font" or get("align", "center") != "center":
                self.fail(n, "note uses font='custom_note_font', align='center'")
            self.ops.append(Op(2, color, int(dynamic), final_x, y, width, height))
        elif method == "rect":
            self.ops.append(Op(1, color, 0, x, y, width, height))
        else:
            if value is None or not self.state(value, ("cents",)):
                self.fail(n, "meter must bind to state.cents")
            style, half_range = get("style", "bar"), get("range_cents", 50)
            if style not in STYLE or type(half_range) not in (float, int) or not math.isfinite(half_range) or not 1 <= half_range <= 100:
                self.fail(n, "meter style must be bar/needle/led_dots; range_cents must be 1..100")
            self.ops.append(Op(3, color, STYLE[style], x, y, width, height, round(half_range * 1000)))

    def compile(self):
        try:
            tree = ast.parse(self.path.read_text(encoding="utf-8-sig"), filename=str(self.path))
        except SyntaxError as exc:
            raise CustomUIError(f"{self.path.name}:{exc.lineno}: {exc.msg}") from exc
        functions = []
        for n in tree.body:
            if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
                key = n.targets[0].id
                if key in self.globals or key in ("draw", "ui", "state"):
                    self.fail(n, "duplicate or reserved configuration name")
                self.globals[key] = self.literal(n.value)
            elif isinstance(n, ast.FunctionDef) and n.name == "draw":
                functions.append(n)
            elif isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str):
                pass  # Module docstring only; it is never evaluated.
            else:
                self.fail(n, "top level supports literal configuration and one def draw(ui, state)")
        if len(functions) != 1:
            raise CustomUIError("provide exactly one def draw(ui, state)")
        fn = functions[0]
        if fn.decorator_list or fn.returns or fn.type_comment or fn.args.posonlyargs or fn.args.kwonlyargs or fn.args.vararg or fn.args.kwarg or fn.args.defaults or [a.arg for a in fn.args.args] != ["ui", "state"] or any(a.annotation for a in fn.args.args):
            self.fail(fn, "draw must have exactly two unannotated parameters: ui, state")
        def asset(name):
            path = Path(name)
            return str(path if path.is_absolute() else self.path.parent / path)
        font = self.font_override or self.globals.get("FONT")
        self.glyphs = raster_font(str(self.font_override) if self.font_override else asset(font) if font else default_font(), self.globals.get("FONT_SIZE", 44))
        indexes = [self.index(self.globals.get(k, COLORS[c])) for k, c in
                   (("CLEAR_COLOR", "BLACK"), ("FOREGROUND_COLOR", "GRAY"),
                    ("IN_TUNE_COLOR", "GREEN"), ("OFF_TUNE_COLOR", "ORANGE"))]
        self.block(fn.body)
        bg = b""
        if self.background_requested:
            name = self.globals.get("BACKGROUND")
            if not isinstance(name, str):
                raise CustomUIError("BACKGROUND must specify an image path")
            try:
                with Image.open(asset(name)) as source:
                    image = ImageOps.fit(ImageOps.exif_transpose(source).convert("RGB"), (128, 160), method=Image.Resampling.LANCZOS)
            except (OSError, ValueError, Image.DecompressionBombError) as exc:
                raise CustomUIError(f"cannot read BACKGROUND image: {exc}") from exc
            remaining = 128 - len(self.palette)
            if remaining < 1:
                raise CustomUIError("drawing colors leave no background palette space")
            quantized = image.quantize(colors=remaining, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
            colors = quantized.getpalette()
            lookup = {i: self.index(tuple(colors[i * 3:i * 3 + 3])) for i in set(quantized.tobytes())}
            bg = bytes(lookup[i] for i in quantized.tobytes())
        parts, offsets, cursor = [], [], 128
        masks, descriptors = bytearray(), bytearray()
        for g in self.glyphs:
            descriptors.extend(struct.pack("<BBBBB3xII", ord(g.key), 0, g.width, g.height, g.advance, len(masks), len(g.mask)))
            masks.extend(g.mask)
        for part in (struct.pack("<" + "H" * len(self.palette), *self.palette), bg, bytes(descriptors), bytes(masks), b"".join(o.pack() for o in self.ops)):
            offsets.append(cursor if part else 0)
            parts.append(part)
            cursor += len(part)
        payload = b"".join(parts)
        h = bytearray(128)
        struct.pack_into("<4sHHIIIHHIHHIIIIIIIHH", h, 0, b"PUI1", 1, 128, cursor,
                         zlib.crc32(payload), 2 | bool(bg), 128, 160, offsets[0], len(self.palette), len(self.glyphs),
                         offsets[1], len(bg), offsets[2], len(descriptors), offsets[3], len(masks), offsets[4], len(self.ops), 24)
        h[64:68] = bytes(indexes)
        struct.pack_into("<I", h, 124, zlib.crc32(h[:124]))
        data = bytes(h) + payload
        parse_package(data)
        return data


def compile_script(path, *, font=None) -> bytes:
    try:
        tree = ast.parse(Path(path).read_text(encoding="utf-8-sig"), filename=str(path))
    except SyntaxError as exc:
        raise CustomUIError(f"{Path(path).name}:{exc.lineno}: {exc.msg}") from exc
    if any(isinstance(n, ast.FunctionDef) and n.name.startswith("draw_") for n in tree.body):
        from skytuner_theme import compile_theme
        return compile_theme(path, font=font, api=sys.modules[__name__])
    if any(isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id in
           ("BACKGROUND_TUNE", "BACKGROUND_POLY", "BACKGROUND_METER", "BACKGROUND_BOOT") for t in n.targets)
           for n in tree.body):
        raise CustomUIError("per-page backgrounds require draw_tune/draw_poly/draw_meter/draw_boot scene functions")
    return Compiler(path, font).compile()


def load_package(path, *, font=None):
    path = Path(path)
    data = compile_script(path, font=font) if path.suffix.lower() == ".py" else path.read_bytes()
    parse_package(data)
    return data


def render_preview(data, note="E", cents=12, signal_valid=True, in_tune=None, *, scene="tune", frame=12):
    """Render validated package using firmware pixel/rounding semantics."""
    p = parse_package(data)
    if p.version >= 2:
        from skytuner_theme import render_theme
        return render_theme(p, note=note, cents=cents, signal_valid=signal_valid,
                            in_tune=in_tune, scene=scene, frame=frame, api=sys.modules[__name__])
    if note not in [k + suffix for k in "ABCDEFG" for suffix in ("", "#", "b")]:
        raise CustomUIError("preview note must be A..G with optional # or b")
    if type(cents) not in (float, int) or not math.isfinite(cents) or abs(cents) > 1000000:
        raise CustomUIError("preview cents must be finite and within ±1000000")
    milli = round(cents * 1000)
    good_state = signal_valid and (abs(cents) <= 3 if in_tune is None else bool(in_tune))
    image = Image.new("RGB", (128, 160), rgb888(p.palette[p.clear]))
    if p.background:
        background = Image.new("RGB", (128, 160))
        background.putdata([rgb888(p.palette[c]) for c in p.background])
        image.paste(background.crop((0, 16, 128, 144)), (0, 16))
    draw = ImageDraw.Draw(image)
    def rect(x, y, w, h, color):
        if w > 0 and h > 0:
            draw.rectangle((x, y, x + w - 1, y + h - 1), fill=rgb888(p.palette[color]))
    i = 0
    while i < len(p.ops):
        o = p.ops[i]
        if o.code == 4:
            condition = (signal_valid, good_state, signal_valid and not good_state and milli < 0,
                         signal_valid and not good_state and milli > 0)[o.arg]
            if not condition:
                i += o.value
        elif o.code == 1:
            rect(o.x, o.y, o.width, o.height, o.color)
        elif o.code == 2:
            text = note if signal_valid else "-"
            glyphs = [p.glyphs[k] for k in text]
            width = glyphs[0].width if len(glyphs) == 1 else glyphs[0].advance + glyphs[1].width
            x = o.x + (o.width - width) // 2
            color = p.good if good_state else p.bad
            if not o.arg:
                color = o.color
            for g in glyphs:
                mask = Image.frombytes("1", (g.width, g.height), g.mask)
                image.paste(rgb888(p.palette[color]), (x, o.y, x + g.width, o.y + g.height), mask)
                x += g.advance
        else:
            center = o.x + o.width // 2
            value = min(o.value, max(-o.value, milli))
            # C integer division truncates toward zero, including negative values.
            pos = center + math.trunc(value * (o.width // 2 - 1) / o.value)
            color = p.good if good_state else o.color
            rect(center, o.y, 1, o.height, p.good)
            if signal_valid:
                if o.arg == 0:
                    if good_state:
                        rect(center - 1, o.y, 3, o.height, p.good)
                    elif pos < center:
                        rect(pos, o.y, center - pos, o.height, color)
                    else:
                        rect(center + 1, o.y, pos - center, o.height, color)
                elif o.arg == 1:
                    rect(pos, o.y, 1, o.height, color)
                else:
                    index = min(6, (pos - o.x) * 6 // (o.width - 1))
                    step = (o.width - 3) // 6
                    for j in range(7):
                        rect(o.x + j * step, o.y, 3, o.height, color if j == index else p.foreground)
        i += 1
    # Firmware-owned regions are illustrative; central content is pixel accurate.
    draw.rectangle((0, 0, 127, 15), fill="#101820")
    draw.rectangle((0, 144, 127, 159), fill="#101820")
    draw.text((4, 2), "TUNE          A4 440", fill="#dce8ef", font=ImageFont.load_default(size=9))
    draw.text((4, 147), "GUITAR 6     STD", fill="#dce8ef", font=ImageFont.load_default(size=9))
    return image


@dataclass(frozen=True)
class UIStatus:
    state: int
    result: int
    maximum: int
    session: int
    received: int
    expected: int
    generation: int
    enabled: bool
    busy: bool
    progress: int

    @classmethod
    def decode(cls, data):
        if len(data) != 32 or data[:2] != b"\x01\x0f" or data[2] > 6 or data[3] > 7 or data[24] > 1 or data[25] > 1 or any(data[26:28]):
            raise CustomUIError("invalid custom UI status response")
        maximum, session, received, expected, generation = struct.unpack_from("<IIIII", data, 4)
        progress = struct.unpack_from("<I", data, 28)[0]
        if maximum != MAX_PACKAGE or expected > maximum or received > expected or progress > 65536:
            raise CustomUIError("invalid custom UI status bounds")
        # Synthetic BUSY has state=idle and zero session/offsets; this is legal.
        if data[3] != 2 and bool(data[25]) != (data[2] in (1, 3, 4, 5)):
            raise CustomUIError("inconsistent custom UI worker state")
        return cls(data[2], data[3], maximum, session, received, expected,
                   generation, bool(data[24]), bool(data[25]), progress)


class Uploader:
    """Session-owned asynchronous upload; completion requires durable STATUS."""
    ERRORS = {1: "BAD_REQUEST", 3: "BAD_SESSION", 4: "BAD_OFFSET", 5: "IO_ERROR", 6: "BAD_PACKAGE", 7: "BAD_CRC"}

    def __init__(self, trigger, *, timeout=180, poll_seconds=0.015, progress=None):
        if not math.isfinite(timeout) or timeout <= 0 or not math.isfinite(poll_seconds) or poll_seconds < 0:
            raise CustomUIError("timeout must be positive; poll delay must be nonnegative")
        self.trigger, self.timeout, self.delay, self.progress = trigger, timeout, poll_seconds, progress
        self.deadline = None

    def check_deadline(self):
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise CustomUIError("timed out waiting for custom UI; upload completion was not confirmed")

    def request(self, payload):
        from skytuner_hid.errors import ProtocolError, UpdateTimeoutError
        self.check_deadline()
        for attempt in range(3):
            try:
                return UIStatus.decode(self.trigger.custom_ui_request(payload))
            except (ProtocolError, UpdateTimeoutError, TimeoutError) as exc:
                if "BAD_COMMAND" in str(exc):
                    raise CustomUIError("firmware does not support custom UI; upgrade firmware first") from exc
                if isinstance(exc, ProtocolError) and not any(s in str(exc) for s in ("HID read failed", "HID write failed", "short HID", "BUSY")):
                    raise CustomUIError(str(exc)) from exc
                self.check_deadline()
                if attempt == 2:
                    raise CustomUIError(f"custom UI HID transport failed: {exc}") from exc
                time.sleep(self.delay)

    def status(self):
        self.deadline = time.monotonic() + self.timeout
        while True:
            status = self.request(b"\x00")
            if status.result != 2:
                return status
            time.sleep(self.delay)

    def mutation(self, payload, session):
        while True:
            s = self.request(payload)
            # A synthetic cache-contention BUSY intentionally has no session.
            if s.result == 2:
                time.sleep(self.delay)
                continue  # Resend exactly the same idempotent mutation.
            self.ensure(s, session)
            return s

    def ensure(self, s, session):
        if s.result or s.state == 6:
            raise CustomUIError("device custom UI error: " + self.ERRORS.get(s.result, "FAILED"))
        if s.session != session:
            raise CustomUIError("custom UI session changed; another host may be uploading")

    def wait(self, session, ready):
        while True:
            s = self.request(b"\x00")
            if s.result == 2:
                time.sleep(self.delay)
                continue
            self.ensure(s, session)
            if self.progress:
                self.progress(s)
            if ready(s):
                return s
            time.sleep(self.delay)

    def abort(self, session, cleanup_timeout=2.0):
        """Best-effort session-owned cleanup with an independent retry budget.

        STATUS establishes ownership before each idempotent ABORT. A lost
        acknowledgement is recovered by STATUS; cache BUSY may have no session.
        The retry deadline is checked between serialized HID transactions.
        """
        deadline = time.monotonic() + cleanup_timeout
        operation_deadline = self.deadline
        self.deadline = deadline
        try:
            while time.monotonic() < deadline:
                try:
                    s = UIStatus.decode(self.trigger.custom_ui_request(b"\x00"))
                    if s.result == 2:
                        time.sleep(max(self.delay, 0.01))
                        continue
                    if s.session != session or s.state in (0, 6):
                        return
                    if time.monotonic() >= deadline:
                        return
                    s = UIStatus.decode(self.trigger.custom_ui_request(struct.pack("<BI", 4, session)))
                    if s.result != 2 and (s.session != session or s.state in (0, 6)):
                        return
                except Exception:
                    # Preserve the original upload/cancellation error. A retry
                    # starts with STATUS so it cannot cancel another host.
                    pass
                time.sleep(max(self.delay, 0.01))
        finally:
            self.deadline = operation_deadline

    def upload(self, data):
        package = parse_package(data)
        if package.version >= 2:
            capability_error = "This firmware needs an upgrade for " + (
                "per-page backgrounds" if package.version == 3 else "whole-UI themes")
            # Read-only preflight before BEGIN can erase the inactive slot.
            deadline = time.monotonic() + min(self.timeout, 3)
            while True:
                try:
                    caps = self.trigger.custom_ui_request(bytes((7 if package.version == 3 else 6,)))
                except Exception as exc:
                    raise CustomUIError(capability_error) from exc
                if (len(caps) == 32 and caps[:6] == (b"PUI3\x03\x06" if package.version == 3 else b"PUI2\x02\x06")
                        and struct.unpack_from("<I", caps, 8)[0] == MAX_PACKAGE
                        and not any(caps[6:8] + caps[12:])):
                    break
                if len(caps) != 32 or caps[:2] != b"\x01\x0f" or caps[3] != 2 or time.monotonic() >= deadline:
                    raise CustomUIError(capability_error)
                time.sleep(self.delay)  # Must happen before BEGIN can erase the inactive slot.
        before = self.status()
        if before.state in (1, 2, 3, 4, 5):
            raise CustomUIError("another custom UI upload is active")
        session = secrets.randbelow(0xffffffff) + 1
        self.deadline = time.monotonic() + self.timeout
        try:
            self.mutation(struct.pack("<BIII", 1, session, len(data), zlib.crc32(data)), session)
            s = self.wait(session, lambda s: s.state == 2 and not s.busy)
            offset = 0
            while offset < len(data):
                chunk = data[offset:offset + 40]
                self.mutation(struct.pack("<BII", 2, session, offset) + chunk, session)
                s = self.wait(session, lambda s: s.state == 2 and not s.busy)
                if s.received != offset + len(chunk) or s.expected != len(data):
                    raise CustomUIError("device reported an unexpected programmed offset")
                offset = s.received
            self.mutation(struct.pack("<BI", 3, session), session)
            return self.wait(session, lambda s: s.state == 0 and not s.busy and s.enabled
                             and s.generation == ((before.generation + 1) & 0xffffffff)
                             and s.received == len(data) and s.expected == len(data))
        except BaseException:
            self.abort(session)
            raise

    def reset(self):
        before = self.status()
        if before.state in (1, 2, 3, 4, 5):
            raise CustomUIError("another custom UI upload is active")
        session = secrets.randbelow(0xffffffff) + 1
        self.deadline = time.monotonic() + self.timeout
        try:
            self.mutation(struct.pack("<BI", 5, session), session)
            return self.wait(session, lambda s: s.state == 0 and not s.busy and not s.enabled
                             and s.generation == ((before.generation + 1) & 0xffffffff))
        except BaseException:
            self.abort(session)
            raise


def list_devices(product_id=0x4001, hid_module=None):
    """List matching application interfaces without opening or modifying them."""
    if hid_module is None:
        import hid as hid_module
    from skytuner_hid.hid_trigger import (
        DEFAULT_VENDOR_ID, HID_USAGE_PAGE, HID_USAGE,
        HID_INTERFACE_NUMBER, HID_LEGACY_INTERFACE_NUMBER,
        HID_INTERFACE_UNAVAILABLE,
    )
    return [
        {"name": "SkyTuner", "serial": entry.get("serial_number") or "",
         "usb_product": entry.get("product_string") or "",
         "vid": f"0x{DEFAULT_VENDOR_ID:04x}", "pid": f"0x{product_id:04x}"}
        for entry in hid_module.enumerate(DEFAULT_VENDOR_ID, product_id)
        if entry.get("vendor_id") == DEFAULT_VENDOR_ID
        and entry.get("product_id") == product_id
        and entry.get("usage_page") == HID_USAGE_PAGE
        and entry.get("usage") == HID_USAGE
        and entry.get("interface_number") in (
            HID_INTERFACE_NUMBER, HID_LEGACY_INTERFACE_NUMBER,
            HID_INTERFACE_UNAVAILABLE)
    ]


def main(argv=None):
    parser = argparse.ArgumentParser(prog="skytuner-ui", description=__doc__)
    parser.add_argument("--version", action="version", version=f"SkyTuner UI Toolkit {VERSION}")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("devices", "build", "preview", "upload", "status", "reset"):
        sub = commands.add_parser(name)
        if name in ("build", "preview", "upload"):
            sub.add_argument("input", type=Path)
            sub.add_argument("--font", help="override TTF/OTF font path")
        if name in ("build", "preview"):
            sub.add_argument("--output", type=Path, required=True)
        if name == "preview":
            sub.add_argument("--note", default="E")
            sub.add_argument("--cents", type=float, default=12)
            sub.add_argument("--no-signal", action="store_true")
            sub.add_argument("--scene", choices=("tune", "poly", "meter", "boot"), default="tune")
            sub.add_argument("--frame", type=int, choices=range(24), default=12)
            sub.add_argument("--animate-boot", action="store_true", help="save all 24 boot frames as a GIF")
        if name in ("upload", "status", "reset"):
            sub.add_argument("--serial", help="optional USB device ID; auto-detects a single connected SkyTuner")
            sub.add_argument("--timeout", type=float, default=180)
        if name in ("devices", "upload", "status", "reset"):
            sub.add_argument("--pid", type=lambda x: int(x, 0), default=0x4001)
    args = parser.parse_args(argv)
    try:
        if args.command == "devices":
            print(json.dumps(list_devices(args.pid), indent=2))
            return 0
        data = load_package(args.input, font=args.font) if hasattr(args, "input") else None
        if args.command == "build":
            args.output.write_bytes(data)
            print(f"Built {args.output}: {len(data)} bytes, CRC32 {zlib.crc32(data):08x}")
        elif args.command == "preview":
            if args.animate_boot:
                if args.output.suffix.lower() != ".gif":
                    raise CustomUIError("animated boot preview needs a .gif output")
                frames = [render_preview(data, args.note, args.cents, not args.no_signal, scene="boot", frame=f) for f in range(24)]
                frames[0].save(args.output, save_all=True, append_images=frames[1:], duration=50, loop=0)
            else:
                render_preview(data, args.note, args.cents, not args.no_signal, scene=args.scene, frame=args.frame).save(args.output)
            print(f"Preview saved: {args.output}")
        else:
            from skytuner_hid.hid_trigger import HidTrigger
            from skytuner_hid.errors import SkyTunerError
            last_progress = [-1]
            def progress(s):
                percent = s.received * 100 // max(s.expected, 1)
                if args.command == "upload" and percent // 10 != last_progress[0]:
                    print(f"Upload {percent}% ({s.received}/{s.expected} programmed bytes)")
                    last_progress[0] = percent // 10
            try:
                with HidTrigger.find(serial_number=args.serial, product_id=args.pid) as trigger:
                    uploader = Uploader(trigger, timeout=args.timeout, progress=progress)
                    status = uploader.upload(data) if args.command == "upload" else uploader.reset() if args.command == "reset" else uploader.status()
                    print(json.dumps(asdict(status), indent=2))
            except SkyTunerError as exc:
                raise CustomUIError(str(exc)) from exc
        return 0
    except (CustomUIError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Cancelled; uncommitted upload aborted when possible.", file=sys.stderr)
        return 130
    except ImportError as exc:
        print(f"Missing dependency: {exc}; run python -m pip install -r requirements.txt", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
