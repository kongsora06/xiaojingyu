"""A small always-on-top window in a corner of the screen: what she is doing right now.

It reads the same monitor events as the room page and shows only a label ("在写日记"), how
long ago she last did something, and the status line she set for herself. Drag it anywhere;
double-click (or right-click) opens her room, which this window serves itself when dfy-room
is not already running. Read-only, like the monitor: closing it does not touch her.
"""

import argparse
import ctypes
import threading
import webbrowser
from datetime import datetime

from .now import ago
from .room_view import DEFAULT_PORT, make_server, view_from_env

WIDTH, HEIGHT = 250, 86
POLL_MS = 700
KEY = "#010203"  # Painted behind the rounded panel and made see-through.
PANEL, INK, MUTED, WARM = "#18222c", "#eef2f5", "#93a3b0", "#f2c988"
DOTS = {"online": "#5fb37f", "idle": "#f2b35a", "dnd": "#d2584b"}
UI_FONT, HAND_FONT = "Microsoft YaHei UI", "KaiTi"


def lines(live: dict, now: datetime | None = None) -> tuple[str, str, str]:
    """(what she is doing, a smaller line under it, her own status line)."""
    status = f"「{live['status']['text']}」" if live["status"]["text"] else ""
    if not live["watching"]:
        return "看不到她在做什么", "监视器没开，或者还没有动静", status
    if live["doing"]:
        return live["doing"], live["where"], status
    if live["did"]:
        return "发呆中", f"{ago(live['did_at'], now)}{live['did']}", status
    return "发呆中", f"上次有动静是{ago(live['since'], now)}", status


def rounded(canvas, x1, y1, x2, y2, r, **options):
    points = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2, x2 - r, y2,
              x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]  # fmt: skip
    return canvas.create_polygon(points, smooth=True, **options)


class Widget:
    def __init__(self, view, room_url):
        import tkinter as tk

        self.view, self.room_url = view, room_url
        self.root = root = tk.Tk()
        root.title("她现在")
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        try:
            root.attributes("-alpha", 0.95)
            root.attributes("-transparentcolor", KEY)
            background = KEY
        except tk.TclError:
            background = PANEL  # Not Windows: a plain square panel.
        root.configure(bg=background)
        # Sizes are for a 96 dpi screen; a scaled display gets a bigger window to match its text.
        scale = root.winfo_fpixels("1i") / 96
        self.px = px = lambda value: round(value * scale)
        x = root.winfo_screenwidth() - px(WIDTH + 24)
        y = root.winfo_screenheight() - px(HEIGHT + 72)
        root.geometry(f"{px(WIDTH)}x{px(HEIGHT)}+{x}+{y}")
        self.canvas = canvas = tk.Canvas(
            root, width=px(WIDTH), height=px(HEIGHT), bg=background, highlightthickness=0
        )
        canvas.pack()
        rounded(canvas, 1, 1, px(WIDTH) - 1, px(HEIGHT) - 1, px(18), fill=PANEL, outline="#2c3a47")
        self.dot = canvas.create_oval(0, 0, 0, 0, fill=DOTS["online"], outline="")
        self.title = canvas.create_text(
            px(38), px(25), anchor="w", fill=INK, font=(UI_FONT, 11, "bold")
        )
        self.detail = canvas.create_text(px(38), px(47), anchor="w", fill=MUTED, font=(UI_FONT, 9))
        self.status = canvas.create_text(
            px(38), px(68), anchor="w", fill=WARM, font=(HAND_FONT, 11)
        )
        self.menu = tk.Menu(root, tearoff=False)
        self.menu.add_command(label="打开她的房间", command=self.open_room)
        self.menu.add_separator()
        self.menu.add_command(label="关掉这个小窗", command=root.destroy)
        canvas.bind("<ButtonPress-1>", self.grab)
        canvas.bind("<B1-Motion>", self.drag)
        canvas.bind("<Double-Button-1>", lambda event: self.open_room())
        canvas.bind("<Button-3>", lambda event: self.menu.tk_popup(event.x_root, event.y_root))
        self.offset = (0, 0)
        self.beat = 0
        self.tick()

    def grab(self, event):
        self.offset = (event.x, event.y)

    def drag(self, event):
        self.root.geometry(f"+{event.x_root - self.offset[0]}+{event.y_root - self.offset[1]}")

    def open_room(self):
        webbrowser.open(self.room_url)

    def tick(self):
        try:
            live = self.view.now()
        except OSError:
            live = None
        if live is not None:
            title, detail, status = lines(live)
            canvas = self.canvas
            canvas.itemconfigure(self.title, text=title)
            canvas.itemconfigure(self.detail, text=detail)
            canvas.itemconfigure(self.status, text=status)
            # The dot breathes while she is at something.
            self.beat = (self.beat + 1) % 2 if live["doing"] else 0
            px, grow = self.px, 2 * self.beat
            canvas.coords(self.dot, px(16 - grow), px(19 - grow), px(28 + grow), px(31 + grow))
            canvas.itemconfigure(self.dot, fill=DOTS.get(live["status"]["state"], DOTS["online"]))
        self.root.after(POLL_MS, self.tick)

    def run(self):
        self.root.mainloop()


def main():
    parser = argparse.ArgumentParser(description="桌面小窗：她现在在干嘛（只读）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="她的房间页面的本机端口")
    args = parser.parse_args()
    view = view_from_env()
    try:
        # Serve the room page too, unless dfy-room already has the port.
        server = make_server(view, args.port)
        threading.Thread(target=server.serve_forever, daemon=True).start()
    except OSError:
        server = None
    if windll := getattr(ctypes, "windll", None):
        try:
            windll.shcore.SetProcessDpiAwareness(1)  # Sharp text on scaled displays.
        except (AttributeError, OSError):
            pass
    try:
        Widget(view, f"http://127.0.0.1:{args.port}/").run()
    finally:
        if server is not None:
            server.shutdown()


if __name__ == "__main__":
    main()
