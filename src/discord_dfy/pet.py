"""She lives on the desktop: her picture, always on top, bouncy to the touch (dfy-pet).

Click her and she squashes like jelly and says what she is up to; drag her and she dangles
and swings from your hand; rub her head and hearts float up; the wheel makes her bigger or
smaller. Her lantern pulses while she is busy with something (the same monitor events the
room page reads), she hops when she starts, and when she puts something new in her showcase
she tells you: a click opens her room (the right-click menu opens it any time). Poking her
again and again is just more poking: no double-click, so she can be bounced freely.

What her bubble says is never made up: it is the host's label for what she is doing, the
status line she set herself, or the name of a thing she put on show. It reads what the
monitor reads and writes two small files of its own: its place and size (data/pet.json),
and each pat and rub (data/pet-touch.jsonl), which she is told about with his next message
(pet_touch). It never talks to her itself.

The right-click menu also opens the Live Monitor as a window of hers (pet_monitor): the
same stream dfy-monitor prints in a terminal, so the pet can be the one place to watch her.
Windows only: a see-through window with soft edges needs UpdateLayeredWindow.
"""

import argparse
import ctypes
import json
import re
import threading
import time
import webbrowser
from ctypes import wintypes
from pathlib import Path

from PIL import Image

from .host_tools import read_showcase
from .monitor import event_sources, read_environment
from .pet_body import BUBBLE, Body, Painter, Spring, bubble_picture, cut_out
from .pet_monitor import Feed, Panel
from .pet_touch import Touches, touch_path
from .room_view import DEFAULT_PORT, make_server, view_from_env
from .widget import lines

SIZES = {"小": 180, "中": 260, "大": 360}
MIN_HEIGHT, MAX_HEIGHT = 120, 560
POLL_MS, SHOWCASE_MS = 700, 5000
# Rubbing: this many changes of direction over her head within this long.
PET_TURNS, PET_SECONDS = 3, 1.3
ULW_ALPHA, AC_SRC_ALPHA, WS_EX_LAYERED, GWL_EXSTYLE, GA_ROOT = 2, 1, 0x80000, -20, 2
SPI_GETWORKAREA = 48


class BitmapHeader(ctypes.Structure):
    _fields_ = [
        ("size", wintypes.DWORD), ("width", wintypes.LONG), ("height", wintypes.LONG),
        ("planes", wintypes.WORD), ("bits", wintypes.WORD), ("compression", wintypes.DWORD),
        ("image_size", wintypes.DWORD), ("x_ppm", wintypes.LONG), ("y_ppm", wintypes.LONG),
        ("used", wintypes.DWORD), ("important", wintypes.DWORD),
    ]  # fmt: skip


class Blend(ctypes.Structure):
    _fields_ = [("op", ctypes.c_ubyte), ("flags", ctypes.c_ubyte),
                ("alpha", ctypes.c_ubyte), ("format", ctypes.c_ubyte)]  # fmt: skip


class Layer:
    """A window whose every pixel has its own transparency; clicks fall through the clear ones."""

    def __init__(self, window_id: int):
        self.user, self.gdi = ctypes.windll.user32, ctypes.windll.gdi32
        handle = ctypes.c_void_p
        self.user.GetAncestor.restype = self.user.GetDC.restype = handle
        self.user.GetAncestor.argtypes = [handle, wintypes.UINT]
        self.user.GetDC.argtypes = [handle]
        self.user.GetWindowLongW.argtypes = [handle, ctypes.c_int]
        self.user.SetWindowLongW.argtypes = [handle, ctypes.c_int, wintypes.LONG]
        self.user.UpdateLayeredWindow.argtypes = [
            handle, handle, ctypes.c_void_p, ctypes.POINTER(wintypes.SIZE), handle,
            ctypes.POINTER(wintypes.POINT), wintypes.DWORD, ctypes.POINTER(Blend), wintypes.DWORD,
        ]  # fmt: skip
        self.gdi.CreateCompatibleDC.restype = self.gdi.CreateDIBSection.restype = handle
        self.gdi.SelectObject.restype = handle
        self.gdi.CreateCompatibleDC.argtypes = [handle]
        self.gdi.CreateDIBSection.argtypes = [
            handle, ctypes.POINTER(BitmapHeader), wintypes.UINT,
            ctypes.POINTER(ctypes.c_void_p), handle, wintypes.DWORD,
        ]  # fmt: skip
        self.gdi.SelectObject.argtypes = [handle, handle]
        self.gdi.DeleteObject.argtypes = [handle]
        self.window = self.user.GetAncestor(window_id, GA_ROOT)
        style = self.user.GetWindowLongW(self.window, GWL_EXSTYLE)
        self.user.SetWindowLongW(self.window, GWL_EXSTYLE, style | WS_EX_LAYERED)
        self.screen = self.user.GetDC(None)
        self.memory = self.gdi.CreateCompatibleDC(self.screen)
        self.bitmap, self.pixels, self.size = None, ctypes.c_void_p(), (0, 0)

    def show(self, picture: Image.Image) -> None:
        if picture.size != self.size:
            self.size = picture.size
            header = BitmapHeader(ctypes.sizeof(BitmapHeader), picture.width, -picture.height,
                                  1, 32, 0, 0, 0, 0, 0, 0)  # fmt: skip
            bitmap = self.gdi.CreateDIBSection(
                self.screen, ctypes.byref(header), 0, ctypes.byref(self.pixels), None, 0
            )
            self.gdi.SelectObject(self.memory, bitmap)
            if self.bitmap:
                self.gdi.DeleteObject(self.bitmap)
            self.bitmap = bitmap
        data = picture.tobytes("raw", "BGRa")  # Windows wants colour already times alpha.
        ctypes.memmove(self.pixels, data, len(data))
        self.user.UpdateLayeredWindow(
            self.window, self.screen, None, ctypes.byref(wintypes.SIZE(*picture.size)),
            self.memory, ctypes.byref(wintypes.POINT(0, 0)), 0,
            ctypes.byref(Blend(0, 0, 255, AC_SRC_ALPHA)), ULW_ALPHA,
        )  # fmt: skip


