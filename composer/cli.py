"""Composer in the terminal: a simple chat loop, like using claude or codex directly."""
import asyncio
import os
import shutil
import sys

from .engine import EFFORTS, MODES, ROUTES, Engine

DIM, BOLD, GREEN, RED, CYAN, RESET = "\033[2m", "\033[1m", "\033[32m", "\033[31m", "\033[36m", "\033[0m"
if not sys.stdout.isatty():
    DIM = BOLD = GREEN = RED = CYAN = RESET = ""


class TerminalUI:
    def __init__(self):
        self.tools = {}
        self.thinking = False

    async def update(self, upd):
        kind = upd.get("sessionUpdate")
        if kind == "agent_message_chunk":
            self._end_thinking()
            print((upd.get("content") or {}).get("text", ""), end="", flush=True)
        elif kind == "agent_thought_chunk":
            if not self.thinking:
                print(f"{DIM}… thinking{RESET}", flush=True)
                self.thinking = True
        elif kind == "tool_call":
            self._end_thinking()
            title = upd.get("title") or upd.get("kind") or "tool"
            self.tools[upd.get("toolCallId")] = title
            print(f"\n{CYAN}⏺ {title}{RESET}", flush=True)
        elif kind == "tool_call_update":
            st = upd.get("status")
            if st in ("completed", "failed"):
                mark = f"{GREEN}✓{RESET}" if st == "completed" else f"{RED}✗{RESET}"
                title = upd.get("title") or self.tools.get(upd.get("toolCallId"), "tool")
                print(f"  {mark} {DIM}{title}{RESET}", flush=True)

    def _end_thinking(self):
        self.thinking = False

    async def say(self, text):
        print(text, end="", flush=True)

    async def plan(self, entries):
        if all(e["status"] == "pending" for e in entries):
            print(f"{BOLD}Plan{RESET}")
            for n, e in enumerate(entries, 1):
                print(f"  {n}. {e['content']}")
            print()

    async def permission(self, params):
        tc = params.get("toolCall") or {}
        opts = params.get("options") or []
        print(f"\n{BOLD}Permission needed:{RESET} {tc.get('title') or tc.get('kind') or 'an action'}")
        for n, o in enumerate(opts, 1):
            print(f"  {n}. {o.get('name')}")
        while True:
            ans = await asyncio.get_running_loop().run_in_executor(None, input, "Choose a number: ")
            if ans.strip().isdigit() and 1 <= int(ans) <= len(opts):
                return {"outcome": {"outcome": "selected", "optionId": opts[int(ans) - 1]["optionId"]}}


HELP = """Commands: /auto /claude /codex (who does the work) · /effort auto|low|medium|high|xhigh
          /mode default|acceptEdits|auto|bypassPermissions · /status · /quit
Start a message with @claude or @codex to send just that message to one agent."""


async def run(args):
    eng = Engine(os.path.abspath(args.cwd), TerminalUI())
    eng.route, eng.mode = args.route, args.mode
    try:
        if args.print:
            await eng.turn(args.print)
            print()
            return
        print(f"{BOLD}Composer{RESET} · {eng.cwd}\n{DIM}{HELP}{RESET}\n")
        while True:
            try:
                line = await asyncio.get_running_loop().run_in_executor(None, input, f"{BOLD}› {RESET}")
            except (EOFError, KeyboardInterrupt):
                break
            line = line.strip()
            if not line:
                continue
            if line in ("/quit", "/exit"):
                break
            if line in ("/auto", "/claude", "/codex"):
                eng.route = line[1:]
                print(f"{DIM}Routing: {ROUTES[eng.route]}{RESET}")
                continue
            if line.startswith("/effort "):
                v = line.split(None, 1)[1]
                if v in EFFORTS:
                    eng.effort = v
                print(f"{DIM}Effort: {eng.effort}{RESET}")
                continue
            if line.startswith("/mode "):
                v = line.split(None, 1)[1]
                if v in MODES:
                    eng.mode = v
                print(f"{DIM}Mode: {eng.mode}{RESET}")
                continue
            if line == "/status":
                print(f"{DIM}Routing {eng.route} · effort {eng.effort} · mode {eng.mode} · "
                      f"notes {eng.notes}{RESET}")
                continue
            if line in ("/help", "?"):
                print(HELP)
                continue
            task = asyncio.create_task(eng.turn(line))
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                eng.cancel()
                await task
            print("\n")
    finally:
        await eng.close()


def width():
    return shutil.get_terminal_size((100, 20)).columns
