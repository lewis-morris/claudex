"""Claudex as an ACP agent (for Agents of Empires' structured view, Zed, etc.)."""
import asyncio
import os
import sys
import pathlib
import signal
import subprocess
import uuid

from . import __version__
from .paths import self_command
from .commands import acp_commands, expand_skill, load_skills
from .engine import CHECKS, EFFORTS, LEGACY_MODES, MODES, ROUTES, Engine, norm_mode
from .rpc import Peer, RpcError


def config_options(e):
    def select(id_, name, category, current, values):
        return {"id": id_, "name": name, "category": category, "type": "select", "currentValue": current,
                "options": [{"value": v, "name": n} for v, n in values]}
    return [
        select("mode", "Mode", "mode", e.mode, [(k, v[0]) for k, v in MODES.items()]),
        select("model", "Routing", "model", e.route, list(ROUTES.items())),
        select("effort", "Effort", "thought_level", e.effort,
               [(v, "Auto (per step)" if v == "auto" else v.title()) for v in EFFORTS]),
        select("checks", "Checks", "other", e.checks, list(CHECKS.items())),
    ]


class SessionUI:
    def __init__(self, peer, session_id):
        self.peer, self.session_id = peer, session_id

    async def update(self, upd):
        self.peer.notify("session/update", {"sessionId": self.session_id, "update": upd})

    async def say(self, text):
        await self.update({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}})

    async def plan(self, entries):
        await self.update({"sessionUpdate": "plan", "entries": entries})

    async def permission(self, params):
        return await self.peer.request("session/request_permission", {**params, "sessionId": self.session_id})


OPTION_ATTR = {"mode": "mode", "model": "route", "effort": "effort", "checks": "checks"}


