"""Claudex in the terminal: a chat like claude or codex, with a `/` menu of commands and skills."""
import asyncio
import os
import pathlib
import re
import signal
import subprocess
import sys

from . import fun
from .commands import COMMANDS, expand_skill, load_skills
from .engine import CHECKS, EFFORTS, MODES, ROUTES, Engine

TTY = sys.stdout.isatty()
DIM, BOLD, GREEN, RED, CYAN, MAGENTA, RESET = (("\033[2m", "\033[1m", "\033[32m", "\033[31m", "\033[36m",
                                                "\033[35m", "\033[0m") if TTY else ("",) * 7)
ARG_CHOICES = {"effort": EFFORTS, "mode": list(MODES), "checks": list(CHECKS)}
ANIM_CHOICES = ["playful", "calm", "off", "big", "small", "tiny"]
HISTORY = pathlib.Path.home() / ".cache/claudex/history"


def pretty(text):
    """Light markdown for the terminal: **bold**, _quiet notes_."""
    if not TTY:
        return text
    text = re.sub(r"\*\*(.+?)\*\*", lambda m: BOLD + m.group(1) + RESET, text)
    return re.sub(r"(?m)^_(.+?)_(\s*)$", lambda m: DIM + m.group(1) + RESET + m.group(2), text)


class TerminalUI:
    def __init__(self):
        self.tools = {}
        self.thinking = False

    async def update(self, upd):
        kind = upd.get("sessionUpdate")
        if kind == "agent_message_chunk":
            self.thinking = False
            print((upd.get("content") or {}).get("text", ""), end="", flush=True)
        elif kind == "agent_thought_chunk":
            if not self.thinking:
                print(f"{DIM}… thinking{RESET}", flush=True)
                self.thinking = True
        elif kind == "tool_call":
            self.thinking = False
            title = upd.get("title") or upd.get("kind") or "tool"
            self.tools[upd.get("toolCallId")] = title
            print(f"\n{CYAN}⏺ {title}{RESET}", flush=True)
        elif kind == "tool_call_update":
            st = upd.get("status")
            if st in ("completed", "failed"):
                mark = f"{GREEN}✓{RESET}" if st == "completed" else f"{RED}✗{RESET}"
                title = upd.get("title") or self.tools.get(upd.get("toolCallId"), "tool")
                print(f"  {mark} {DIM}{title}{RESET}", flush=True)

    async def say(self, text):
        print(pretty(text), end="", flush=True)

    async def plan(self, entries):
        if all(e["status"] == "pending" for e in entries):
            print(f"{BOLD}Plan{RESET}")
            for n, e in enumerate(entries, 1):
                print(f"  {MAGENTA}{n}.{RESET} {e['content']}")
            print()

    async def permission(self, params):
        tc = params.get("toolCall") or {}
        opts = params.get("options") or []
        print(f"\n{BOLD}Permission needed:{RESET} {tc.get('title') or tc.get('kind') or 'an action'}")
        if not sys.stdin.isatty():  # nobody to ask (claudex -p in a script): refuse, don't hang or crash
            deny = next((o for o in opts if o.get("kind", "").startswith("reject")), None)
            print(f"{DIM}  (no one to ask, so refused){RESET}")
            if deny:
                return {"outcome": {"outcome": "selected", "optionId": deny["optionId"]}}
            return {"outcome": {"outcome": "cancelled"}}
        for n, o in enumerate(opts, 1):
            print(f"  {n}. {o.get('name')}")
        while True:
            ans = await asyncio.get_running_loop().run_in_executor(None, input, "Choose a number: ")
            if ans.strip().isdigit() and 1 <= int(ans) <= len(opts):
                return {"outcome": {"outcome": "selected", "optionId": opts[int(ans) - 1]["optionId"]}}


# ---------- the `/` menu ----------
def skill_tag(s):
    return "".join({"claude": "C", "codex": "X"}[a] for a in s["agents"])


