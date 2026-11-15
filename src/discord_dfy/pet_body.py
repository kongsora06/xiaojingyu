"""The desktop pet's body: cutting her picture out of its background, the springs that
make her bouncy, and drawing one frame. No window here; pet.py puts frames on the screen.

Everything she does on the desktop is squash, stretch and lean on one flat picture:
poked, she squashes and wobbles back; picked up, she stretches and swings from where she
is held; put down, she lands with a bounce. The lantern on her chest is found by its colour
and glows while she is busy with something.
"""

import math
import random
from dataclasses import dataclass, field

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

# The picture kept on disk: tall enough to stay sharp at the largest size on a scaled screen.
MASTER_HEIGHT = 900
# A background pixel is this close to white (the picture's corners decide which white).
BACKGROUND_FLOOR = 236
# Springs: how stiff (higher = faster wobble) and how quickly the wobble dies down.
SQUASH = (300.0, 8.0)
LEAN = (130.0, 6.5)
BUBBLE = (420.0, 26.0)
GRAVITY = 9.0  # Her own heights per second², for hops: quick up, quick down.
MAX_LEAN = 0.26
HEART_SECONDS = 1.4
INK, PAPER, WARM, MUTED = (36, 48, 58), (255, 253, 248), (150, 96, 30), (96, 108, 118)
HEART = (236, 112, 140)
LANTERN = (255, 150, 40)  # What her lantern adds to the colours near it.
FONTS = ("msyh.ttc", "msyhl.ttc", "simhei.ttf", "simsun.ttc", "NotoSansCJK-Regular.ttc")


# ---------- her picture ----------


def cut_out(picture: Image.Image) -> Image.Image:
    """Her picture without its plain light background, edges kept soft, trimmed to her.

    The background is whatever near-white area touches the border, so the white of her
    cuffs, frills and the lights in her eyes stays. A picture that already has a
    transparent background is only trimmed.
    """
    if picture.mode in {"RGBA", "LA", "PA"} or "transparency" in picture.info:
        rgba = picture.convert("RGBA")
        if rgba.getchannel("A").getextrema()[0] < 250:
            return trim(rgba)
    rgb = picture.convert("RGB")
    red, green, blue = rgb.split()
    lightest = ImageChops.darker(ImageChops.darker(red, green), blue)  # min of the channels
    light = lightest.point(lambda value: 255 if value >= BACKGROUND_FLOOR else 0)
    width, height = rgb.size
    edge = [(x, y) for x in range(0, width, 7) for y in (0, height - 1)]
    edge += [(x, y) for y in range(0, height, 7) for x in (0, width - 1)]
    for point in edge:
        if light.getpixel(point) == 255:
            ImageDraw.floodfill(light, point, 128)
    outside = light.point(lambda value: 255 if value == 128 else 0)
    # Next to the background her outline fades into white: there, how far a pixel is from
    # white is how much of it is her. Everywhere else inside she is solid.
    rim = ImageChops.subtract(outside.filter(ImageFilter.MaxFilter(5)), outside)
    faded = ImageChops.invert(lightest).point(lambda value: min(255, int(value * 1.5)))
    alpha = Image.composite(faded, Image.new("L", rgb.size, 255), rim)
    alpha = ImageChops.subtract(alpha, outside)
    # Take the white out of the faded pixels: colour = (seen - white × (1 - alpha)) / alpha.
    white_part = ImageChops.invert(alpha)
    bands = [ImageChops.subtract(band, white_part) for band in (red, green, blue)]
    return trim(Image.merge("RGBa", [*bands, alpha]).convert("RGBA"))


def trim(picture: Image.Image) -> Image.Image:
    box = picture.getchannel("A").point(lambda value: 255 if value > 8 else 0).getbbox()
    picture = picture.crop(box) if box else picture
    if picture.height > MASTER_HEIGHT:
        width = round(picture.width * MASTER_HEIGHT / picture.height)
        picture = picture.resize((width, MASTER_HEIGHT), Image.LANCZOS)
    return picture


def find_lantern(picture: Image.Image) -> tuple[float, float] | None:
    """Where her lantern is, as fractions of the picture: the middle of its warm light."""
    small = picture.convert("RGBA")
    small.thumbnail((240, 240))
    points = [
        (x, y)
        for y in range(small.height)
        for x in range(small.width)
        for r, g, b, a in [small.getpixel((x, y))]
        if a > 200 and r > 225 and 150 < g < 235 and b < 150 and r - b > 90
    ]
    if len(points) < 4:
        return None
    xs, ys = sorted(x for x, _ in points), sorted(y for _, y in points)
    middle = len(points) // 2  # The median: a stray gold button does not pull it away.
    return (xs[middle] + 0.5) / small.width, (ys[middle] + 0.5) / small.height