class Server:
    def __init__(self, settings=None):
        self.sessions = {}
        self.peer = None
        self.settings = settings or {}

    async def on_request(self, method, p):
        if method == "initialize":
            return {"protocolVersion": 1,
                    "agentCapabilities": {"loadSession": False,
                                          "promptCapabilities": {"image": False, "audio": False,
                                                                 "embeddedContext": True},
                                          "mcpCapabilities": {"http": True, "sse": False}},
                    "authMethods": [],
                    "agentInfo": {"name": "claudex", "title": "Claudex", "version": __version__}}
        if method == "session/new":
            sid = "claudex-" + uuid.uuid4().hex[:16]
            key = None if os.environ.get("AOE_ARTIFACT_DIR") else sid
            e = Engine(p.get("cwd") or ".", SessionUI(self.peer, sid), p.get("mcpServers") or [],
                       settings=self.settings, session_key=key)
            self.sessions[sid] = e
            e.skills = load_skills()
            # Fill Agents of Empires' `/` menu once the session exists (just after this reply).
            # call_soon runs after this handler returns, and the reply is written as it returns, so the
            # client always knows the session before the menu arrives.
            asyncio.get_running_loop().call_soon(lambda: self.peer.notify("session/update", {
                "sessionId": sid, "update": {"sessionUpdate": "available_commands_update",
                                             "availableCommands": acp_commands(e.skills)}}))
            return {"sessionId": sid, "configOptions": config_options(e)}
        e = self.sessions.get(p.get("sessionId"))
        if e is None:
            raise RpcError(-32602, "unknown session")
        if method == "session/set_config_option":
            cid, val = p.get("configId"), p.get("value")
            if cid == "mode" and val in LEGACY_MODES:
                val = norm_mode(val)
            valid = {"mode": MODES, "model": ROUTES, "effort": EFFORTS, "checks": CHECKS}.get(cid)
            if valid is None or val not in valid:
                raise RpcError(-32602, f"unknown option {cid}={val}")
            setattr(e, OPTION_ATTR[cid], val)
            return {"configOptions": config_options(e)}
        if method == "session/set_mode":
            if p.get("modeId") in MODES or p.get("modeId") in LEGACY_MODES:
                e.mode = norm_mode(p["modeId"])
            return {}
        if method == "session/prompt":
            said, attached = [], []
            for b in p.get("prompt") or []:
                if b.get("type") == "text":
                    said.append(b.get("text", ""))
                elif b.get("type") == "resource_link":
                    attached.append(f"[{b.get('name') or b.get('uri')}]({b.get('uri')})")
                elif b.get("type") == "resource":
                    r = b.get("resource") or {}
                    attached.append(f"\n<file {r.get('uri')}>\n{r.get('text', '')}\n</file>\n")
            # Commands and skills are read from what the user typed, wherever attachments sit.
            typed = "\n".join(said).strip()
            extra = "\n".join(attached)
            if e.busy:
                raise RpcError(-32000, "Claudex is still working on the last message")
            e.busy = True
            try:
                if typed.startswith("/") and self.local_command(typed):
                    e.local = asyncio.ensure_future(self.slash(e, typed))
                    try:
                        await e.ui.say(await e.local)
                        return {"stopReason": "end_turn"}
                    except asyncio.CancelledError:
                        return {"stopReason": "cancelled"}
                    finally:
                        e.local = None
                skill = expand_skill(typed, getattr(e, "skills", {})) if typed.startswith("/") else None
                if skill:
                    typed = (f"@{skill[1]} " if skill[1] else "") + skill[0]
                text = (typed + ("\n" + extra if extra else "")).strip()
                return {"stopReason": await e.turn(text)}
            finally:
                e.busy = False
        raise RpcError(-32601, f"method not found: {method}")

    LOCAL = ("rules", "doctor", "log", "memory", "status", "help", "shared", "permissions", "init", "usage", "models")

    def local_command(self, text):
        parts = text[1:].split(None, 1)
        return bool(parts) and parts[0] in self.LOCAL

    async def slash(self, e, text):
        """Answer /help, /rules, /doctor, /log, /memory, /status directly."""
        cmd = text[1:].split(None, 1)[0]
        rest = text[1:].split(None, 1)[1].strip() if len(text[1:].split(None, 1)) > 1 else ""
        if cmd == "usage" and rest == "web":
            import threading
            from . import usage
            if not getattr(self, "_web", None):
                self._web = threading.Thread(target=usage.serve, kwargs={"open_browser": False}, daemon=True)
                self._web.start()
            return "Usage page: http://localhost:8765/ (live; `claudex usage --web --lan` to see it from your phone)\n"
        if cmd in ("rules", "doctor", "log", "shared", "memory", "permissions", "init", "usage", "models"):
            extra = {"log": ["--cwd", e.cwd], "shared": ["--cwd", e.cwd], "memory": ["--cwd", e.cwd],
                     "permissions": ["--cwd", e.cwd], "init": ["--cwd", e.cwd, "--yes"], "models": ["--cwd", e.cwd]}.get(cmd, [])
            args, env = self_command(cmd, *extra, rules=e.rules_path)
            p = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                     stderr=asyncio.subprocess.STDOUT, env=env,
                                                     start_new_session=True)
            try:
                out, _ = await p.communicate()
            except asyncio.CancelledError:
                try:
                    os.killpg(p.pid, signal.SIGKILL)  # the command and anything it started
                except ProcessLookupError:
                    pass
                await p.wait()
                raise
            return "```\n" + out.decode(errors="replace").strip() + "\n```\n"
        if cmd == "status":
            return (f"Routing {e.route} · effort {e.effort} · mode {e.mode} · checks {e.checks}  \n"
                    f"Notes: `{e.notes}`\n")
        if cmd == "help":
            lines = ["**Commands:** /init, /shared, /memory, /permissions, /usage (/usage web), /rules, /doctor, /log, "
                     "/status. Routing, Effort, Mode and Checks are "
                     "the pickers. Start a message with @claude or @codex to send it to one agent.", "",
                     "**Skills** (/name, then what you want done):"]
            for name, s in sorted(getattr(e, "skills", {}).items()):
                lines.append(f"- `/{name}` ({' / '.join(a.title() for a in s['agents'])}): {s['description']}")
            return "\n".join(lines) + "\n"
        return ""

    async def on_notify(self, method, p):
        if method == "session/cancel":
            e = self.sessions.get(p.get("sessionId"))
            if e and getattr(e, "local", None):
                e.local.cancel()
            elif e:
                e.cancel()


async def serve(settings=None):
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=64 * 1024 * 1024)
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    out = sys.stdout

    def write(line):
        out.write(line + "\n")
        out.flush()

    srv = Server(settings)
    srv.peer = Peer(reader, write, srv.on_request, srv.on_notify, name="client")
    try:
        await srv.peer.run()
    finally:
        for e in srv.sessions.values():
            await e.close()