def work_area() -> tuple[int, int, int, int]:
    """The main screen without the taskbar: left, top, right, bottom."""
    box = wintypes.RECT()
    ctypes.windll.user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(box), 0)
    return box.left, box.top, box.right, box.bottom


def all_screens() -> tuple[int, int, int, int]:
    metric = ctypes.windll.user32.GetSystemMetrics
    left, top = metric(76), metric(77)
    return left, top, left + metric(78), top + metric(79)


def load_state(path: Path) -> dict:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


PLACE = re.compile(r"(\d+)x(\d+)\+(-?\d+)\+(-?\d+)")


def kept_place(geometry, screens: tuple[int, int, int, int]) -> str | None:
    """The monitor window's remembered size and place, if its title bar is still on a screen
    (a second display may have been unplugged since)."""
    found = PLACE.fullmatch(geometry) if isinstance(geometry, str) else None
    if not found:
        return None
    width, height, x, y = map(int, found.groups())
    left, top, right, bottom = screens
    reachable = left - width + 80 <= x <= right - 80 and top <= y <= bottom - 60
    return geometry if reachable and width >= 200 and height >= 120 else None


def unseen(showcase: list[dict], seen: dict) -> list[dict]:
    """What she put out (or put out again) since he last looked."""
    return [item for item in showcase if seen.get(item["path"]) != item["since"]]


class Rub:
    """Tell a rub (back and forth over her head) from the mouse just passing by."""

    def __init__(self):
        self.x, self.way, self.turns = None, 0, []

    def moved(self, x: float, now: float, reach: float) -> bool:
        """True when this movement completes a rub; reach is how far counts as a stroke."""
        if self.x is None:
            self.x = x
            return False
        step = x - self.x
        if abs(step) < reach:
            return False
        way, self.x = (1 if step > 0 else -1), x
        if way != self.way:
            self.way = way
            self.turns = [at for at in self.turns if now - at < PET_SECONDS] + [now]
        if len(self.turns) >= PET_TURNS:
            self.turns = self.turns[1:]
            return True
        return False

    def left(self) -> None:
        self.x, self.way, self.turns = None, 0, []


