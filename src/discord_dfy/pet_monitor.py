"""The Live Monitor as a window of the desktop pet: the same stream, without a terminal.

It reads the same event files as dfy-monitor and puts them through the monitor's own Renderer,
so what shows here is what the terminal shows: same redaction, same order, same labels.
Only the last step differs: the renderer's styled text goes into a text box instead of a
console. Like the terminal monitor it only reads, and closing it changes nothing for her.
"""

import ctypes
import time
from collections import deque

from .monitor import EventTail, Renderer

HISTORY = 200  # Events read back when the window opens (the most the tail keeps).
KEPT = 4000  # Events remembered, to draw again when "详细" is switched.
MAX_LINES = 5000  # The text box forgets its oldest lines beyond this.
POLL_MS = 200

BACK, BAR, FORE, DIM = "#1a1b26", "#24283b", "#c0caf5", "#a9b1d6"
COLOURS = {
    "blue": "#7aa2f7",
    "green": "#9ece6a",
    "yellow": "#e0af68",
    "red": "#f7768e",
    "magenta": "#bb9af7",
    "grey70": "#9aa0b8",
}
INTRO = "Live Monitor｜只读观察｜关掉这个窗口不影响她\n"
NO_EVENTS = "这里还没有事件：要在 .env 里开 MONITOR_ENABLED=true，并且她正在运行。\n"


def pieces(text) -> list[tuple[str, str]]:
    """A Rich Text as (words, style name) runs, in order. Nothing is parsed or interpreted."""
    plain, base = text.plain, str(text.style or "")
    out, at = [], 0
    for span in sorted(text.spans, key=lambda span: span.start):
        start, end = max(span.start, at), min(span.end, len(plain))
        if start > at:
            out.append((plain[at:start], base))
        if end > start:
            out.append((plain[start:end], str(span.style)))
            at = end
    if at < len(plain):
        out.append((plain[at:], base))
    return out


def look(style: str) -> tuple[str, bool]:
    """(colour, bold) for one of the renderer's style names; unknown ones are plain."""
    words = style.split()
    colour = next((COLOURS[word] for word in words if word in COLOURS), FORE)
    return colour, "bold" in words


class Page:
    """Stands where the renderer expects a console and keeps what it prints."""

    def __init__(self):
        self.runs: list[tuple[str, str]] = []

    def print(self, text, end="\n", **_):
        self.runs += pieces(text)
        if end:
            self.runs.append((end, ""))

    def take(self) -> list[tuple[str, str]]:
        """What was printed since the last take; neighbours of one style are joined."""
        joined: list[tuple[str, str]] = []
        for words, style in self.runs:
            if joined and joined[-1][1] == style:
                joined[-1] = (joined[-1][0] + words, style)
            else:
                joined.append((words, style))
        self.runs = []
        return joined


class Feed:
    """Events in, styled text out: the monitor without a screen."""

    def __init__(self, directories, secrets=(), *, verbose=False, history=HISTORY):
        self.directories, self.secrets, self.verbose = list(directories), secrets, verbose
        self.tail = EventTail(self.directories, history=history)
        self.kept = deque(maxlen=KEPT)
        self.opened = False
        self._begin()

    def _begin(self) -> None:
        self.page = Page()
        # The window polls a few times a second; that is the throttle, so every poll draws.
        self.renderer = Renderer(
            None, secrets=self.secrets, verbose=self.verbose, console=self.page, refresh_rate=20
        )

    def _intro(self) -> list[tuple[str, str]]:
        lines = [(INTRO, "blue")]
        if not any(directory.is_dir() for directory in self.directories):
            lines.append((NO_EVENTS, "yellow"))
        return lines

    def poll(self) -> list[tuple[str, str]]:
        """What is new since the last poll (the first poll starts with the recent past)."""
        events = self.tail.poll()
        if lost := self.tail.take_lost():
            self.renderer.warning(f"{lost} 条记录/文件因损坏、截断或未读轮换无法读取。")
        for event in events:
            self.kept.append(event)
            self.renderer.event(event)
        self.renderer.refresh(force=True)
        runs = self.page.take()
        if not self.opened:
            self.opened = True
            runs = self._intro() + runs
        return runs

    def again(self, verbose: bool) -> list[tuple[str, str]]:
        """Everything remembered, drawn afresh with or without the details."""
        self.verbose = verbose
        self._begin()
        for event in self.kept:
            self.renderer.event(event)
        self.renderer.refresh(force=True)
        return self._intro() + self.page.take()


