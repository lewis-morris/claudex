"""`claudex demo`: a full-screen preview of a friendlier Claudex screen. No agents are run.

Keys: 1 2 3 switch colour scheme · space sends the pretend job round again · q quits.
Pure ANSI (24-bit colour, half-block "pixels"), no extra libraries.
"""
import math
import os
import select
import shutil
import sys
import termios
import time
import tty

PALETTES = {
    "1": ("Dusk", {"bg": "1b1724", "panel": "241e30", "edge": "3a3150", "text": "ece6f5", "dim": "8d84a3",
                   "warm": "ff9e7a", "warm2": "c96f52", "cool": "6fd3c7", "cool2": "3f9c92",
                   "accent": "c3a6ff", "good": "9be38b", "eye": "1b1724"}),
    "2": ("Meadow", {"bg": "141b16", "panel": "1c2620", "edge": "2f4136", "text": "e6efe4", "dim": "86998c",
                     "warm": "f2b45a", "warm2": "c28a3a", "cool": "7fc8e8", "cool2": "4f93b3",
                     "accent": "a3d977", "good": "a3d977", "eye": "141b16"}),
    "3": ("Paper", {"bg": "f6f1e7", "panel": "ece5d6", "edge": "d3c8b2", "text": "2b2a33", "dim": "7d7668",
                    "warm": "e07a5f", "warm2": "b5563d", "cool": "3d8a99", "cool2": "2b6673",
                    "accent": "6c5ce7", "good": "3f8f4f", "eye": "2b2a33"}),
}

# Two original characters, drawn as pixel grids. W/w warm body, C/c cool body, e eye, a accent, . empty.
CLAU = [
    "....WWWWWW....",
    "..WWWWWWWWWW..",
    ".WWWWWWWWWWWW.",
    "WWWWeeWWWeeWWW",
    "WWWWeeWWWeeWWW",
    "WWWWWWWWWWWWWW",
    "WWWWWwwwwWWWWW",
    ".WWWWWWWWWWWW.",
    "..ww......ww..",
]
DEX = [
    "......aa......",
    "......cc......",
    ".CCCCCCCCCCCC.",
    ".CCCCCCCCCCCC.",
    ".CCeeCCCCeeCC.",
    ".CCeeCCCCeeCC.",
    ".CCCCccccCCCC.",
    ".CCCCCCCCCCCC.",
    "..cc......cc..",
]


def rgb(h):
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def fg(h):
    return "\033[38;2;%d;%d;%dm" % rgb(h)


def bg(h):
    return "\033[48;2;%d;%d;%dm" % rgb(h)


def mix(a, b, t):
    ra, rb = rgb(a), rgb(b)
    return "%02x%02x%02x" % tuple(int(x + (y - x) * t) for x, y in zip(ra, rb))


class Screen:
    """A grid of cells (char, fg, bg), drawn in one write per frame."""

    def __init__(self, w, h, base):
        self.w, self.h = w, h
        self.cells = [[(" ", base, base) for _ in range(w)] for _ in range(h)]

    def put(self, x, y, s, f, b=None):
        if not 0 <= y < self.h:
            return
        for i, ch in enumerate(s):
            if 0 <= x + i < self.w:
                old = self.cells[y][x + i]
                self.cells[y][x + i] = (ch, f, b or old[2])

    def fill(self, x, y, w, h, b):
        for yy in range(y, y + h):
            self.put(x, yy, " " * w, b, b)

    def pixel(self, x, py, color):
        """Half-block pixels: two pixel rows per text row."""
        y = py // 2
        if not (0 <= y < self.h and 0 <= x < self.w):
            return
        ch, f, b = self.cells[y][x]
        top, bot = (f, b) if ch == "▀" else (b, b)
        if py % 2 == 0:
            top = color
        else:
            bot = color
        self.cells[y][x] = ("▀", top, bot)

    def render(self):
        out, last = ["\033[H"], None
        for y, row in enumerate(self.cells):
            out.append("\033[%d;1H" % (y + 1))
            for ch, f, b in row:
                if (f, b) != last:
                    out.append(fg(f) + bg(b))
                    last = (f, b)
                out.append(ch)
        return "".join(out)