class Pet:
    def __init__(self, view, room_url: str, picture_path: Path, state_path: Path, sources=None):
        """sources: (event directories, secrets never to show) for the monitor window."""
        import tkinter as tk

        self.view, self.room_url = view, room_url
        self.picture_path, self.state_path = picture_path, state_path
        self.state = load_state(state_path)
        self.root = root = tk.Tk()
        root.title("小桌宠")
        root.withdraw()  # Shown once her first frame is ready: no grey box flashing up.
        root.overrideredirect(True)
        root.attributes("-topmost", self.state.get("on_top", True))
        self.dpi = root.winfo_fpixels("1i") / 96
        self.master = Image.open(picture_path).convert("RGBA")
        self.body = Body()
        self.height = int(self.state.get("height") or SIZES["中"])
        self.painter = Painter(self.master, round(self.height * self.dpi), self.dpi)
        left, top, right, bottom = work_area()
        self.feet = [
            float(self.state.get("x", right - 170 * self.dpi)),
            float(self.state.get("y", bottom)),
        ]
        self.place()
        root.update_idletasks()
        self.layer = Layer(root.winfo_id())
        # What she is saying: the bubble's picture, how long it stays, what a click on it does.
        self.bubble, self.bubble_until, self.bubble_text = None, None, None
        self.pop = Spring(*BUBBLE)
        self.note = None  # Something new in the showcase, until he has looked.
        self.live, self.was_busy, self.quiet_since = None, False, 0.0
        self.seen = self.state.get("seen")
        self.press, self.dragging, self.last_drag = None, False, (0.0, 0.0, 0.0)
        self.speed, self.rub, self.opened = 0.0, Rub(), 0.0
        # Written down beside her other data, for her to hear of with his next message.
        self.touches = Touches(touch_path(state_path.parent))
        # The Live Monitor as a window of hers: open or not, and where, is remembered.
        directories, secrets = sources or ([], ())
        if not isinstance(self.state.get("monitor"), dict):
            self.state["monitor"] = {}
        self.panel = Panel(
            root,
            lambda verbose: Feed(directories, secrets, verbose=verbose),
            self.state["monitor"],
            self.save,
            self.master,
        )
        self.menu = self.build_menu(tk)
        root.bind("<ButtonPress-1>", self.on_press)
        root.bind("<B1-Motion>", self.on_drag)
        root.bind("<ButtonRelease-1>", self.on_release)
        root.bind("<Button-3>", lambda event: self.menu.tk_popup(event.x_root, event.y_root))
        root.bind("<Motion>", self.on_move)
        root.bind("<Leave>", lambda event: self.rub.left())
        root.bind("<MouseWheel>", self.on_wheel)
        self.last_frame = time.perf_counter()
        self.body.hop(0.9)  # Hello.
        self.frame()
        root.deiconify()
        self.poll()
        self.check_showcase()
        if self.state["monitor"].get("open"):
            self.show_monitor()

    # ----- where she is -----

    def place(self) -> None:
        left, top, right, bottom = all_screens()
        margin = 40 * self.dpi
        self.feet[0] = max(left + margin, min(right - margin, self.feet[0]))
        self.feet[1] = max(top + 2 * margin, min(bottom, self.feet[1]))
        x = round(self.feet[0] - self.painter.feet[0])
        y = round(self.feet[1] - self.painter.feet[1])
        width, height = self.painter.size
        self.root.geometry(f"{width}x{height}+{x}+{y}")  # "+-40" is 40 left of the main screen.

    def resize(self, height: float) -> None:
        self.height = round(max(MIN_HEIGHT, min(MAX_HEIGHT, height)))
        self.painter = Painter(self.master, round(self.height * self.dpi), self.dpi)
        self.bubble_text = None  # Drawn again where it now belongs.
        self.place()
        self.save()

    def save(self) -> None:
        self.state.update(x=self.feet[0], y=self.feet[1], height=self.height, seen=self.seen)
        self.panel.keep_place()
        try:
            self.state_path.write_text(json.dumps(self.state, ensure_ascii=False), "utf-8")
        except OSError:
            pass  # Next time she starts in the corner again; nothing else is lost.

    def home(self) -> None:
        left, top, right, bottom = work_area()
        self.feet = [right - 170 * self.dpi, float(bottom)]
        self.place()
        self.body.put_down()
        self.save()

    # ----- the mouse -----

    def on_press(self, event) -> None:
        self.press = (event.x_root, event.y_root, event.x, event.y)
        self.last_drag = (event.x_root, event.y_root, time.perf_counter())

    def on_drag(self, event) -> None:
        if self.press is None:
            return
        if not self.dragging:
            if abs(event.x_root - self.press[0]) + abs(event.y_root - self.press[1]) < 6 * self.dpi:
                return
            self.dragging = True
            self.grip = (self.feet[0] - self.press[0], self.feet[1] - self.press[1])
            self.body.pick_up()
        x, y, at = self.last_drag
        now = time.perf_counter()
        if now - at > 0.004:
            pace = (event.x_root - x) / (now - at) / self.painter.sprite.height
            self.speed = 0.6 * self.speed + 0.4 * pace
            self.body.swing(self.speed)
            self.last_drag = (event.x_root, event.y_root, now)
        self.feet = [event.x_root + self.grip[0], event.y_root + self.grip[1]]
        self.place()

    def on_release(self, event) -> None:
        if self.press is None:
            return
        x, y = self.press[2:]
        self.press = None
        if self.dragging:
            self.dragging, self.speed = False, 0.0
            self.body.put_down()
            self.save()
            return
        if self.bubble is not None and self.note and self.over_bubble(x, y):
            self.open_room()
            return
        sprite = self.painter.sprite
        side = (x - self.painter.feet[0]) / (sprite.width / 2)
        self.body.poke(max(-1.0, min(1.0, side)))
        self.touches.pat()
        if self.note:
            self.open_room()
        elif not self.body.busy:
            self.tell()

    def on_move(self, event) -> None:
        if self.press is not None:
            return
        sprite = self.painter.sprite
        over_head = self.painter.top <= event.y <= self.painter.top + sprite.height * 0.5
        now = time.perf_counter()
        if not over_head:
            self.rub.left()
        elif self.rub.moved(event.x, now, sprite.width * 0.07):
            self.body.pet()
            self.touches.rub(now)

    def on_wheel(self, event) -> None:
        self.resize(self.height * (1.08 if event.delta > 0 else 1 / 1.08))

    def over_bubble(self, x: float, y: float) -> bool:
        left, top, right, bottom = self.painter.bubble_box(self.bubble)
        return left <= x <= right and top <= y <= bottom

    # ----- what she says -----

    def say(self, title: str, detail: str = "", seconds: float | None = 5.0, marked=False) -> None:
        text = (title, detail, marked)
        if text != self.bubble_text:
            fresh = self.bubble is None
            self.bubble = bubble_picture(title, detail, self.dpi, marked)
            self.bubble_text = text
            if fresh:
                self.pop.value, self.pop.speed = 0.3, 0.0
        self.pop.target = 1.0
        self.bubble_until = None if seconds is None else time.perf_counter() + seconds

    def hush(self) -> None:
        self.pop.target = 0.0
        self.bubble_until = None

    def tell(self) -> None:
        """What she is up to, as the widget would put it."""
        if self.live is None:
            return
        title, detail, status = lines(self.live)
        self.say(title, "\n".join(part for part in (detail, status) if part))

    def show_monitor(self) -> None:
        """Open the monitor window where it was, or beside her the first time."""
        place = kept_place(self.state["monitor"].get("geometry"), all_screens())
        if place is None:
            left, top, right, bottom = work_area()
            width = min(round(540 * self.dpi), right - left)
            height = min(round(660 * self.dpi), bottom - top)
            gap = round(12 * self.dpi)
            mine = round(self.feet[0] - self.painter.feet[0])
            x = mine - width - gap  # To her left; to her right if there is no room there.
            if x < left:
                x = mine + self.painter.size[0] + gap
            x = max(left, min(right - width, x))
            y = max(top, bottom - height - round(48 * self.dpi))
            place = f"{width}x{height}+{x}+{y}"
        self.panel.show(place)

    def leave(self) -> None:
        self.save()  # The monitor window comes back next time if it was open now.
        self.root.destroy()

    def open_room(self) -> None:
        now = time.perf_counter()
        if now - self.opened > 1.0:  # Two quick clicks on her news are one wish, not two tabs.
            webbrowser.open(self.room_url)
        self.opened = now
        if self.note:
            self.seen = {item["path"]: item["since"] for item in read_showcase(self.view.room_dir)}
            self.note = None
            self.hush()
            self.save()

    def poll(self) -> None:
        try:
            self.live = self.view.now()
        except OSError:
            self.live = None
        live, now = self.live, time.perf_counter()
        busy = bool(live and live["doing"])
        self.body.busy = busy
        if busy:
            if not self.was_busy:
                self.body.hop(0.8)
            self.say(live["doing"], live["where"], None)
        elif self.was_busy:
            self.quiet_since = now
        elif self.note:
            names = "、".join(Path(item["path"]).stem for item in self.note[:3])
            self.say("橱窗里摆了新东西", f"{names}\n点我去看", None, marked=True)
        elif self.bubble_text and self.bubble_until is None and now - self.quiet_since > 2.5:
            self.hush()  # She stopped a moment ago; the label has had its time.
        self.was_busy = busy
        if live and self.panel.open:
            title, detail, status = lines(live)
            self.panel.set_status(" · ".join(part for part in (title, detail) if part))
        self.root.after(POLL_MS, self.poll)

    def check_showcase(self) -> None:
        showcase = read_showcase(self.view.room_dir)
        if self.seen is None:  # The first time: what is already out is not news.
            self.seen = {item["path"]: item["since"] for item in showcase}
            self.save()
        fresh = unseen(showcase, self.seen)
        if fresh and not self.note:
            self.body.hop(1.0)
            self.root.after(520, self.body.hop, 0.7)
        self.note = fresh or None
        self.root.after(SHOWCASE_MS, self.check_showcase)

    # ----- every frame -----

    def frame(self) -> None:
        now = time.perf_counter()
        dt, self.last_frame = now - self.last_frame, now
        if self.dragging and now - self.last_drag[2] > 0.09:
            self.speed = 0.0
            self.body.swing(0.0)  # Held still: she stops swinging.
        self.body.step(dt)
        self.pop.step(min(dt, 0.05))
        if self.bubble_until is not None and now > self.bubble_until:
            self.hush()
        if self.bubble is not None and self.pop.target == 0.0 and self.pop.value < 0.08:
            self.bubble = self.bubble_text = None
        self.layer.show(self.painter.frame(self.body, self.bubble, max(0.0, self.pop.value)))
        still = self.body.calm() and self.pop.resting() and not self.dragging
        self.root.after(40 if still else 15, self.frame)

    # ----- the menu -----

    def build_menu(self, tk):
        menu = tk.Menu(self.root, tearoff=False)
        menu.add_command(label="她在干嘛", command=self.tell)
        menu.add_command(label="看 Monitor", command=self.show_monitor)
        menu.add_command(label="打开她的房间", command=self.open_room)
        menu.add_separator()
        sizes = tk.Menu(menu, tearoff=False)
        for name, height in SIZES.items():
            sizes.add_command(label=name, command=lambda height=height: self.resize(height))
        menu.add_cascade(label="大小（滚轮也行）", menu=sizes)
        self.on_top = tk.BooleanVar(value=self.state.get("on_top", True))
        menu.add_checkbutton(label="总在最前", variable=self.on_top, command=self.set_on_top)
        menu.add_command(label="回到右下角", command=self.home)
        menu.add_command(label="换一张图…", command=self.change_picture)
        menu.add_separator()
        menu.add_command(label="让她回去", command=self.leave)
        return menu

    def set_on_top(self) -> None:
        self.state["on_top"] = self.on_top.get()
        self.root.attributes("-topmost", self.state["on_top"])
        self.save()

    def change_picture(self) -> None:
        if path := choose_picture():
            try:
                keep_picture(path, self.picture_path)
            except (OSError, ValueError):
                self.say("这张图打不开", "换一张 PNG / JPG / WebP 试试")
                return
            self.master = Image.open(self.picture_path).convert("RGBA")
            self.resize(self.height)
            self.body.hop(1.0)

    def run(self) -> None:
        self.root.mainloop()