def make_completer(skills):
    from prompt_toolkit.completion import Completer, Completion

    class SlashMenu(Completer):
        def get_completions(self, document, complete_event):
            text = document.text_before_cursor
            if text.startswith("@") and " " not in text:
                for who in ("claude", "codex"):
                    if who.startswith(text[1:].lower()):
                        yield Completion("@" + who + " ", -len(text), display="@" + who,
                                         display_meta=f"send just this message to {who.title()}")
                return
            if not text.startswith("/"):
                return
            if " " in text:
                cmd, _, arg = text[1:].partition(" ")
                for v in ARG_CHOICES.get(cmd, ANIM_CHOICES if cmd == "anim" else []):
                    if v.startswith(arg) and " " not in arg:
                        yield Completion(v, -len(arg))
                return
            word = text[1:].lower()
            for name, hint, desc in COMMANDS:
                if name.startswith(word):
                    yield Completion("/" + name + (" " if hint else ""), -len(text), display="/" + name,
                                     display_meta=desc)
            ranked = sorted((not n.lower().startswith(word), n) for n in skills if word in n.lower())
            for _, name in ranked:
                s = skills[name]
                tag = skill_tag(s)
                if True:
                    yield Completion("/" + name + " ", -len(text), display=f"/{name}",
                                     display_meta=f"[{tag}] {s['description']}")

    return SlashMenu()


def print_help(skills):
    print(f"\n{BOLD}Commands{RESET}  {DIM}(type / to pick from a menu){RESET}")
    for name, hint, desc in COMMANDS:
        left = f"/{name}" + (f" {hint}" if hint else "")
        print(f"  {CYAN}{left:<44}{RESET}{desc}")
    print(f"  {CYAN}{'@claude … / @codex …':<44}{RESET}send just that message to one agent")
    print(f"\n{BOLD}Skills{RESET}  {DIM}([C] Claude, [X] Codex; /name then what you want done){RESET}")
    for name, s in sorted(skills.items()):
        tag = skill_tag(s)
        print(f"  {CYAN}/{name:<30}{RESET}{DIM}[{tag:<2}]{RESET} {s['description']}")
    print()


def run_tool(*args, rules=None):
    """Run `claudex doctor|rules|log` as its own process so its output can't tangle with the chat."""
    from .paths import self_command
    argv, env = self_command(*args, rules=rules)
    subprocess.call(argv, env=env)


def show_memory(eng):
    for title, path in (("Project map", eng.map), ("Past decisions", eng.decisions)):
        print(f"\n{BOLD}{title}{RESET} {DIM}{path}{RESET}")
        print(path.read_text()[-4000:] if path.exists() else f"{DIM}(nothing yet){RESET}")


def status_line(eng):
    return (f" {ROUTES[eng.route]} · effort {eng.effort} · mode {MODES[eng.mode][0]} · "
            f"checks {CHECKS[eng.checks]}" + (" · PLAN ONLY" if eng.dry_run else "")
            + f" · {pathlib.Path(eng.cwd).name}   / for commands ")


async def handle_command(line, eng, skills):
    """Returns (handled?, quit?)."""
    cmd, _, arg = line[1:].partition(" ")
    arg = arg.strip()
    if cmd in ("quit", "exit"):
        return True, True
    if cmd in ("help", ""):
        print_help(skills)
    elif cmd in ("auto", "claude", "codex"):
        eng.route = cmd
        print(f"{DIM}Routing: {ROUTES[eng.route]}{RESET}")
    elif cmd == "anim":
        print(f"{DIM}The monkeys live in the full screen: run claudex without --plain.{RESET}")
    elif cmd in ARG_CHOICES:
        if arg in ARG_CHOICES[cmd]:
            setattr(eng, cmd, arg)
        else:
            print(f"{DIM}Choose one of: {' '.join(ARG_CHOICES[cmd])}{RESET}")
        print(f"{DIM}{cmd.title()}: {getattr(eng, cmd)}{RESET}")
    elif cmd == "dry":
        eng.dry_run = not eng.dry_run
        print(f"{DIM}Plan only: {'on' if eng.dry_run else 'off'}{RESET}")
    elif cmd == "status":
        print(f"{DIM}{status_line(eng).strip()}\nNotes: {eng.notes}{RESET}")
    elif cmd in ("rules", "doctor", "log", "models", "shared", "memory", "permissions", "init", "usage"):
        extra = {"log": ["--cwd", eng.cwd], "shared": ["--cwd", eng.cwd], "memory": ["--cwd", eng.cwd],
                 "permissions": ["--cwd", eng.cwd], "init": ["--cwd", eng.cwd],
                 "models": ["--cwd", eng.cwd] + (["--models", eng.models_file] if eng.models_file else [])
                 + (["--global"] if "--global" in arg.split() else []),
                 "doctor": ["--cwd", eng.cwd] + (["--models", eng.models_file] if eng.models_file else [])
                 + (["--live"] if "--live" in arg.split() else []),
                 "usage": ["--web"] if arg == "web" else []}.get(cmd, [])
        await asyncio.get_running_loop().run_in_executor(None, lambda: run_tool(cmd, *extra, rules=eng.rules_path))
    else:
        return False, False
    return True, False


