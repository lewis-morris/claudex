"""Claudex full screen: two cheeky monkeys act out the job while you chat.

F2 monkeys (playful/calm/off × big/small/tiny) · F3 layout · F4 colours · PgUp/PgDn scroll · Ctrl-C stop the job · Ctrl-D quit.
Built on prompt_toolkit (already installed); the monkeys are half-block pixels in 24-bit colour.
"""
import asyncio
import json
import math
import os
import pathlib
import random
import re
import subprocess
import time

from prompt_toolkit.application import Application
from prompt_toolkit.data_structures import Point
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import (ConditionalContainer, DynamicContainer, Float, FloatContainer, HSplit, Layout,
                                   VSplit, Window)
from prompt_toolkit.layout.controls import FormattedTextControl, UIContent, UIControl
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.filters import Condition
from prompt_toolkit.styles import DynamicStyle, Style
from prompt_toolkit.widgets import Frame, TextArea

from . import fun
from .cli import ARG_CHOICES, make_completer
from .commands import COMMANDS, expand_skill, load_skills
from .engine import CHECKS, MODES, ROUTES

PREFS = pathlib.Path.home() / ".config/claudex/ui.json"
BLOCK_CHARS = 60_000     # one agent's run of output kept on screen
TOTAL_CHARS = 400_000    # the whole conversation kept on screen (the full record is in .claudex)
LAYOUTS = ["wide", "side", "focus"]
LAYOUT_NAMES = {"wide": "monkeys on top", "side": "monkeys at the side", "focus": "just the chat"}
STYLES = ["playful", "calm", "off"]      # how lively the monkeys are
SIZES = ["big", "small", "tiny"]          # how much room they take
STAGE_ROWS = {"big": 10, "small": 4, "tiny": 1}

PALETTES = {
    "Jungle": {"bg": "#13201a", "panel": "#1a2a22", "edge": "#2e4a3c", "text": "#eef3e6", "dim": "#8fa597",
               "accent": "#ffd23f", "good": "#9be38b", "bad": "#ff7a6b", "branch": "#6b4a2b", "leaf": "#4fae5b",
               "leaf2": "#3a8a46", "fur": "#8a5a3b", "fur2": "#6a4129", "skin": "#e8c39e", "eye": "#1b1410",
               "claude": "#ff8f6b", "codex": "#5fd0e0", "banana": "#ffd23f", "banana2": "#c99a12"},
    "Sunset": {"bg": "#1d1426", "panel": "#261b33", "edge": "#3f2e57", "text": "#f3eafa", "dim": "#9a8bad",
               "accent": "#ffb86b", "good": "#9be38b", "bad": "#ff6b8a", "branch": "#5a3d2e", "leaf": "#c86bd0",
               "leaf2": "#8e4ba0", "fur": "#9a6340", "fur2": "#714427", "skin": "#f0caa4", "eye": "#1d1426",
               "claude": "#ff8f6b", "codex": "#7fd6ff", "banana": "#ffd86b", "banana2": "#d0a43a"},
    "Daylight": {"bg": "#f7f3e8", "panel": "#efe8d6", "edge": "#d2c6a9", "text": "#2a2a30", "dim": "#7c7565",
                 "accent": "#d88a00", "good": "#2f8a3e", "bad": "#c4452f", "branch": "#8a6239", "leaf": "#59a85f",
                 "leaf2": "#3f8446", "fur": "#8a5a3b", "fur2": "#6a4129", "skin": "#e6be97", "eye": "#2a2a30",
                 "claude": "#e0673f", "codex": "#1f8fa3", "banana": "#f2c230", "banana2": "#b88d10"},
}

# ---------- the monkeys (16 x 14 pixels). F fur, f dark fur, S face/hands, e eye, m mouth, T tail, B their colour
BASE = [
    "....FFFFFFFF....",
    "...FFFFFFFFFF...",
    ".SSFFSSSSSSFFSS.",
    ".SSFSSSSSSSSFSS.",
    ".SSFSSeSSeSSFSS.",
    "..FFSSSSSSSSFF..",
    "...FSSSmmSSSF...",
    "....FFSSSSFF....",
    ".....ffffff.....",
    "...FFFFFFFFFF...",
    "..FF.FFFFFF.FF..",
    "..SS.FFFFFF.SS..",
    ".....FFFFFF.....",
    ".....SS..SS.....",
]
TAILS = [[(11, 15), (12, 14), (12, 13)], [(10, 15), (11, 15), (12, 14)], [(12, 15), (13, 14), (13, 13)]]
BANANA = ["....Yy", "YYYYY.", ".YYY.."]
LAPTOP = ["..LLLLLLLLLLLL..", "..LGGGGGGGGGGL..", "..LGGGGGGGGGGL..", ".KKKKKKKKKKKKKK."]


def _grid(rows):
    return [list(r) for r in rows]