# ---------- springs ----------


@dataclass
class Spring:
    stiffness: float
    damping: float
    value: float = 0.0
    speed: float = 0.0
    target: float = 0.0

    def step(self, dt: float) -> None:
        steps = max(1, math.ceil(dt / 0.004))  # Small steps keep a stiff spring sane.
        h = dt / steps
        for _ in range(steps):
            pull = self.stiffness * (self.target - self.value) - self.damping * self.speed
            self.speed += pull * h
            self.value += self.speed * h

    def kick(self, speed: float) -> None:
        self.speed += speed

    def resting(self) -> bool:
        return abs(self.value - self.target) < 0.002 and abs(self.speed) < 0.02


@dataclass
class Heart:
    x: float
    y: float
    drift: float
    size: float
    age: float = 0.0


@dataclass
class Body:
    """How she is deformed right now. Lengths are in sprite heights, so size does not matter."""

    squash: Spring = field(default_factory=lambda: Spring(*SQUASH))  # > 0 taller, < 0 flatter
    lean: Spring = field(default_factory=lambda: Spring(*LEAN))  # > 0: top right, feet left
    hang: Spring = field(default_factory=lambda: Spring(160.0, 22.0))  # 1 while held
    height: float = 0.0  # Off the ground, during a hop.
    rising: float = 0.0
    clock: float = 0.0
    held: bool = False
    busy: bool = False
    hearts: list[Heart] = field(default_factory=list)

    def poke(self, side: float = 0.0) -> None:
        """Pressed like jelly; side is -1 (poked on her left) to 1."""
        self.squash.kick(-3.2)
        self.lean.kick(-2.4 * side)

    def pick_up(self) -> None:
        self.held = True
        self.hang.target = 1.0
        self.squash.target = 0.07  # She hangs a little longer than she stands.
        self.squash.kick(2.6)
        self.height = self.rising = 0.0

    def swing(self, speed: float) -> None:
        """Carried sideways at this many sprite heights a second: her feet trail behind."""
        self.lean.target = max(-MAX_LEAN, min(MAX_LEAN, speed * 0.16))

    def put_down(self) -> None:
        self.held = False
        self.hang.target = self.squash.target = self.lean.target = 0.0
        self.squash.kick(-3.6)

    def hop(self, strength: float = 1.0) -> None:
        if not self.held and self.height == 0.0:
            self.rising = 1.8 * strength
            self.squash.kick(2.2)

    def pet(self, rng=random) -> None:
        self.squash.kick(-1.1)
        for _ in range(2):
            self.hearts.append(
                Heart(rng.uniform(0.3, 0.7), rng.uniform(0.02, 0.2), rng.uniform(-0.12, 0.12),
                      rng.uniform(0.07, 0.11))
            )  # fmt: skip
        del self.hearts[:-14]

    def step(self, dt: float) -> None:
        dt = min(dt, 0.05)  # After a stall, do not shoot her across the screen.
        self.clock += dt
        for spring in (self.squash, self.lean, self.hang):
            spring.step(dt)
        self.squash.value = max(-0.45, min(0.5, self.squash.value))
        if self.rising or self.height:
            self.rising -= GRAVITY * dt
            self.height += self.rising * dt
            if self.height <= 0.0:
                landed, self.height, self.rising = self.rising, 0.0, 0.0
                self.squash.kick(landed * 1.6)  # Landing flattens her; the spring does the rest.
        for heart in self.hearts:
            heart.age += dt
            heart.y -= 0.22 * dt
            heart.x += heart.drift * dt
        self.hearts = [heart for heart in self.hearts if heart.age < HEART_SECONDS]

    def calm(self) -> bool:
        """Only breathing: nothing that needs many frames a second."""
        springs = (self.squash, self.lean, self.hang)
        return not self.hearts and not self.height and all(s.resting() for s in springs)

    def shape(self) -> tuple[float, float, float, float]:
        """(wide, tall, lean, off the ground): 1, 1, 0, 0 is the picture as drawn."""
        period, depth = (1.1, 0.014) if self.busy else (3.4, 0.011)
        breath = depth * math.sin(self.clock * math.tau / period)
        tall = 1.0 + self.squash.value + breath
        wide = 1.0 - 0.62 * self.squash.value - 0.5 * breath  # Flatter means wider: jelly.
        return wide, tall, self.lean.value, self.height

    def glow(self) -> float:
        """How bright her lantern is, 0 to 1: steady and low, or pulsing while she is busy."""
        if not self.busy:
            return 0.22
        return 0.62 + 0.38 * math.sin(self.clock * math.tau / 1.6)


