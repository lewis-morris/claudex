"""Claudex as an ACP agent (for Agents of Empires' structured view, Zed, etc.)."""
import asyncio
import json
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
from .keepalive import PromptActivity


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


SESSIONS_DIR = pathlib.Path(os.environ.get("XDG_DATA_HOME", pathlib.Path.home() / ".local/share")) / "claudex/acp-sessions"


def save_session_meta(sid, e):
    """What a restarted session needs to carry on: its folder, its memory key and its pickers."""
    if not sid or not SAFE_ID.match(str(sid)):
        return
    try:
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        (SESSIONS_DIR / f"{sid}.json").write_text(json.dumps({
            "cwd": e.cwd, "key": e.session_key,
            "settings": {"route": e.route, "effort": e.effort, "mode": e.mode, "checks": e.checks}}))
    except Exception:
        pass


SAFE_ID = __import__("re").compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
PICKERS = {"route": ROUTES, "effort": EFFORTS, "mode": MODES, "checks": CHECKS}


def load_session_meta(sid):
    """Saved metadata for a session, checked: only known pickers with valid values come back."""
    if not sid or not SAFE_ID.match(sid):
        return {}
    try:
        data = json.loads((SESSIONS_DIR / f"{sid}.json").read_text())
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    out = {}
    if isinstance(data.get("cwd"), str):
        out["cwd"] = data["cwd"]
    if isinstance(data.get("key"), str) and SAFE_ID.match(data["key"]):
        out["key"] = data["key"]
    settings = data.get("settings") if isinstance(data.get("settings"), dict) else {}
    out["settings"] = {k: v for k, v in settings.items() if k in PICKERS and isinstance(v, str) and v in PICKERS[k]}
    return out