def monkey(kind, pose, blink=False, look=0, tail=0, face=None):
    g = _grid(BASE)
    if kind == "claude":            # a bandana with a knot
        for x in range(3, 13):
            g[1][x] = "B"
        g[1][13], g[0][14] = "B", "B"
    else:                           # headphones
        for x in range(5, 11):
            g[0][x] = "B"
        for y in (2, 3, 4):
            g[y][1], g[y][2], g[y][13], g[y][14] = "B", "B", "B", "B"
    for y in range(len(g)):        # eyes: blink or look sideways
        for x in range(len(g[y])):
            if g[y][x] == "e":
                g[y][x] = "S"
    if not blink:
        for x in (6 + look, 9 + look):
            if not (face == "wink" and x == 9 + look):
                g[4][x] = "e"
    if face == "wink":
        g[4][9 + look] = "m"                 # a closed, cheeky eye
    if face == "tongue":
        g[7][7], g[7][8] = "t", "t"
    if pose == "up":                # arms up: cheering, scratching, thumbs up
        for y, x in ((10, 2), (10, 3), (10, 12), (10, 13), (11, 2), (11, 3), (11, 12), (11, 13)):
            g[y][x] = "."
        for y, x in ((7, 2), (8, 2), (7, 13), (8, 13), (9, 3), (9, 12)):
            g[y][x] = "F"
        g[6][1], g[6][2], g[6][13], g[6][14] = "S", "S", "S", "S"
    elif pose == "scratch":         # one hand on the head
        g[10][12] = g[10][13] = g[11][12] = g[11][13] = "."
        g[9][13], g[8][14], g[7][14] = "F", "F", "F"
        g[6][14] = g[5][14] = "S"
    elif pose in ("typeA", "typeB"):  # hands tapping away
        if pose == "typeB":
            g[11][2] = g[11][3] = "."
            g[10][2] = g[10][3] = "S"
        else:
            g[11][12] = g[11][13] = "."
            g[10][12] = g[10][13] = "S"
    elif pose == "droop":
        g[6][7], g[6][8] = "S", "S"
        g[7][7], g[7][8] = "m", "m"
    for y, x in TAILS[tail % len(TAILS)]:
        if g[y][x] == ".":
            g[y][x] = "T"
    return g


MINI = [
    "...FFFF...",
    "..FFFFFF..",
    ".SFSSSSFS.",
    ".SFeSSeFS.",
    "..FSmmSF..",
    "...FFFF...",
    "...S..S...",
]


def mini_monkey(kind, pose, blink=False, look=0, face=None, tail=0):
    g = _grid(MINI)
    if kind == "claude":
        for x in range(2, 8):
            g[1][x] = "B"
        g[0][8] = "B"
    else:
        for x in range(3, 7):
            g[0][x] = "B"
        g[2][1] = g[3][1] = g[2][8] = g[3][8] = "B"
    for x in (3, 6):
        g[3][x] = "S"
    if not blink:
        for x in (3 + look, 6 + look):
            if not (face == "wink" and x == 6 + look):
                g[3][x] = "e"
    if face == "wink":
        g[3][6 + look] = "m"
    if face == "tongue":
        g[4][4], g[4][5] = "t", "t"
    if pose == "up":
        g[0][0], g[1][1], g[0][9], g[1][8] = "S", "F", "S", "F"
    elif pose == "scratch":
        g[0][8], g[1][8] = "S", "F"
    elif pose == "droop":
        g[4][4], g[4][5] = "S", "S"
    elif pose in ("typeA", "typeB"):
        for x in range(2, 8):
            g[5][x] = "G" if x % 2 else "K"
        g[5][1 if pose == "typeA" else 8] = "S"
    for y, x in ((5, 8), (6, 9)) if tail else ((4, 9), (5, 9)):
        if g[y][x] == ".":
            g[y][x] = "T"
    return g


class Canvas:
    """Text cells that can also hold two stacked pixels (▀ with fg on top and bg below)."""

    def __init__(self, w, h, bg):
        self.w, self.h, self.bg = w, h, bg
        self.cells = [[[" ", bg, bg] for _ in range(w)] for _ in range(h)]

    def pixel(self, x, py, color):
        y = py // 2
        if not (0 <= x < self.w and 0 <= y < self.h) or not color:
            return
        c = self.cells[y][x]
        top, bot = (c[1], c[2]) if c[0] == "▀" else (c[2], c[2])
        if py % 2 == 0:
            top = color
        else:
            bot = color
        self.cells[y][x] = ["▀", top, bot]

    def text(self, x, y, s, color):
        for i, ch in enumerate(s):
            if 0 <= x + i < self.w and 0 <= y < self.h:
                self.cells[y][x + i] = [ch, color, self.cells[y][x + i][2]]

    def sprite(self, g, x, py, colors):
        for r, row in enumerate(g):
            for c, k in enumerate(row):
                if k != ".":
                    self.pixel(x + c, py + r, colors.get(k))

    def fragments(self):
        lines = []
        for row in self.cells:
            frags, cur, buf = [], None, []
            for ch, f, b in row:
                style = f"fg:{f} bg:{b}"
                if style != cur and buf:
                    frags.append((cur, "".join(buf)))
                    buf = []
                cur = style
                buf.append(ch)
            if buf:
                frags.append((cur, "".join(buf)))
            lines.append(frags)
        return lines


class StageControl(UIControl):
    def __init__(self, tui):
        self.tui = tui

    def create_content(self, width, height):
        lines = self.tui.draw_stage(width, height)
        return UIContent(get_line=lambda i: lines[i] if i < len(lines) else [], line_count=len(lines))

    def is_focusable(self):
        return False