# ---------- drawing ----------


def font(size: int) -> ImageFont.ImageFont:
    for name in FONTS:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def wrap(text: str, limit: int) -> list[str]:
    """Break a line every `limit` characters: Chinese has no spaces to break at."""
    lines = []
    for line in text.split("\n"):
        lines += [line[i : i + limit] for i in range(0, len(line), limit)] or [""]
    return lines[:4]


def bubble_picture(title: str, detail: str, scale: float, marked: bool = False) -> Image.Image:
    """A speech bubble with a tail: a bold line, a smaller line under it, a dot if marked."""
    big, small = font(round(15 * scale)), font(round(12 * scale))
    rows = [(line, big, INK) for line in wrap(title, 11)]
    rows += [(line, small, WARM if line.startswith("「") else MUTED) for line in wrap(detail, 14)]
    rows = [row for row in rows if row[0]]
    pad, gap, tail = round(11 * scale), round(4 * scale), round(9 * scale)
    sizes = [face.getbbox(line) for line, face, _ in rows]
    width = (
        max((box[2] for box in sizes), default=0) + 2 * pad + (round(14 * scale) if marked else 0)
    )
    heights = [face.getbbox("国g")[3] for _, face, _ in rows]
    height = sum(heights) + gap * (len(rows) - 1) + 2 * pad
    picture = Image.new("RGBA", (width + 4, height + tail + 4), (0, 0, 0, 0))
    draw = ImageDraw.Draw(picture)
    radius = round(13 * scale)
    middle = picture.width // 2
    outline = (*INK, 150)
    draw.polygon(
        [(middle - tail, height), (middle + tail, height), (middle, height + tail)],
        fill=PAPER, outline=outline,
    )  # fmt: skip
    draw.rounded_rectangle((1, 1, width + 1, height + 1), radius, fill=PAPER, outline=outline)
    draw.line(
        [(middle - tail + 2, height + 1), (middle + tail - 2, height + 1)], fill=PAPER, width=2
    )
    y = pad
    for (line, face, colour), tall in zip(rows, heights, strict=True):
        draw.text((pad + 1, y), line, font=face, fill=colour)
        y += tall + gap
    if marked:
        r = round(4 * scale)
        draw.ellipse((width - pad - r, pad, width - pad + r, pad + 2 * r), fill=(196, 84, 70))
    return picture


def heart_picture(size: int) -> Image.Image:
    big = 4 * max(size, 4)
    picture = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(picture)
    half = big / 2
    draw.ellipse((0, big * 0.08, half * 1.05, big * 0.6), fill=HEART)
    draw.ellipse((half * 0.95, big * 0.08, big - 1, big * 0.6), fill=HEART)
    draw.polygon(
        [(big * 0.04, big * 0.44), (big * 0.96, big * 0.44), (half, big * 0.97)], fill=HEART
    )
    return picture.resize((max(size, 4), max(size, 4)), Image.LANCZOS)


def faded(picture: Image.Image, strength: float) -> Image.Image:
    alpha = picture.getchannel("A").point(lambda value: int(value * strength))
    result = picture.copy()
    result.putalpha(alpha)
    return result