async def chat(eng):
    skills = load_skills()
    print(f"{MAGENTA}{fun.BANNER}{RESET}{DIM}   {fun.pick(fun.HELLO)}\n   {eng.cwd}\n"
          f"   Type / for commands and skills, @claude or @codex for one agent, /quit to leave.{RESET}\n")
    session, last = None, None
    if sys.stdin.isatty():
        from prompt_toolkit import PromptSession
        from prompt_toolkit.history import FileHistory
        from prompt_toolkit.styles import Style
        HISTORY.parent.mkdir(parents=True, exist_ok=True)
        session = PromptSession(
            history=FileHistory(str(HISTORY)), completer=make_completer(skills), complete_while_typing=True,
            bottom_toolbar=lambda: status_line(eng),
            style=Style.from_dict({"bottom-toolbar": "#888888 bg:#1e1e2e", "prompt": "bold #c678dd"}))
    while True:
        try:
            if session:
                line = await session.prompt_async([("class:prompt", "› ")])
            else:
                line = await asyncio.get_running_loop().run_in_executor(None, input, "› ")
        except (EOFError, KeyboardInterrupt):
            break
        line = line.strip()
        if not line:
            continue
        if line == "?":
            line = "/help"
        if line == "/retry":
            if not last:
                print(f"{DIM}Nothing to retry yet.{RESET}")
                continue
            line = last
        if not line.startswith("/"):
            last = line
        if line.startswith("/"):
            skill = expand_skill(line, skills)
            if skill:
                message, forced = skill
                line = (f"@{forced} " if forced else "") + message
            else:
                handled, quit_ = await handle_command(line, eng, skills)
                if quit_:
                    break
                if not handled:
                    print(f"{DIM}No such command or skill. Type / to see them.{RESET}")
                continue
        # While a job runs, Ctrl-C stops the job (not the chat). The handler is ours only for this job.
        loop = asyncio.get_running_loop()
        task = asyncio.create_task(eng.turn(line))

        def stop_job():
            print(f"\n{DIM}Stopping…{RESET}", flush=True)
            eng.cancel()
        try:
            loop.add_signal_handler(signal.SIGINT, stop_job)
        except (NotImplementedError, RuntimeError):
            pass
        try:
            await task
        finally:
            try:
                loop.remove_signal_handler(signal.SIGINT)
            except (NotImplementedError, RuntimeError):
                pass
        print("\n")
    print(f"{DIM}{fun.pick(fun.BYE)}{RESET}")


async def run(args, settings):
    if args.print and not sys.stdin.isatty():
        settings = dict(settings, unattended=True)       # a script: nobody to ask
    eng = Engine(os.path.abspath(args.cwd), TerminalUI(), settings=settings)
    try:
        if args.print:
            text = args.print
            skill = expand_skill(text, load_skills()) if text.startswith("/") else None
            if skill:
                text = (f"@{skill[1]} " if skill[1] else "") + skill[0]
            stop = await eng.turn(text)
            print()
            return 0 if eng.ok and stop == "end_turn" else 1
        if sys.stdin.isatty() and sys.stdout.isatty() and not getattr(args, "plain", False):
            from .tui import run_tui
            return await run_tui(eng)
        await chat(eng)
        return 0
    finally:
        await eng.close()