def box(s, x, y, w, h, p, title=""):
    s.fill(x, y, w, h, p["panel"])
    s.put(x, y, "╭" + "─" * (w - 2) + "╮", p["edge"])
    for yy in range(y + 1, y + h - 1):
        s.put(x, yy, "│", p["edge"])
        s.put(x + w - 1, yy, "│", p["edge"])
    s.put(x, y + h - 1, "╰" + "─" * (w - 2) + "╯", p["edge"])
    if title:
        s.put(x + 2, y, f" {title} ", p["dim"])


def sprite(s, art, x, py, colors, blink):
    for r, line in enumerate(art):
        for c, k in enumerate(line):
            if k == ".":
                continue
            col = colors.get(k)
            if k == "e" and blink:
                col = colors["W"] if "W" in colors else colors["C"]
            s.pixel(x + c, py + r, col)


SCRIPT = [  # (seconds after start, who, line)
    (0.5, "you", "Add a VAT total to the quote page"),
    (1.6, "lead", "Two parts: the sums for Codex, the page for Claude."),
    (3.0, "lead", "Big decision: money in whole pence. Asking Codex…"),
    (4.4, "dex", "Agreed. Whole pence, VAT on the subtotal."),
    (6.0, "dex", "Added quote_total() and 5 tests. All pass."),
    (7.6, "clau", "Checked Codex's sums: looks right."),
    (9.2, "clau", "Built the quote page. Matches the tests."),
    (10.8, "dex", "Checked Claude's page: one fix needed (nesting)."),
    (12.0, "clau", "Fixed it."),
    (13.2, "lead", "All parts fit together. Done in 3 min 20 s."),
]
STEPS = [("Plan the job", "lead", 0.5, 3.0), ("Second opinion", "dex", 3.0, 4.4),
         ("Sums + tests", "dex", 4.4, 6.0), ("Review", "clau", 6.0, 7.6),
         ("Quote page", "clau", 7.6, 9.2), ("Review + fix", "dex", 9.2, 12.0), ("Final check", "lead", 12.0, 13.2)]
SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def frame(w, h, t, key, start):
    name, p = PALETTES[key]
    s = Screen(w, h, p["bg"])
    el = t - start
    who_col = {"you": p["text"], "lead": p["accent"], "dex": p["cool"], "clau": p["warm"]}
    who_name = {"you": "You", "lead": "Lead", "dex": "Codex", "clau": "Claude"}
    active = next((st for st in STEPS if st[2] <= el < st[3]), None)

    # header: gradient title
    title = "  C L A U D E X  "
    for i, ch in enumerate(title):
        s.put(2 + i, 1, ch, mix(p["warm"], p["cool"], i / len(title)))
    s.put(2 + len(title) + 1, 1, "two brains, one plan", p["dim"])
    label = f"colours: {name}  (press 1 2 3)"
    s.put(w - len(label) - 2, 1, label, p["dim"])

    # the two characters, bobbing; the one working bounces more
    stage_y = 3
    cx = w // 2
    for art, x, colors, me, phase in ((CLAU, cx - 22, {"W": p["warm"], "w": p["warm2"], "e": p["eye"]}, "clau", 0),
                                      (DEX, cx + 8, {"C": p["cool"], "c": p["cool2"], "e": p["eye"],
                                                     "a": p["accent"] if int(t * 3) % 2 else p["cool2"]}, "dex", 1.3)):
        busy = active and active[1] == me
        bob = round(math.sin(t * (6 if busy else 2) + phase) * (1.5 if busy else 0.6))
        blink = (t + phase) % 4 < 0.15
        sprite(s, art, x, stage_y * 2 + 2 + bob, colors, blink)
        label = who_name[me] + ("  working" if busy else "")
        s.put(x + 7 - len(who_name[me]) // 2, stage_y + 7, label, who_col[me] if busy else p["dim"])

    # a spark carries the work between them (the hand-off), along an arc
    if active and active[1] in ("clau", "dex"):
        k = ((el - active[2]) / (active[3] - active[2]))
        a, b = (cx - 8, cx + 8) if active[1] == "dex" else (cx + 8, cx - 8)
        x = int(a + (b - a) * min(k * 2, 1))
        yy = stage_y * 2 + 6 - int(math.sin(min(k * 2, 1) * math.pi) * 5)
        for trail in range(4):
            s.pixel(x - trail * (1 if b > a else -1), yy, mix(p["accent"], p["bg"], trail / 4))
    if active and active[1] == "lead":
        sp = SPIN[int(t * 10) % len(SPIN)]
        s.put(cx - 1, stage_y + 2, sp + " " + sp, p["accent"])

    # panels
    top = stage_y + 9
    ph = h - top - 4
    lw = max(30, int(w * 0.64))
    box(s, 2, top, lw, ph, p, "Conversation")
    box(s, lw + 3, top, w - lw - 5, ph, p, "Plan")
    lines = [(who, txt) for at, who, txt in SCRIPT if el >= at]
    y = top + 1
    for who, txt in lines[-(ph - 2) // 2:]:
        s.put(4, y, "● " + who_name[who], who_col[who])
        s.put(14, y, txt[: lw - 16], p["text"])
        y += 2
    for i, (title_, who, a, b) in enumerate(STEPS):
        yy = top + 1 + i * 2
        if yy >= top + ph - 1:
            break
        if el >= b:
            mark, col = "✓", p["good"]
        elif el >= a:
            mark, col = SPIN[int(t * 10) % len(SPIN)], who_col[who]
        else:
            mark, col = "·", p["dim"]
        s.put(lw + 5, yy, mark, col)
        s.put(lw + 7, yy, title_[: w - lw - 18], p["text"] if el >= a else p["dim"])
        s.put(w - 10, yy, who_name[who][:6], who_col[who] if el >= a else p["dim"])

    # input box and status chips
    box(s, 2, h - 4, w - 4, 3, p)
    cursor = "▌" if int(t * 2) % 2 else " "
    s.put(4, h - 3, "›", p["accent"])
    s.put(6, h - 3, "Ask anything · type / for commands and skills" + cursor, p["dim"])
    chips = [("Auto", p["accent"]), ("checks on", p["good"]), ("space: replay · q: quit", p["dim"])]
    x = w - 2
    for txt, col in reversed(chips):
        x -= len(txt) + 3
        s.put(x, h - 1, f" {txt} ", p["bg"], col)
    return s.render()


def main():
    if not sys.stdout.isatty():
        print("claudex demo needs a terminal.")
        return 1
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    sys.stdout.write("\033[?1049h\033[?25l")  # full screen (alternate screen), hide cursor
    key, start = "1", time.time()
    try:
        tty.setcbreak(fd)
        while True:
            w, h = shutil.get_terminal_size((100, 32))
            if w < 70 or h < 26:
                sys.stdout.write("\033[2J\033[H" + "Make the window a bit bigger (70 × 26 or more)…")
            else:
                sys.stdout.write(frame(w, h, time.time(), key, start))
            sys.stdout.flush()
            if select.select([sys.stdin], [], [], 0.08)[0]:
                ch = os.read(fd, 1).decode(errors="ignore")
                if ch in ("q", "\x1b"):
                    break
                if ch in PALETTES:
                    key = ch
                if ch == " ":
                    start = time.time()
            if time.time() - start > 17:
                start = time.time()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        sys.stdout.write("\033[0m\033[?25h\033[?1049l")
        sys.stdout.flush()
    return 0
