"""Whole-screen theme format v2; shares assets and the v1 upload transaction."""
import ast
import math
from pathlib import Path
import struct
import zlib

from PIL import Image, ImageDraw, ImageFont, ImageOps

SCENES = ("header", "footer", "tune", "poly", "meter", "boot")
BACKGROUND_PAGES = ("tune", "poly", "meter", "boot")
FIELDS = ("literal", "status", "a4", "path", "audio", "mute", "view",
          "peak_db", "average_db", "cents_text", "input")
WIDGETS = ("level_meter", "waveform", "spectrum", "poly_strings", "page_dots", "progress")
CONDITIONS = ("signal_valid", "in_tune", "flat", "sharp", "frame_eq", "frame_ge",
              "muted", "monitor", "buffered", "input_available")


def bounds(scene):
    return (0, 16) if scene == 0 else (144, 160) if scene == 1 else (0, 160) if scene == 5 else (16, 144)


def parse_theme(data, *, api):
    def need(ok, message):
        if not ok:
            raise api.CustomUIError("invalid UI package: " + message)
    need(128 <= len(data) <= api.MAX_PACKAGE, "size")
    h = data[:128]
    u16 = lambda n: struct.unpack_from("<H", h, n)[0]
    u32 = lambda n: struct.unpack_from("<I", h, n)[0]
    version = u16(4)
    need(h[:4] == b"PUI1" and version in (2, 3) and u16(6) == 128, "format")
    need(u32(8) == len(data) and zlib.crc32(h[:124]) == u32(124), "header CRC/length")
    need(zlib.crc32(data[128:]) == u32(12), "payload CRC")
    need(u32(16) <= 3 and u16(20) == 128 and u16(22) == 160 and
         not any(h[92:100]) and (version == 3 or not any(h[108:124])), "canvas/flags/reserved bytes")
    count, ng, no = u16(28), u16(30), u16(60)
    need(1 <= count <= 128 and ng <= 16 and 1 <= no <= 128 and u16(62) == 24, "counts")
    need(all(c < count for c in h[64:68]), "colors")
    background_length = u32(36)
    need(bool(u32(16) & 1) == bool(background_length), "background flag")
    if version == 3:
        pages = struct.unpack_from("<IIII", h, 108)
        images = background_length // 20480
        need(background_length % 20480 == 0 and all(i <= images for i in pages)
             and set(pages) - {0} == set(range(1, images + 1)), "page background references")
    else:
        pages = ()
        need(background_length in (0, 20480), "background length")
    need(bool(u32(16) & 2) == bool(ng) and u32(44) == ng * 16 and bool(u32(52)) == bool(ng), "font")
    scenes = tuple(struct.unpack_from("<HH", h, 68 + i * 4) for i in range(6))
    covered = 0
    for first, size in scenes:
        need((first == covered and size <= no - covered) if size else first == 0, "scene coverage")
        covered += size
    need(covered == no and u32(104) <= 2048, "scene/text bounds")
    sections = [(u32(24), count * 2), (u32(32), u32(36)), (u32(40), u32(44)),
                (u32(48), u32(52)), (u32(56), no * 24), (u32(100), u32(104))]
    for i, (offset, size) in enumerate(sections):
        need((128 <= offset <= len(data) - size) if size else offset == 0, "section bounds")
        need(all(not (size and n and offset < o + n and o < offset + size) for o, n in sections[:i]), "overlap")
    palette = struct.unpack_from("<" + "H" * count, data, u32(24))
    background = data[u32(32):u32(32) + u32(36)]
    need(not background or max(background) < count, "background palette")
    text = data[u32(100):u32(100) + u32(104)]
    need(all(32 <= c <= 126 for c in text), "text must be printable ASCII")
    glyphs, ranges = {}, []
    for i in range(ng):
        b = data[u32(40) + i * 16:u32(40) + (i + 1) * 16]
        key, w, height, advance = chr(b[0]), b[2], b[3], b[4]
        offset, size = struct.unpack_from("<II", b, 8)
        stride = (w + 7) // 8
        need(key in api.KEYS and key not in glyphs and not b[1] and not any(b[5:8]), "glyph key")
        need(1 <= w <= advance <= 64 and 1 <= height <= 80 and size == stride * height
             and offset + size <= u32(52), "glyph bounds")
        need(all(not (offset < o + n and o < offset + size) for o, n in ranges), "glyph overlap")
        ranges.append((offset, size))
        mask = data[u32(48) + offset:u32(48) + offset + size]
        need(not w % 8 or all(not mask[row * stride + stride - 1] & ((1 << (8 - w % 8)) - 1)
                             for row in range(height)), "glyph padding")
        glyphs[key] = api.Glyph(key, w, height, advance, mask)
    ops = []
    for scene, (first, size) in enumerate(scenes):
        top, bottom = bounds(scene)
        pixels, rows = 128 * (bottom - top), 0
        for index in range(first, first + size):
            code, flags, color, arg, x, y, w, height, value, extra, extra2 = struct.unpack_from(
                "<BBBBhhhhiII", data, u32(56) + index * 24)
            need(not flags and color < count, "operation flags/color")
            if code == 4:
                need(color == 0 and arg <= 9 and not (x or y or w or height) and not extra2
                     and 1 <= value < first + size - index and
                     (extra <= 23 if arg in (4, 5) else extra == 0), "IF scene bounds/condition")
            else:
                need(x >= 0 and y >= top and w > 0 and height > 0 and x + w <= 128 and y + height <= bottom,
                     f"drawing box escapes {SCENES[scene]}")
                if code == 5:
                    scale, align = value & 255, value >> 8
                    need(arg <= 10 and 1 <= scale <= 3 and 0 <= align <= 2 and w >= 5 * scale and height >= 7 * scale,
                         "TEXT geometry/font")
                    need((extra == extra2 == 0) if arg else 1 <= extra2 <= 31 and extra + extra2 <= len(text), "TEXT reference")
                else:
                    need(not extra and not extra2, "operation reserved bytes")
                    if code == 1:
                        need(not arg and not value, "RECT arguments")
                    elif code == 2:
                        need(arg <= 1 and not value and all(k in glyphs for k in api.KEYS), "NOTE font")
                        need(all(g.width <= w and g.height <= height for g in glyphs.values()) and
                             all(glyphs[k].advance + glyphs[s].width <= w for k in "ABCDEFG" for s in "#b"), "NOTE box")
                        rows += 2 * max(g.height for g in glyphs.values())
                    elif code == 3:
                        need(arg <= 2 and w >= 15 and height >= 3 and 1000 <= value <= 100000, "METER arguments")
                    elif code == 6:
                        need(arg <= 5 and value in (0, 1) and w >= (16 if arg == 2 else 15 if arg == 4 else 1)
                             and height >= 3, "WIDGET arguments")
                    else:
                        need(False, "unknown opcode")
                pixels += (max(w, 22) if code == 3 else w) * height
            ops.append(api.Op(code, color, arg, x, y, w, height, value, extra, extra2))
        need(pixels <= (49152 if scene == 5 else 32768) and rows <= 512, "scene work budget")
    return api.Package(palette, background, glyphs, tuple(ops), *h[64:68], scenes, text, version, pages)