class Painter:
    """Draws her at one size. The window is bigger than she is: room to stretch, lean, hop,
    and for a bubble over her head."""

    def __init__(self, master: Image.Image, height: int, scale: float = 1.0):
        self.scale = scale
        width = max(1, round(master.width * height / master.height))
        self.sprite = master.resize((width, height), Image.LANCZOS)
        self.side = round(width * 0.42)
        self.top = round(height * 0.34) + round(92 * scale)
        self.size = (width + 2 * self.side, height + self.top + round(8 * scale))
        self.feet = (self.side + width / 2, self.top + height)
        spot = find_lantern(master)
        self.lantern = (spot[0] * width, spot[1] * height) if spot else None
        # Her lantern's light: brightest at the flame, gone a fifth of her height away.
        reach = max(8, round(height * 0.2))
        light = Image.new("L", (reach * 2, reach * 2), 0)
        ImageDraw.Draw(light).ellipse(
            (reach * 0.5, reach * 0.5, reach * 1.5, reach * 1.5), fill=255
        )
        self.light = light.filter(ImageFilter.GaussianBlur(reach * 0.26))
        self.lights: dict[int, Image.Image] = {}
        self.heart_cache: dict[tuple[int, int], Image.Image] = {}

    def place(self, shape) -> tuple[float, ...]:
        """Where each point of the picture goes: X = a·u + b·v + c, Y = e·v + f."""
        wide, tall, lean, up = shape[:4]
        hang = shape[4] if len(shape) > 4 else 0.0
        width, height = self.sprite.size
        # She stretches and leans about one point: her feet while standing, her head while
        # held, so the hand that holds her does not slide off.
        pivot = height * (1 - 0.82 * hang)
        a, b = wide, -lean * tall
        c = width / 2 * (1 - wide) + lean * tall * pivot + self.side
        e, f = tall, pivot * (1 - tall) - up * height + self.top
        return a, b, c, e, f

    def at(self, shape, u: float, v: float) -> tuple[float, float]:
        a, b, c, e, f = self.place(shape)
        return a * u + b * v + c, e * v + f

    def frame(self, body: Body, bubble: Image.Image | None = None, pop: float = 1.0) -> Image.Image:
        shape = (*body.shape(), body.hang.value)
        a, b, c, e, f = self.place(shape)
        # Pillow wants the way back: for each point of the frame, where in the picture it is.
        back = (1 / a, -b / (a * e), b * f / (a * e) - c / a, 0.0, 1 / e, -f / e)
        canvas = self.sprite.transform(self.size, Image.AFFINE, back, Image.BILINEAR)
        if self.lantern and (level := round(body.glow() * 10)) > 0:
            if level not in self.lights:
                self.lights[level] = Image.merge(
                    "RGB",
                    [self.light.point(lambda v, c=c: v * c * level // 2550) for c in LANTERN],
                )
            light = self.lights[level]
            x, y = self.at(shape, *self.lantern)
            x, y = self.inside(x - light.width / 2, y - light.height / 2, light)
            box = (x, y, x + light.width, y + light.height)
            # Light adds to what it falls on, and only falls on her: her outline stays hers.
            lit = canvas.crop(box)
            glowing = ImageChops.add(lit.convert("RGB"), light)
            glowing.putalpha(lit.getchannel("A"))
            canvas.paste(glowing, box)
        for heart in body.hearts:
            size = max(4, round(heart.size * self.sprite.height))
            level = max(1, round((1 - heart.age / HEART_SECONDS) ** 0.7 * 6))
            if (size, level) not in self.heart_cache:
                self.heart_cache[size, level] = faded(heart_picture(size), level / 6)
            picture = self.heart_cache[size, level]
            x, y = self.at(shape, heart.x * self.sprite.width, heart.y * self.sprite.height)
            sway = math.sin(heart.age * 7 + heart.x * 9) * size * 0.3
            canvas.alpha_composite(picture, self.inside(x + sway - size / 2, y - size, picture))
        if bubble is not None and pop > 0.05:
            if abs(pop - 1) > 0.01:
                grown = (max(1, round(bubble.width * pop)), max(1, round(bubble.height * pop)))
                bubble = bubble.resize(grown, Image.BILINEAR)
            x, y = self.at(shape, self.sprite.width / 2, self.sprite.height * 0.04)
            canvas.alpha_composite(
                bubble, self.inside(x - bubble.width / 2, y - bubble.height, bubble)
            )
        return canvas

    def inside(self, x: float, y: float, picture: Image.Image) -> tuple[int, int]:
        """Keep a pasted picture within the frame (alpha_composite refuses to go outside)."""
        return (
            max(0, min(self.size[0] - picture.width, round(x))),
            max(0, min(self.size[1] - picture.height, round(y))),
        )

    def bubble_box(self, bubble: Image.Image) -> tuple[int, int, int, int]:
        over_head = self.top + self.sprite.height * 0.04 - bubble.height
        x, y = self.inside(self.feet[0] - bubble.width / 2, over_head, bubble)
        return x, y, x + bubble.width, y + bubble.height