def _md(text, base):
    """**bold** and _quiet_ lines into prompt_toolkit fragments."""
    out = []
    for line in text.split("\n"):
        # _quiet notes_ (anywhere a word starts) and **bold**
        for j, chunk in enumerate(re.split(r"(?:(?<=\s)|^)_(.+?)_(?=\s|$|[.,:;!?])", line)):
            if not chunk:
                continue
            if j % 2:
                out.append((base + " class:dim italic", chunk))
                continue
            for i, part in enumerate(re.split(r"\*\*(.+?)\*\*", chunk)):
                if part:
                    out.append((base + (" bold" if i % 2 else ""), part))
        out.append(("", "\n"))
    if out:
        out.pop()
    return out


class TUI:
    NAMES = {"you": "You", "lead": "Lead", "claude": "Claude", "codex": "Codex", "sys": "Claudex"}

    def __init__(self, eng):
        self.eng = eng
        self.prefs = self._load_prefs()
        self.blocks = []            # transcript: {"who", "text"}
        self.steps = []           # the lead's plan, as shown in the Plan pane
        self.busy = None            # the running job's task
        self.speaker = "sys"
        self.state = {"who": None, "state": "idle", "target": None, "since": time.time()}
        self.banana = None          # (from, to, started)
        self.confetti = []
        self.scroll_back = 0
        self.questions = []         # permission questions waiting for an answer, oldest first: (future, options)
        self.cmd_task = None        # a running /doctor, /rules or /log
        self.stopping = False       # Ctrl-C pressed: refuse any permission question that arrives late
        self._cache = (None, None)  # (version, fragments) so long transcripts aren't re-parsed every frame
        self.version = 0
        self.skills = load_skills()
        self.t0 = time.time()
        self.app = None

    # ---------- preferences ----------
    def _load_prefs(self):
        try:
            p = json.loads(PREFS.read_text())
        except Exception:
            p = {}
        style = p.get("style") if p.get("style") in STYLES else ("off" if p.get("animate") is False else "playful")
        return {"style": style, "size": p.get("size") if p.get("size") in SIZES else "big",
                "layout": p.get("layout", "wide"),
                "palette": p.get("palette", "Jungle") if p.get("palette") in PALETTES else "Jungle"}

    def _save_prefs(self):
        try:
            PREFS.parent.mkdir(parents=True, exist_ok=True)
            PREFS.write_text(json.dumps(self.prefs))
        except Exception:
            pass

    @property
    def moving(self):
        return self.prefs["style"] != "off"

    @property
    def playful(self):
        return self.prefs["style"] == "playful"

    def set_look(self, style=None, size=None):
        if style in STYLES:
            self.prefs["style"] = style
        if size in SIZES:
            self.prefs["size"] = size
        if not self.playful:
            self.banana, self.confetti = None, []
        self._save_prefs()
        self._refresh()

    def cycle_look(self):
        """F2: steps through all nine: playful big → small → tiny → calm big … → off tiny."""
        combos = [(st, sz) for st in STYLES for sz in SIZES]
        i = combos.index((self.prefs["style"], self.prefs["size"]))
        self.set_look(*combos[(i + 1) % len(combos)])

    def look_name(self):
        return f"{self.prefs['style'].title()} · {self.prefs['size']}"

    @property
    def p(self):
        return PALETTES[self.prefs["palette"]]

    # ---------- what the engine calls ----------
    def _add(self, who, text):
        if self.blocks and self.blocks[-1]["who"] == who:
            self.blocks[-1]["text"] = (self.blocks[-1]["text"] + text)[-BLOCK_CHARS:]
        else:
            self.blocks.append({"who": who, "text": text[-BLOCK_CHARS:]})
        while len(self.blocks) > 1 and (len(self.blocks) > 400
                                         or sum(len(b["text"]) for b in self.blocks) > TOTAL_CHARS):
            self.blocks.pop(0)   # oldest first; the newest block always stays
        self.version += 1
        self._refresh()

    async def say(self, text):
        self._add("sys", text)

    async def plan(self, entries):
        self.steps = [dict(e) for e in entries]
        self._refresh()

    async def update(self, upd):
        kind = upd.get("sessionUpdate")
        who = self.speaker if self.speaker in ("claude", "codex") else "sys"
        if kind == "agent_message_chunk":
            self._add(who, (upd.get("content") or {}).get("text", ""))
        elif kind == "tool_call":
            title = upd.get("title") or upd.get("kind") or "tool"
            self._add(who, f"\n_⏺ {title[:110]}_\n")

    async def permission(self, params):
        if self.stopping or not (self.busy and not self.busy.done()):
            return {"outcome": {"outcome": "cancelled"}}   # no live job to ask for: never carry it forward
        tc = params.get("toolCall") or {}
        opts = params.get("options") or []
        fut = asyncio.get_running_loop().create_future()
        self.questions.append((fut, opts, tc.get("title") or tc.get("kind") or "an action"))
        if len(self.questions) == 1:
            self._show_question()
        try:
            return await fut
        finally:
            self.questions = [q for q in self.questions if q[0] is not fut]

    def _show_question(self):
        if not self.questions:
            return
        _, opts, title = self.questions[0]
        lines = [f"**Permission needed:** {title}"] + [f"  {n}. {o.get('name')}" for n, o in enumerate(opts, 1)]
        lines.append("_Type a number and press Enter._")
        self._add("sys", "\n" + "\n".join(lines) + "\n")

    def _answer(self, line):
        fut, opts, _ = self.questions[0]
        if not (line.isdigit() and 1 <= int(line) <= len(opts)):
            self._add("sys", f"\n_Type a number from 1 to {len(opts)}._\n")
            return
        self.questions.pop(0)
        if not fut.done():
            fut.set_result({"outcome": {"outcome": "selected", "optionId": opts[int(line) - 1]["optionId"]}})
        self._show_question()

    def drop_questions(self):
        """Answer every waiting question with "cancelled" (the job stopped, finished or we're leaving)."""
        qs, self.questions = self.questions, []
        for fut, _, _ in qs:
            if not fut.done():
                fut.set_result({"outcome": {"outcome": "cancelled"}})

    def activity(self, who, state, target=None):
        if who in ("claude", "codex"):
            self.speaker = who
        if state == "reviewing" and target in ("claude", "codex") and self.playful:
            self.banana = (target, who, time.time())        # the work is tossed across to be checked
        if state == "done" and self.playful:
            self._burst()
        self.state = {"who": who, "state": state, "target": target, "since": time.time()}
        self._refresh()

    def _burst(self):
        cols = [self.p[k] for k in ("accent", "claude", "codex", "good", "banana", "leaf")]
        self.confetti = [[random.uniform(0.2, 0.8), random.uniform(0.3, 0.6), random.uniform(-0.5, 0.5),
                          random.uniform(-1.4, -0.6), random.choice(cols)] for _ in range(60)]

    def _refresh(self):
        if self.app:
            self.app.invalidate()

    # ---------- the stage ----------
    def _clock(self):
        t = time.time() - self.t0
        return {"playful": t, "calm": t * 0.5, "off": 0.0}[self.prefs["style"]]

    def draw_stage(self, w, h):
        size = self.prefs["size"]
        if size == "tiny":
            return self._draw_tiny(w, h)
        if size == "small":
            return self._draw_small(w, h)
        return self._draw_big(w, h)

    def _draw_big(self, w, h):
        p, t = self.p, self._clock()
        cv = Canvas(w, h, p["bg"])
        ph = h * 2
        # the branch they sit under, with leaves that sway
        for x in range(w):
            cv.pixel(x, 1, p["branch"])
            if (x * 7) % 5 == 0:
                sway = int(round(math.sin(t * 1.5 + x * 0.4))) if self.moving else 0
                cv.pixel(x + sway, 2, p["leaf"] if x % 2 else p["leaf2"])
                if (x * 3) % 4 == 0:
                    cv.pixel(x + sway, 3, p["leaf2"])
        st = self.state
        cx = w // 2
        spots = {"claude": max(1, cx - 22), "codex": min(w - 17, cx + 6)}
        ground = ph - 16   # feet just above the name row
        for kind, x in spots.items():
            pose, bob, blink, look, bubble = "idle", 0, False, 0, None
            tail = int(t * 2.5 + (0 if kind == "claude" else 1.3)) % 3
            if self.moving:
                bob = round(math.sin(t * 2 + (0 if kind == "claude" else 1.7)) * 0.6)
                blink = (time.time() + (0 if kind == "claude" else 1.1)) % 3.7 < 0.18
            other = "codex" if kind == "claude" else "claude"
            mine, who, s = st["who"] == kind, st["who"], st["state"]
            age = time.time() - st["since"]
            if s == "thinking":                                     # the lead is planning: both scratch heads
                pose = "scratch" if int(t * 2 + (kind == "codex")) % 2 else "idle"
                bubble = ("?", p["accent"])
            elif s == "working":
                if mine:
                    pose = "typeA" if int(t * 8) % 2 else "typeB"
                    bob = round(abs(math.sin(t * 9)) * -1.2) if self.playful else 0
                    bubble = ("*" if int(t * 4) % 2 else "+", p[kind])
                else:
                    look = 1 if kind == "claude" else -1             # watching the other one work
            elif s in ("reviewing", "checking") and mine:
                look = 1 if kind == "claude" else -1
                bubble = ("🔍" if False else "◎", p["accent"])
                pose = "scratch" if int(t * 1.5) % 2 else "idle"
            elif s == "pass" and mine and age < 2.5:
                pose, bubble = "up", ("✓", p["good"])
            elif s == "fix" and mine and age < 2.5:
                bubble = ("!", p["bad"])
            elif s == "done" and age < 4:
                pose = "up"
                bob = -round(abs(math.sin(t * 7)) * 3) if self.playful else 0
                bubble = ("♪", p["accent"])
            elif s == "stopped" and age < 6:
                pose, bubble = "droop", ("…", p["dim"])
                bob = 1
            face = None
            if self.playful and s in ("idle", "done", "pass", "working") and not (s == "working" and mine):
                mood = (int(t / 2.5) + (2 if kind == "codex" else 0)) % 7   # every so often, something cheeky
                if mood == 1:
                    face = "wink"
                elif mood == 3:
                    face = "tongue"
                elif mood == 5 and s == "idle":
                    pose = "scratch"
                elif mood == 6:
                    look = 1 if kind == "claude" else -1
            g = monkey(kind, pose, blink, look, tail, face)
            colors = {"F": p["fur"], "f": p["fur2"], "S": p["skin"], "e": p["eye"], "m": p["fur2"],
                      "T": p["fur2"], "B": p[kind], "t": p["bad"]}
            cv.sprite(g, x, ground + bob, colors)
            if s == "working" and mine:                                # tapping away at a little laptop
                glow = [_mix(p[kind], p["bg"], 0.15 + 0.5 * random.random()) if self.playful
                        else p[kind]]
                cv.sprite([list(r) for r in LAPTOP], x, ground + 9,
                          {"L": p["edge"], "G": glow[0], "K": p["dim"]})
                for hx in ((3, 12) if int(t * 8) % 2 else (5, 10)):
                    cv.pixel(x + hx, ground + 12 - (int(t * 8 + hx) % 2), p["skin"])
            label = self.NAMES[kind]
            busy_label = {"working": " is working", "reviewing": " is checking", "checking": " is checking"}
            if mine and s in busy_label:
                label += busy_label[s]
            cv.text(x + 8 - len(label) // 2, h - 1, label, p[kind] if mine else p["dim"])
            if bubble:
                cv.text(x + 15, (ground + bob) // 2, bubble[0], bubble[1])
        # the banana: work tossed across to be checked
        if self.banana and self.playful:
            a, b, started = self.banana
            k = (time.time() - started) / 1.2
            if k >= 1:
                self.banana = None
            else:
                x0, x1 = spots[a] + 12, spots[b] + 2
                x = int(x0 + (x1 - x0) * k)
                y = int(ground + 4 - math.sin(k * math.pi) * (ground + 2))
                cv.sprite([list(r) for r in BANANA], x, max(0, y), {"Y": p["banana"], "y": p["banana2"]})
        # confetti when a job is done
        self._confetti(cv, w, ph)
        if st["state"] == "thinking" and st["who"] == "lead":
            sp = self._spin()
            cv.text(cx - 6, max(0, h - 3), f"{sp} planning {sp}", p["accent"])
        return cv.fragments()

    def _spin(self):
        return "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"[int(time.time() * (10 if self.playful else 4)) % 10] if self.moving else "…"

    def _confetti(self, cv, w, ph):
        if not (self.confetti and self.playful):
            return
        alive = []
        for c in self.confetti:
            c[0] += c[2] * 0.02
            c[1] += c[3] * 0.04
            c[3] += 0.12
            if 0 <= c[1] <= 1:
                cv.pixel(int(c[0] * w), int(c[1] * ph), c[4])
                alive.append(c)
        self.confetti = alive

    def _mood(self, kind):
        """What a monkey is up to right now: (pose, face, look, bubble, bob) for any size."""
        p, t, st = self.p, self._clock(), self.state
        mine, s = st["who"] == kind, st["state"]
        age = time.time() - st["since"]
        pose, face, look, bubble, bob = "idle", None, 0, None, 0
        if self.moving:
            bob = round(math.sin(t * 2 + (0 if kind == "claude" else 1.7)) * 0.6)
        if s == "thinking":
            pose = "scratch" if int(t * 2 + (kind == "codex")) % 2 else "idle"
            bubble = ("?", p["accent"])
        elif s == "working":
            if mine:
                pose = ("typeA" if int(t * 8) % 2 else "typeB") if self.moving else "typeA"
                bubble = ("*" if int(t * 4) % 2 else "+", p[kind])
            else:
                look = 1 if kind == "claude" else -1
        elif s in ("reviewing", "checking") and mine:
            look, bubble = (1 if kind == "claude" else -1), ("◎", p["accent"])
        elif s == "pass" and mine and age < 2.5:
            pose, bubble = "up", ("✓", p["good"])
        elif s == "fix" and mine and age < 2.5:
            bubble = ("!", p["bad"])
        elif s == "done" and age < 4:
            pose, bubble = "up", ("♪", p["accent"])
        elif s == "stopped" and age < 6:
            pose, bubble = "droop", ("…", p["dim"])
        if self.playful and s in ("idle", "done", "pass", "working") and not (s == "working" and mine):
            m = (int(t / 2.5) + (2 if kind == "codex" else 0)) % 7
            face = {1: "wink", 3: "tongue"}.get(m)
            if m == 5 and s == "idle":
                pose = "scratch"
            if m == 6:
                look = 1 if kind == "claude" else -1
        blink = self.moving and (time.time() + (0 if kind == "claude" else 1.1)) % 3.7 < 0.18
        return pose, face, look, bubble, bob, blink

    def _label(self, kind):
        st = self.state
        words = {"working": "working", "reviewing": "checking", "checking": "checking"}
        return words.get(st["state"]) if st["who"] == kind else None

    def _draw_small(self, w, h):
        """Half-height monkeys with their name and what they're doing beside them."""
        p, t = self.p, self._clock()
        cv = Canvas(w, h, p["bg"])
        ph = h * 2
        for x in range(w):
            cv.pixel(x, 0, p["branch"])
            if (x * 7) % 9 == 0:
                cv.pixel(x + (int(round(math.sin(t * 1.5 + x))) if self.moving else 0), 1, p["leaf2"])
        cx = w // 2
        spots = {"claude": max(0, cx - 24), "codex": min(w - 24, cx + 2)}
        for kind, x in spots.items():
            pose, face, look, bubble, bob, blink = self._mood(kind)
            g = mini_monkey(kind, pose, blink, look, face, tail=int(t * 2.5 + (kind == "codex")) % 2)
            colors = {"F": p["fur"], "f": p["fur2"], "S": p["skin"], "e": p["eye"], "m": p["fur2"],
                      "T": p["fur2"], "B": p[kind], "t": p["bad"], "G": p[kind], "K": p["dim"]}
            cv.sprite(g, x, 1 + max(-1, min(0, bob)), colors)
            mine = self.state["who"] == kind
            cv.text(x + 11, 1, self.NAMES[kind], p[kind] if mine else p["dim"])
            doing = self._label(kind)
            extra = (bubble[0] + " " if bubble else "") + (doing or "")
            if extra.strip():
                cv.text(x + 11, 2, extra.strip(), bubble[1] if bubble else p[kind])
        if self.banana and self.playful:
            a, b, started = self.banana
            k = (time.time() - started) / 1.0
            if k >= 1:
                self.banana = None
            else:
                x0, x1 = spots[a] + 8, spots[b] + 2
                cv.sprite([list("YYy"), list(".YY")], int(x0 + (x1 - x0) * k), int(4 - math.sin(k * math.pi) * 3),
                          {"Y": p["banana"], "y": p["banana2"]})
        self._confetti(cv, w, ph)
        if self.state["state"] == "thinking" and self.state["who"] == "lead":
            cv.text(cx - 5, h - 1, f"{self._spin()} planning", p["accent"])
        return cv.fragments()

    def _draw_tiny(self, w, h):
        """One line: a little face for each, and what they're doing."""
        p = self.p
        frags = [("", " ")]
        for kind in ("claude", "codex"):
            pose, face, look, bubble, bob, blink = self._mood(kind)
            eyes = ("-", "-") if blink else ("•", "-") if face == "wink" else ("•", "•")
            mouth = "P" if face == "tongue" else "_" if pose == "droop" or (bubble and bubble[0] in "?!") else "‿"
            fc = f"({eyes[0]}{mouth}{eyes[1]})"
            if pose == "up":
                fc = "\\" + fc[1:-1] + "/"
            mine = self.state["who"] == kind
            frags.append((f"fg:{p[kind]} bold" if mine else f"fg:{p[kind]}", fc))
            frags.append((f"fg:{p[kind] if mine else p['dim']}", f" {self.NAMES[kind]}"))
            doing = self._label(kind)
            if doing:
                dots = "." * (1 + int(time.time() * 3) % 3) if self.moving else "…"
                frags.append((f"fg:{p[kind]}", f" {doing}{dots:<3}"))
            if bubble:
                frags.append((f"fg:{bubble[1]}", f" {bubble[0]}"))
            frags.append(("", "     "))
        if self.state["state"] == "thinking" and self.state["who"] == "lead":
            frags.append((f"fg:{p['accent']}", f"{self._spin()} the lead is planning"))
        clipped, used = [], 0
        for st, x in frags:                      # fit the window exactly, however narrow
            x = x[:max(0, w - used)]
            if x:
                clipped.append((f"bg:{p['bg']} {st}".strip(), x))
                used += len(x)
        frags = clipped
        if used < w:
            frags.append((f"bg:{p['bg']}", " " * (w - used)))
        return [frags] + [[(f"bg:{p['bg']}", " " * w)] for _ in range(h - 1)]

    # ---------- panes ----------
    def transcript(self):
        if self._cache[0] == self.version:
            return self._cache[1]
        frags = self._transcript()
        self._cache = (self.version, frags)
        return frags

    def _transcript(self):
        frags = []
        for b in self.blocks:
            who = b["who"]
            if who != "sys":
                frags.append((f"class:who.{who} bold", f"● {self.NAMES[who]}\n"))
            frags.extend(_md(b["text"].strip("\n"), "class:body" if who != "sys" else "class:sys"))
            frags.append(("", "\n\n"))
        if not frags:
            frags = [("class:dim", "  Ask for anything. Type / for commands and skills.\n")]
        return frags

    def cursor(self):
        lines = sum(t.count("\n") for _, t in self.transcript())
        return Point(0, max(0, lines - self.scroll_back))

    def plan_text(self):
        if not self.steps:
            return [("class:dim", "No plan yet.\n\nWhen you ask for something,\nthe lead's plan shows here.")]
        out = []
        spin = self._spin() if self.moving else "▸"
        for e in self.steps:
            mark, style = {"completed": ("✓", "class:good"), "in_progress": (spin, "class:accent")}.get(
                e["status"], ("·", "class:dim"))
            out += [(style, f"{mark} "), ("class:body" if e["status"] != "pending" else "class:dim",
                                          e["content"] + "\n\n")]
        return out

    def header(self):
        title = "C L A U D E X"
        p = self.p
        frags = [("", " ")]
        for i, ch in enumerate(title):
            frags.append((f"fg:{_mix(p['claude'], p['codex'], i / len(title))} bold", ch))
        frags.append(("class:dim", "   two cheeky monkeys, one plan"))
        return frags

    def status(self):
        e = self.eng
        if self.questions:
            left = " Answer the permission question: type its number "
        elif self.busy and not self.busy.done():
            left = " Working… Ctrl-C to stop "
        else:
            left = " Ready "
        right = (f" {ROUTES[e.route]} · effort {e.effort} · checks {CHECKS[e.checks]}"
                 + (" · PLAN ONLY" if e.dry_run else "")
                 + f" · F2 monkeys {self.look_name()} · F3 {LAYOUT_NAMES[self.prefs['layout']]}"
                 + f" · F4 {self.prefs['palette']} ")
        return [("class:chip", left), ("class:status", right)]

    # ---------- layout ----------
    def build(self):
        self.input = TextArea(height=1, prompt=[("class:accent bold", "› ")], multiline=False,
                              completer=make_completer(self.skills), complete_while_typing=True,
                              accept_handler=self.on_accept, style="class:input")
        self.stage = Window(StageControl(self), height=lambda: STAGE_ROWS[self.prefs["size"]], style="class:bg")
        self.chat = Window(FormattedTextControl(self.transcript, get_cursor_position=self.cursor, show_cursor=False),
                           wrap_lines=True, style="class:panel")
        self.plan_win = Window(FormattedTextControl(self.plan_text), wrap_lines=True, style="class:panel")

        def body():
            lay = self.prefs["layout"]
            chat = Frame(self.chat, title="Conversation", style="class:frame")
            plan = Frame(self.plan_win, title="Plan", style="class:frame", width=44)
            if lay == "side":
                return VSplit([HSplit([Window(StageControl(self), height=lambda: STAGE_ROWS[self.prefs["size"]], width=44,
                                             style="class:bg"), plan],
                                      width=46), chat])
            if lay == "focus":
                return chat
            return HSplit([self.stage, VSplit([chat, plan])])

        root = HSplit([
            Window(FormattedTextControl(self.header), height=1, style="class:bg"),
            DynamicContainer(body),
            Frame(self.input, style="class:frame"),
            Window(FormattedTextControl(self.status), height=1, style="class:status"),
        ], style="class:bg")
        root = FloatContainer(root, floats=[Float(xcursor=True, ycursor=True,
                                                  content=CompletionsMenu(max_height=14, scroll_offset=1))])
        kb = KeyBindings()

        @kb.add("c-c")
        def _(event):
            if self.busy and not self.busy.done():
                self._add("sys", "\n_Stopping…_\n")
                self.stopping = True
                self.drop_questions()
                self.eng.cancel()
            elif self.cmd_task and not self.cmd_task.done():
                self.cmd_task.cancel()
            else:
                self.input.text = ""

        @kb.add("c-d")
        def _(event):
            event.app.exit()

        @kb.add("f2")
        def _(event):
            self.cycle_look()

        @kb.add("f3")
        def _(event):
            self.prefs["layout"] = LAYOUTS[(LAYOUTS.index(self.prefs["layout"]) + 1) % len(LAYOUTS)]
            self._save_prefs()

        @kb.add("f4")
        def _(event):
            names = list(PALETTES)
            self.prefs["palette"] = names[(names.index(self.prefs["palette"]) + 1) % len(names)]
            self._save_prefs()

        @kb.add("pageup")
        def _(event):
            self.scroll_back += 12

        @kb.add("pagedown")
        def _(event):
            self.scroll_back = max(0, self.scroll_back - 12)

        from prompt_toolkit.output import ColorDepth
        depth = ColorDepth.TRUE_COLOR if os.environ.get("COLORTERM", "").lower() in ("truecolor", "24bit") \
            or os.environ.get("CLAUDEX_TRUECOLOR") else None
        self.app = Application(layout=Layout(root, focused_element=self.input), key_bindings=kb, full_screen=True,
                               style=DynamicStyle(self.style), mouse_support=False, color_depth=depth)
        return self.app

    def style(self):
        p = self.p
        return Style.from_dict({
            "bg": f"bg:{p['bg']} {p['text']}", "panel": f"bg:{p['panel']} {p['text']}",
            "frame": f"bg:{p['panel']} {p['edge']}", "frame.label": f"{p['dim']} bold",
            "frame.border": p["edge"], "input": f"bg:{p['panel']} {p['text']}",
            "dim": p["dim"], "accent": p["accent"], "good": p["good"], "body": p["text"], "sys": p["dim"],
            "who.you": f"{p['text']}", "who.lead": p["accent"], "who.claude": p["claude"],
            "who.codex": p["codex"], "status": f"bg:{p['bg']} {p['dim']}", "chip": f"bg:{p['accent']} {p['bg']} bold",
            "completion-menu": f"bg:{p['panel']} {p['text']}",
            "completion-menu.completion.current": f"bg:{p['accent']} {p['bg']}",
            "completion-menu.meta.completion": f"bg:{p['panel']} {p['dim']}",
            "completion-menu.meta.completion.current": f"bg:{p['accent']} {p['bg']}",
        })

    # ---------- input ----------
    def on_accept(self, buff):
        line = buff.text.strip()
        if not line:
            return False
        if self.questions:
            self._answer(line)
            return False
        if (self.busy and not self.busy.done()) or (self.cmd_task and not self.cmd_task.done()):
            self._add("sys", "\n_Still working on the last one. Ctrl-C stops it._\n")
            return False
        self.scroll_back = 0
        if line.startswith("/"):
            skill = expand_skill(line, self.skills)
            if skill:
                line = (f"@{skill[1]} " if skill[1] else "") + skill[0]
            else:
                self.cmd_task = asyncio.ensure_future(self.command(line))
                return False
        self.blocks.append({"who": "you", "text": line})
        self.version += 1
        self.busy = asyncio.ensure_future(self.run(line))
        return False

    async def run(self, line):
        self.speaker = "sys"
        try:
            await self.eng.turn(line)
        except Exception as e:
            self._add("sys", f"\n**Something went wrong:** {e}\n")
            self.activity(None, "stopped")
        finally:
            self.drop_questions()   # nothing from this job may answer later
            self.stopping = False
            self._refresh()

    async def command(self, line):
        cmd, _, arg = line[1:].partition(" ")
        arg, e = arg.strip(), self.eng
        if cmd in ("quit", "exit"):
            self.app.exit()
        elif cmd in ("help", ""):
            rows = [f"**/{n}** {h}  _{d}_" if h else f"**/{n}**  {d}" for n, h, d in COMMANDS]
            rows.append("**@claude …** or **@codex …**  send just that message to one agent")
            rows.append("**F2** monkeys: playful/calm/off × big/small/tiny (or **/anim calm small**) · **F3** layout · "
                        "**F4** colours · **PgUp/PgDn** scroll · **Ctrl-C** stop")
            rows.append(f"\n**Skills** ({len(self.skills)}): type / and start typing a name.")
            self._add("sys", "\n" + "\n".join(rows) + "\n")
        elif cmd in ("auto", "claude", "codex"):
            e.route = cmd
        elif cmd in ARG_CHOICES:
            if arg in ARG_CHOICES[cmd]:
                setattr(e, cmd, arg)
            else:
                self._add("sys", f"\n_Choose one of: {' '.join(ARG_CHOICES[cmd])}_\n")
        elif cmd == "dry":
            e.dry_run = not e.dry_run
        elif cmd == "anim":
            words = arg.lower().split()
            style = next((x for x in words if x in STYLES), None)
            size = next((x for x in words if x in SIZES), None)
            if not (style or size):
                self._add("sys", f"\n_Monkeys: {self.look_name()}. Try /anim calm small, /anim tiny, /anim off "
                                 "(styles: playful, calm, off · sizes: big, small, tiny). F2 steps through all nine._\n")
            else:
                self.set_look(style, size)
        elif cmd == "status":
            self._add("sys", f"\nRouting {ROUTES[e.route]} · effort {e.effort} · mode {MODES[e.mode][0]} · "
                             f"checks {CHECKS[e.checks]}\nNotes: {e.notes}\n")
        elif cmd in ("rules", "doctor", "log"):
            self._add("sys", f"\n_Running /{cmd}…_\n")
            me = pathlib.Path(__file__).resolve().parent.parent / "bin/claudex"
            env = dict(os.environ, **({"CLAUDEX_RULES": os.path.abspath(e.rules_path)} if e.rules_path else {}))
            args = [str(me), cmd] + (["--cwd", e.cwd] if cmd == "log" else [])
            proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                        stderr=asyncio.subprocess.STDOUT, env=env,
                                                        start_new_session=True)
            try:
                out, _ = await proc.communicate()
            except asyncio.CancelledError:
                try:
                    os.killpg(proc.pid, 9)
                except ProcessLookupError:
                    pass
                await proc.wait()
                self._add("sys", f"\n_/{cmd} stopped._\n")
                return
            self._add("sys", "\n" + out.decode(errors="replace") + "\n")
        elif cmd == "memory":
            for title, path in (("Project map", e.map), ("Past decisions", e.decisions),
                                ("Recent jobs", e.journal)):
                body = path.read_text()[-3000:] if path.exists() else "_(nothing yet)_"
                self._add("sys", f"\n**{title}**\n{body}\n")
        else:
            self._add("sys", "\n_No such command or skill. Type / to see them._\n")
        self._refresh()

    async def animate(self):
        while True:
            await asyncio.sleep({"playful": 0.08, "calm": 0.2, "off": 0.5}[self.prefs["style"]])
            self._refresh()


def _mix(a, b, t):
    a, b = a.lstrip("#"), b.lstrip("#")
    ca = [int(a[i:i + 2], 16) for i in (0, 2, 4)]
    cb = [int(b[i:i + 2], 16) for i in (0, 2, 4)]
    return "#" + "".join("%02x" % int(x + (y - x) * t) for x, y in zip(ca, cb))


async def run_tui(eng):
    tui = TUI(eng)
    eng.ui = tui
    tui.blocks.append({"who": "sys", "text": f"_{fun.pick(fun.HELLO)}_\n_{eng.cwd}_"})
    app = tui.build()
    ticker = asyncio.ensure_future(tui.animate())
    try:
        await app.run_async()
    finally:
        ticker.cancel()
        tui.drop_questions()
        if tui.cmd_task and not tui.cmd_task.done():
            tui.cmd_task.cancel()
            try:
                await tui.cmd_task
            except BaseException:
                pass
        if tui.busy and not tui.busy.done():
            eng.cancel()
            try:
                await tui.busy
            except Exception:
                pass
    print(fun.pick(fun.BYE))
    return 0