def compile_theme(path, *, font=None, api):
    class ThemeCompiler(api.Compiler):
        def block(self, statements):
            for node in statements:
                if isinstance(node, ast.If):
                    extra = 0
                    if self.state(node.test, CONDITIONS[:4] + CONDITIONS[6:]):
                        condition = CONDITIONS.index(node.test.attr)
                    elif (isinstance(node.test, ast.Compare) and self.state(node.test.left, ("boot_frame",))
                          and len(node.test.ops) == len(node.test.comparators) == 1
                          and isinstance(node.test.ops[0], (ast.Eq, ast.GtE))):
                        condition = 4 if isinstance(node.test.ops[0], ast.Eq) else 5
                        extra = self.literal(node.test.comparators[0])
                        if type(extra) is not int or not 0 <= extra <= 23:
                            self.fail(node, "boot_frame comparison must be an integer 0..23")
                    else:
                        self.fail(node, "unsupported live condition")
                    if node.orelse:
                        self.fail(node, "else is not supported; use another forward condition")
                    start = len(self.ops)
                    self.ops.append(api.Op(4))
                    self.depth += 1
                    self.block(node.body)
                    self.depth -= 1
                    skip = len(self.ops) - start - 1
                    if not skip:
                        self.fail(node, "empty conditional")
                    self.ops[start] = api.Op(4, arg=condition, value=skip, extra=extra)
                else:
                    super().block([node])

        def call(self, node):
            if not isinstance(node.func, ast.Attribute) or not isinstance(node.func.value, ast.Name) or node.func.value.id != "ui":
                self.fail(node, "only ui drawing methods are supported")
            method = node.func.attr
            if method == "image":
                if self.depth or len(self.ops) != self.scene_start or len(node.args) != 1 or self.literal(node.args[0]) != "background":
                    self.fail(node, "background must precede drawing operations in its scene")
                if any(k.arg not in ("x", "y") or self.literal(k.value) != 0 for k in node.keywords):
                    self.fail(node, "background image uses x=0,y=0")
                self.background_requested = True
                return
            value = node.args[0] if len(node.args) == 1 else None
            if method not in WIDGETS and not (method == "text" and value is not None and not self.state(value, ("note", "note_name"))):
                return super().call(node)
            kwargs = {}
            for k in node.keywords:
                if k.arg is None or k.arg in kwargs:
                    self.fail(node, "expanded/duplicate arguments are unsupported")
                kwargs[k.arg] = self.literal(k.value)
            allowed = {"x", "y", "width", "height", "color", "font", "align"} if method == "text" else {"x", "y", "width", "height", "color", "style"}
            if set(kwargs) - allowed or len(node.args) != (1 if method == "text" else 0):
                self.fail(node, "unsupported drawing arguments")
            x, y, w, height = (kwargs.get(k, default) for k, default in
                               (("x", 0), ("y", self.bounds[0]), ("width", 128), ("height", 7 if method == "text" else 12)))
            if any(type(v) is not int for v in (x, y, w, height)) or x < 0 or y < self.bounds[0] or w <= 0 or height <= 0 or x + w > 128 or y + height > self.bounds[1]:
                self.fail(node, "drawing box escapes its scene")
            color = self.index(kwargs.get("color", self.globals.get("OFF_TUNE_COLOR", api.COLORS["ORANGE"])))
            if method == "text":
                extra = extra2 = 0
                if self.state(value, FIELDS[1:]):
                    arg = FIELDS.index(value.attr)
                else:
                    text = self.literal(value)
                    if not isinstance(text, str) or not 1 <= len(text) <= 31 or not all(32 <= ord(c) <= 126 for c in text):
                        self.fail(node, "text must be 1..31 printable ASCII characters or a supported live field")
                    arg, extra, extra2 = 0, len(self.text_pool), len(text)
                    self.text_pool.extend(text.encode("ascii"))
                fonts = {"small": 1, "medium": 2, "large": 3}
                aligns = {"left": 0, "center": 1, "right": 2}
                if kwargs.get("font", "small") not in fonts or kwargs.get("align", "left") not in aligns:
                    self.fail(node, "text font is small/medium/large; alignment is left/center/right")
                scale = fonts[kwargs.get("font", "small")]
                if w < 5 * scale or height < 7 * scale:
                    self.fail(node, "TEXT box is too small for font")
                self.ops.append(api.Op(5, color, arg, x, y, w, height,
                                       scale | aligns[kwargs.get("align", "left")] << 8, extra, extra2))
            else:
                style = kwargs.get("style", "dots")
                if "style" in kwargs and (method != "poly_strings" or style not in ("dots", "bars")):
                    self.fail(node, "only poly_strings accepts style=dots/bars")
                self.ops.append(api.Op(6, color, WIDGETS.index(method), x, y, w, height,
                                       int(method == "poly_strings" and style == "bars")))

    c = ThemeCompiler(path, font)
    c.text_pool = bytearray()
    tree = ast.parse(Path(path).read_text(encoding="utf-8-sig"), filename=str(path))
    functions = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            key = node.targets[0].id
            if key in c.globals or key in ("ui", "state", "draw") or key.startswith("draw_"):
                c.fail(node, "duplicate or reserved configuration")
            c.globals[key] = c.literal(node.value)
        elif isinstance(node, ast.FunctionDef) and node.name in {"draw_" + s for s in SCENES}:
            if node.name in functions:
                c.fail(node, "duplicate scene function")
            functions[node.name] = node
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            pass
        else:
            c.fail(node, "theme supports configuration and draw_header/footer/tune/poly/meter/boot")
    def asset(name):
        p = Path(name)
        return p if p.is_absolute() else Path(path).parent / p
    name = font or c.globals.get("FONT")
    c.glyphs = api.raster_font(str(font) if font else str(asset(name)) if name else api.default_font(), c.globals.get("FONT_SIZE", 44))
    indexes = [c.index(c.globals.get(k, api.COLORS[color])) for k, color in
               (("CLEAR_COLOR", "BLACK"), ("FOREGROUND_COLOR", "GRAY"), ("IN_TUNE_COLOR", "GREEN"), ("OFF_TUNE_COLOR", "ORANGE"))]
    scenes = []
    for i, scene in enumerate(SCENES):
        fn = functions.get("draw_" + scene)
        c.bounds, c.scene_start = bounds(i), len(c.ops)
        if fn:
            a = fn.args
            if fn.decorator_list or fn.returns or fn.type_comment or a.posonlyargs or a.kwonlyargs or a.vararg or a.kwarg or a.defaults or [x.arg for x in a.args] != ["ui", "state"] or any(x.annotation for x in a.args):
                c.fail(fn, "scene must have exactly two unannotated parameters: ui,state")
            c.block(fn.body)
            if len(c.ops) == c.scene_start:
                c.fail(fn, "scene needs at least one drawing operation")
        scenes.append((c.scene_start, len(c.ops) - c.scene_start) if fn else (0, 0))
    background, pages = b"", ()
    version = 3 if any("BACKGROUND_" + page.upper() in c.globals for page in BACKGROUND_PAGES) else 2
    if version == 3:
        default = c.globals.get("BACKGROUND")
        tune = c.globals.get("BACKGROUND_TUNE", default)
        names = [tune, c.globals.get("BACKGROUND_POLY", tune),
                 c.globals.get("BACKGROUND_METER", default), c.globals.get("BACKGROUND_BOOT", default)]
        sources, page_indexes, pixels = [], [], []
        for name in names:
            if name is None:
                page_indexes.append(0)
                continue
            if not isinstance(name, str) or not name:
                raise api.CustomUIError("BACKGROUND and BACKGROUND_TUNE/POLY/METER/BOOT must be image paths or None")
            try:
                with Image.open(asset(name)) as source:
                    image = ImageOps.fit(ImageOps.exif_transpose(source).convert("RGB"), (128, 160), method=Image.Resampling.LANCZOS)
            except (OSError, ValueError, Image.DecompressionBombError) as exc:
                raise api.CustomUIError(f"cannot read background image {name!r}: {exc}") from exc
            raw = image.tobytes()
            if raw not in pixels:
                pixels.append(raw)
                sources.append(image)
            page_indexes.append(pixels.index(raw) + 1)
        pages = tuple(page_indexes)
        if sources:
            # Quantize all pages together so their indices share the bounded palette.
            remaining = 128 - len(c.palette)
            if remaining < 1:
                raise api.CustomUIError("no background palette space")
            atlas = Image.new("RGB", (128, 160 * len(sources)))
            for i, image in enumerate(sources):
                atlas.paste(image, (0, 160 * i))
            quantized = atlas.quantize(colors=remaining, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
            colors = quantized.getpalette()
            lookup = {i: c.index(tuple(colors[i * 3:i * 3 + 3])) for i in set(quantized.tobytes())}
            background = bytes(lookup[i] for i in quantized.tobytes())
    elif c.background_requested:
        name = c.globals.get("BACKGROUND")
        if not isinstance(name, str):
            raise api.CustomUIError("BACKGROUND must specify an image path")
        try:
            with Image.open(asset(name)) as source:
                image = ImageOps.fit(ImageOps.exif_transpose(source).convert("RGB"), (128, 160), method=Image.Resampling.LANCZOS)
        except (OSError, ValueError, Image.DecompressionBombError) as exc:
            raise api.CustomUIError(f"cannot read BACKGROUND image: {exc}") from exc
        remaining = 128 - len(c.palette)
        if remaining < 1:
            raise api.CustomUIError("no background palette space")
        quantized = image.quantize(colors=remaining, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
        colors = quantized.getpalette()
        lookup = {i: c.index(tuple(colors[i * 3:i * 3 + 3])) for i in set(quantized.tobytes())}
        background = bytes(lookup[i] for i in quantized.tobytes())
    masks, descriptors = bytearray(), bytearray()
    for g in c.glyphs:
        descriptors.extend(struct.pack("<BBBBB3xII", ord(g.key), 0, g.width, g.height, g.advance, len(masks), len(g.mask)))
        masks.extend(g.mask)
    parts = (struct.pack("<" + "H" * len(c.palette), *c.palette), background, bytes(descriptors),
             bytes(masks), b"".join(o.pack() for o in c.ops), bytes(c.text_pool))
    offsets, cursor = [], 128
    for part in parts:
        offsets.append(cursor if part else 0)
        cursor += len(part)
    payload, h = b"".join(parts), bytearray(128)
    struct.pack_into("<4sHHIIIHHIHHIIIIIIIHH", h, 0, b"PUI1", version, 128, cursor, zlib.crc32(payload),
                     2 | bool(background), 128, 160, offsets[0], len(c.palette), len(c.glyphs),
                     offsets[1], len(background), offsets[2], len(descriptors), offsets[3], len(masks), offsets[4], len(c.ops), 24)
    h[64:68] = bytes(indexes)
    for i, span in enumerate(scenes):
        struct.pack_into("<HH", h, 68 + i * 4, *span)
    struct.pack_into("<II", h, 100, offsets[5], len(c.text_pool))
    if pages:
        struct.pack_into("<IIII", h, 108, *pages)
    if cursor > api.MAX_PACKAGE:
        raise api.CustomUIError(f"theme is {cursor} bytes; maximum is 61440 (60 KiB). Use fewer distinct background images or share images between pages")
    struct.pack_into("<I", h, 124, zlib.crc32(h[:124]))
    data = bytes(h) + payload
    parse_theme(data, api=api)
    return data


def render_theme(p, *, note, cents, signal_valid, in_tune, scene, frame, api):
    if scene not in SCENES or type(frame) is not int or not 0 <= frame <= 23:
        raise api.CustomUIError("preview scene/frame is invalid")
    if note not in [k + suffix for k in "ABCDEFG" for suffix in ("", "#", "b")] or not isinstance(cents, (int, float)) or not math.isfinite(cents) or abs(cents) > 1000000:
        raise api.CustomUIError("invalid preview note/cents")
    good = signal_valid and (abs(cents) <= 3 if in_tune is None else in_tune)
    image = Image.new("RGB", (128, 160), api.rgb888(p.palette[p.clear]))
    draw = ImageDraw.Draw(image)
    def rect(x, y, w, h, color):
        if w > 0 and h > 0:
            draw.rectangle((x, y, x + w - 1, y + h - 1), fill=api.rgb888(p.palette[color]))
    for name in (["boot"] if scene == "boot" else [scene, "header", "footer"]):
        idx = SCENES.index(name)
        first, count = p.scenes[idx]
        end = first + count
        if not count:
            continue  # Firmware supplies the built-in region when omitted.
        top, bottom = bounds(idx)
        rect(0, top, 128, bottom - top, p.clear)
        page = BACKGROUND_PAGES.index(scene)
        selected = p.background_pages[page] if p.version == 3 else 1
        if p.background and selected:
            bg = Image.new("RGB", (128, 160))
            chunk = p.background[(selected - 1) * 20480:selected * 20480]
            bg.putdata([api.rgb888(p.palette[c]) for c in chunk])
            image.paste(bg.crop((0, top, 128, bottom)), (0, top))
        i = first
        while i < end:
            o = p.ops[i]
            if o.code == 4:
                conditions = (signal_valid, good, signal_valid and not good and cents < 0,
                              signal_valid and not good and cents > 0, frame == o.extra,
                              frame >= o.extra, False, True, False, True)
                if not conditions[o.arg]:
                    i += o.value
            elif o.code == 1:
                rect(o.x, o.y, o.width, o.height, o.color)
            elif o.code == 2:
                glyphs = [p.glyphs[k] for k in (note if signal_valid else "-")]
                width = glyphs[0].width if len(glyphs) == 1 else glyphs[0].advance + glyphs[1].width
                x = o.x + (o.width - width) // 2
                color = (p.good if good else p.bad) if o.arg else o.color
                for g in glyphs:
                    image.paste(api.rgb888(p.palette[color]), (x, o.y, x + g.width, o.y + g.height), Image.frombytes("1", (g.width, g.height), g.mask))
                    x += g.advance
            elif o.code == 3:
                center = o.x + o.width // 2
                pos = center + math.trunc(min(o.value, max(-o.value, round(cents * 1000))) * (o.width // 2 - 1) / o.value)
                color = p.good if good else o.color
                rect(center, o.y, 1, o.height, p.good)
                if signal_valid:
                    if o.arg == 0:
                        rect(center - 1 if good else min(center + 1, pos), o.y,
                             3 if good else abs(pos - center), o.height, color)
                    elif o.arg == 1:
                        rect(pos, o.y, 1, o.height, color)
                    else:
                        index = min(6, (pos - o.x) * 6 // (o.width - 1))
                        for j in range(7):
                            rect(o.x + j * ((o.width - 3) // 6), o.y, 3, o.height, color if j == index else p.foreground)
            elif o.code == 5:
                values = (p.text[o.extra:o.extra + o.extra2].decode("ascii"), "G6 STD B0", "440", "TRUE", "MON", "", "TUNE", "-12", "-24", str(math.trunc(cents)), "INPUT")
                scale, align = o.value & 255, o.value >> 8
                text = values[o.arg][:min(31, (o.width + scale) // (6 * scale))]
                x = o.x + (0 if not align else (o.width - (6 * len(text) - 1) * scale) // (2 if align == 1 else 1))
                # Small built-in text is illustrative; custom note masks are exact.
                draw.text((x, o.y), text, font=ImageFont.load_default(size=8 * scale), fill=api.rgb888(p.palette[o.color]), anchor="lt")
            elif o.arg == 0:
                rect(o.x, o.y, o.width * 16 // 24, o.height, o.color)
            elif o.arg == 1:
                for x in range(o.width):
                    size = int(abs(math.sin(x / 7)) * (o.height - 1) / 2)
                    rect(o.x + x, o.y + o.height // 2 - size, 1, size * 2 + 1, o.color)
            elif o.arg == 2:
                for band in range(16):
                    size = int(o.height * (0.2 + 0.75 * abs(math.sin(band * 0.7))))
                    left, right = band * o.width // 16, (band + 1) * o.width // 16
                    rect(o.x + left, o.y + o.height - size, max(1, right - left - 1), size, o.color)
            elif o.arg == 3:
                for string in range(6):
                    x = o.x + string * o.width // 6 + o.width // 12
                    ink = p.good if string % 3 else o.color
                    if o.value:
                        rect(x, o.y + o.height // 2, 1, o.height // 2, p.foreground)
                        rect(x + (string % 3 - 1) * 3, o.y + o.height // 2, 1, o.height // 2, ink)
                    else:
                        rect(x, o.y + o.height - 3, 1, 3, ink)
            elif o.arg == 4:
                for dot in range(3):
                    rect(o.x + dot * (o.width - 3) // 2, o.y, 3, o.height, o.color if dot == (1 if scene == "meter" else 0) else p.foreground)
            else:
                rect(o.x, o.y, o.width * frame // 23, o.height, o.color)
            i += 1
    return image