def choose_picture() -> str:
    from tkinter import filedialog

    return filedialog.askopenfilename(
        title="选一张她的图（透明底或白底）",
        filetypes=[("图片", "*.png *.webp *.jpg *.jpeg *.gif"), ("所有文件", "*.*")],
    )


def keep_picture(source, target: Path) -> None:
    """Cut her out of the chosen picture and keep that as the pet's picture."""
    with Image.open(source) as picture:
        cut = cut_out(picture)
    target.parent.mkdir(parents=True, exist_ok=True)
    cut.save(target, "PNG")


def main():
    parser = argparse.ArgumentParser(description="桌宠：她待在桌面上，戳一下会弹")
    parser.add_argument("--image", help="换一张图：透明底或白底的立绘，会自动抠出来存好")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="她的房间页面的本机端口")
    args = parser.parse_args()
    if not hasattr(ctypes, "windll"):
        parser.error("桌宠只支持 Windows")
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # Real pixels on scaled displays.
    except (AttributeError, OSError):
        pass
    view = view_from_env()
    data = view.watcher.presence_path.parent
    picture = data / "pet.png"
    if args.image:
        keep_picture(args.image, picture)
    if not picture.is_file():
        import tkinter as tk

        chooser = tk.Tk()
        chooser.withdraw()
        chosen = choose_picture()
        chooser.destroy()
        if not chosen:
            return
        keep_picture(chosen, picture)
    try:
        # Serve the room page too, unless dfy-room (or the widget) already has the port.
        server = make_server(view, args.port)
        threading.Thread(target=server.serve_forever, daemon=True).start()
    except OSError:
        server = None
    try:
        sources = event_sources(read_environment())
        Pet(view, f"http://127.0.0.1:{args.port}/", picture, data / "pet.json", sources).run()
    finally:
        if server is not None:
            server.shutdown()


if __name__ == "__main__":
    main()