class Panel:
    """The window. `state` is its corner of the pet's saved state: open, geometry, verbose."""

    def __init__(self, root, make_feed, state: dict, save, picture=None):
        import tkinter as tk

        self.root, self.make_feed, self.state, self.save = root, make_feed, state, save
        self.feed, self.job, self.icon, self.moved = None, None, None, None
        self.window = window = tk.Toplevel(root)
        window.withdraw()
        window.title("小D · Monitor")
        window.configure(bg=BACK)
        window.minsize(320, 200)
        window.protocol("WM_DELETE_WINDOW", self.hide)
        self._icon(picture)
        face = ("Microsoft YaHei UI", 10)
        bar = tk.Frame(window, bg=BAR)
        bar.pack(fill="x")
        self.status = tk.Label(bar, text="", bg=BAR, fg=FORE, font=face, anchor="w")
        self.status.pack(side="left", padx=10, pady=5, fill="x", expand=True)
        self.detail = tk.BooleanVar(value=bool(state.get("verbose")))
        quiet = {"bg": BAR, "fg": DIM, "activebackground": BAR, "activeforeground": FORE}
        tk.Checkbutton(
            bar,
            text="详细",
            variable=self.detail,
            command=self.redraw,
            font=face,
            selectcolor=BACK,
            bd=0,
            highlightthickness=0,
            **quiet,
        ).pack(side="right", padx=(0, 8))
        self.latest = tk.Button(
            bar, text="↓ 最新", command=self.to_latest, font=face, bd=0, relief="flat", **quiet
        )
        self.text = text = tk.Text(
            window,
            wrap="char",
            bg=BACK,
            fg=FORE,
            font=face,
            bd=0,
            highlightthickness=0,
            padx=12,
            pady=8,
            spacing1=1,
            spacing3=1,
            insertwidth=0,
            selectbackground="#33467c",
            selectforeground=FORE,
            state="disabled",
        )
        text.pack(fill="both", expand=True)
        self.tags = set()

    def _icon(self, picture) -> None:
        if picture is None:
            return
        try:
            from PIL import ImageTk

            side = min(picture.size)
            left = (picture.width - side) // 2
            head = picture.crop((left, 0, left + side, side)).resize((64, 64))
            self.icon = ImageTk.PhotoImage(head, master=self.window)
            self.window.iconphoto(False, self.icon)
        except Exception:  # noqa: BLE001 - an icon is never worth failing for.
            self.icon = None

    def _dark_title(self) -> None:
        try:
            handle = ctypes.windll.user32.GetParent(self.window.winfo_id())
            on = ctypes.c_int(1)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(handle, 20, ctypes.byref(on), 4)
        except (AttributeError, OSError):
            pass

    @property
    def open(self) -> bool:
        return self.feed is not None

    def show(self, geometry: str) -> None:
        if self.open:
            self.window.deiconify()
            self.window.lift()
            return
        self.window.geometry(geometry)
        self.window.update_idletasks()
        self._dark_title()
        self.window.deiconify()
        self.feed = self.make_feed(self.detail.get())
        self.clear()
        self.state["open"] = True
        self.save()
        self.tick()

    def hide(self) -> None:
        if not self.open:
            return
        self.keep_place()
        if self.job is not None:
            self.root.after_cancel(self.job)
        self.feed, self.job = None, None
        self.window.withdraw()
        self.state["open"] = False
        self.save()

    def keep_place(self) -> None:
        """Remember where the window is, unless it is minimised or maximised right now."""
        if self.open and self.window.state() == "normal":
            self.state["geometry"] = self.window.geometry()

    def set_status(self, text: str) -> None:
        if self.open and self.status.cget("text") != text:
            self.status.configure(text=text)

    def following(self) -> bool:
        return self.text.yview()[1] >= 0.999

    def to_latest(self) -> None:
        self.text.see("end")
        self.latest.pack_forget()

    def clear(self) -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")

    def redraw(self) -> None:
        self.state["verbose"] = self.detail.get()
        self.save()
        if self.open:
            self.clear()
            self.add(self.feed.again(self.detail.get()), follow=True)

    def add(self, runs, follow: bool | None = None) -> None:
        if not runs:
            return
        follow = self.following() if follow is None else follow
        text = self.text
        text.configure(state="normal")
        for words, style in runs:
            if style not in self.tags:
                colour, bold = look(style)
                font = ("Microsoft YaHei UI", 10, "bold") if bold else ("Microsoft YaHei UI", 10)
                text.tag_configure(style or "plain", foreground=colour, font=font)
                self.tags.add(style)
            text.insert("end", words, style or "plain")
        lines = int(text.index("end-1c").split(".")[0])
        if lines > MAX_LINES:
            text.delete("1.0", f"{lines - MAX_LINES + 1}.0")
        text.configure(state="disabled")
        if follow:
            text.see("end")

    def tick(self) -> None:
        if not self.open:
            return
        started = time.perf_counter()
        try:
            self.add(self.feed.poll())
        except OSError:
            pass  # A file went away mid-read; the next poll starts from what is there.
        if self.following():
            self.latest.pack_forget()
        elif not self.latest.winfo_ismapped():
            self.latest.pack(side="right", padx=(0, 6))
        # Moved or resized: remembered once it has stayed put for a second, so the place
        # survives the computer shutting down with the window open.
        if self.window.state() == "normal":
            place = self.window.geometry()
            if place != self.state.get("geometry"):
                self.state["geometry"], self.moved = place, started
        if self.moved is not None and started - self.moved > 1.0:
            self.moved = None
            self.save()
        # A slow disk never piles polls on top of each other.
        spent = int((time.perf_counter() - started) * 1000)
        self.job = self.root.after(max(POLL_MS, spent * 2), self.tick)