class SessionUI:
    def __init__(self, peer, session_id):
        self.peer, self.session_id = peer, session_id
        self.keepalive = PromptActivity(self.update)

    async def update(self, upd):
        self.peer.notify("session/update", {"sessionId": self.session_id, "update": upd})
        self.keepalive.touch()

    async def say(self, text):
        await self.update({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}})

    async def plan(self, entries):
        await self.update({"sessionUpdate": "plan", "entries": entries})

    async def working(self, title):
        """A running-work card, so Agents of Empires sees progress while a plan or check is thinking."""
        tid = "claudex-" + uuid.uuid4().hex[:12]
        await self.update({"sessionUpdate": "tool_call", "toolCallId": tid, "title": title, "kind": "think",
                           "status": "in_progress"})
        return tid

    async def done_working(self, tid, ok=True):
        await self.update({"sessionUpdate": "tool_call_update", "toolCallId": tid,
                           "status": "completed" if ok else "failed"})

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
                    "agentCapabilities": {"loadSession": True,
                                          "promptCapabilities": {"image": False, "audio": False,
                                                                 "embeddedContext": True},
                                          "mcpCapabilities": {"http": True, "sse": False}},
                    "authMethods": [],
                    "agentInfo": {"name": "claudex", "title": "Claudex", "version": __version__},
                    # Messages sent mid-job come in as `_session/steering` instead of being turned away.
                    "_meta": {"steering": {"supported": True}}}
        if method in ("session/new", "session/load"):
            # session/load: Agents of Empires restarting a session it already knows (it keeps the conversation
            # itself). Claudex picks the same session back up: same memory and notes, same pickers.
            loading = method == "session/load"
            sid = p.get("sessionId") if loading else "claudex-" + uuid.uuid4().hex[:16]
            if not isinstance(sid, str) or not SAFE_ID.match(sid):
                raise RpcError(-32602, "session/load needs a plain session id")
            if loading and sid in self.sessions:
                e = self.sessions[sid]
                if p.get("cwd") and os.path.realpath(p["cwd"]) != os.path.realpath(e.cwd):
                    raise RpcError(-32602, f"this session belongs to {e.cwd}, not {p['cwd']}")
                return {"configOptions": config_options(e)}
            saved = load_session_meta(sid) if loading else {}
            cwd = p.get("cwd") or saved.get("cwd") or "."
            if saved.get("cwd") and os.path.realpath(cwd) != os.path.realpath(saved["cwd"]):
                # same session, different folder: never resume one project's memory against another's files
                raise RpcError(-32602, f"this session belongs to {saved['cwd']}, not {cwd}")
            key = saved.get("key") or (None if os.environ.get("AOE_ARTIFACT_DIR") else sid)
            e = Engine(cwd, SessionUI(self.peer, sid), p.get("mcpServers") or [],
                       settings=dict(self.settings, **saved.get("settings", {})),   # pickers only; launch options stay
                       session_key=key)
            self.sessions[sid] = e
            save_session_meta(sid, e)
            e.skills = load_skills()
            # Fill Agents of Empires' `/` menu once the session exists (just after this reply).
            # call_soon runs after this handler returns, and the reply is written as it returns, so the
            # client always knows the session before the menu arrives.
            asyncio.get_running_loop().call_soon(lambda: self.peer.notify("session/update", {
                "sessionId": sid, "update": {"sessionUpdate": "available_commands_update",
                                             "availableCommands": acp_commands(e.skills)}}))
            if loading:
                return {"configOptions": config_options(e)}
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
            save_session_meta(p.get("sessionId"), e)
            return {"configOptions": config_options(e)}
        if method == "session/set_mode":
            if p.get("modeId") in MODES or p.get("modeId") in LEGACY_MODES:
                e.mode = norm_mode(p["modeId"])
                save_session_meta(p.get("sessionId"), e)
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
            from .paths import STARTED_STAMP, code_stamp
            if code_stamp() > STARTED_STAMP and not getattr(e, "told_stale", False):
                e.told_stale = True
                await e.ui.say("_🐒 Claudex has been updated since this session started. Restart the session to use "
                               "the new version (in Agents of Empires: stop and resume it, or `aoe acp stop <session>`; "
                               "it comes back on your next message)._\n\n")
            extra = "\n".join(attached)
            if e.busy:
                raise RpcError(-32000, "Claudex is still working on the last message")
            e.busy = True
            try:
                await e.ui.keepalive.start()
                if typed.startswith("/") and self.local_command(typed):
                    e.local = asyncio.ensure_future(self.slash(e, typed))
                    try:
                        await e.ui.say(await e.local)
                        return {"stopReason": "end_turn"}
                    except asyncio.CancelledError:
                        return {"stopReason": "cancelled"}
                    finally:
                        e.local = None
                skill = expand_skill(typed, getattr(e, "skills", {})) if typed.startswith(("/", "$")) else None
                if skill:
                    typed = (f"@{skill[1]} " if skill[1] else "") + skill[0]
                text = (typed + ("\n" + extra if extra else "")).strip()
                stop = await e.turn(text)
                while stop == "end_turn" and getattr(e, "steers", None):     # added mid-job, but the job ended before any worker took it
                    more, e.steers = "\n\n".join(e.steers), []
                    await e.ui.say("\n\n---\n_Now for what you added while that was running._\n\n")
                    stop = await e.turn(more, already_said=True)
                e.steers = []
                return {"stopReason": stop}
            finally:
                await e.ui.keepalive.stop()
                e.busy = False
        if method == "_session/steering":
            e = self.sessions.get(p.get("sessionId"))
            if not e:
                raise RpcError(-32602, "unknown session")
            text = "\n".join(b.get("text", "") for b in p.get("prompt") or [] if b.get("type") == "text").strip()
            outcome = await e.steer(text) if e.busy and not getattr(e, "local", None) else "idle"
            if outcome != "idle":
                return {"outcome": "injected"}       # a worker has it, or the next one will
            if ((p.get("_meta") or {}).get("steering") or {}).get("idleBehavior") == "promptRequired":
                return {"outcome": "promptRequired", "reason": "noRunningTurn"}
            asyncio.ensure_future(self.on_request("session/prompt", {k: v for k, v in p.items() if k != "_meta"}))
            return {"outcome": "startedNewTurn"}
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
                     "permissions": ["--cwd", e.cwd], "init": ["--cwd", e.cwd, "--yes"],
                     "models": ["--cwd", e.cwd] + (["--models", e.models_file] if e.models_file else [])
                     + (["--global"] if "--global" in rest.split() else []),
                     "doctor": ["--cwd", e.cwd] + (["--models", e.models_file] if e.models_file else [])
                     + (["--live"] if "--live" in rest.split() else [])}.get(cmd, [])
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
                     "**Skills** (type $name, then what you want done):"]
            for name, s in sorted(getattr(e, "skills", {}).items()):
                lines.append(f"- `${name}` ({' / '.join(a.title() for a in s['agents'])}): {s['description']}")
            return "\n".join(lines) + "\n"
        return ""

    async def on_notify(self, method, p):
        if method == "session/cancel":
            e = self.sessions.get(p.get("sessionId"))
            if e:
                await e.ui.keepalive.stop()
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
            await e.ui.keepalive.stop()
            await e.close()
