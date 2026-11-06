"""Composer as an ACP agent (for Agents of Empires' structured view, Zed, etc.)."""
import asyncio
import sys
import uuid

from . import __version__
from .engine import EFFORTS, MODES, ROUTES, Engine
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


class Server:
    def __init__(self):
        self.sessions = {}
        self.peer = None

    async def on_request(self, method, p):
        if method == "initialize":
            return {"protocolVersion": 1,
                    "agentCapabilities": {"loadSession": False,
                                          "promptCapabilities": {"image": False, "audio": False,
                                                                 "embeddedContext": True},
                                          "mcpCapabilities": {"http": True, "sse": False}},
                    "authMethods": [],
                    "agentInfo": {"name": "composer", "title": "Composer", "version": __version__}}
        if method == "session/new":
            sid = "composer-" + uuid.uuid4().hex[:16]
            e = Engine(p.get("cwd") or ".", SessionUI(self.peer, sid), p.get("mcpServers") or [])
            self.sessions[sid] = e
            return {"sessionId": sid, "configOptions": config_options(e)}
        e = self.sessions.get(p.get("sessionId"))
        if e is None:
            raise RpcError(-32602, "unknown session")
        if method == "session/set_config_option":
            cid, val = p.get("configId"), p.get("value")
            valid = {"mode": MODES, "model": ROUTES, "effort": EFFORTS}.get(cid)
            if valid is None or val not in valid:
                raise RpcError(-32602, f"unknown option {cid}={val}")
            setattr(e, {"mode": "mode", "model": "route", "effort": "effort"}[cid], val)
            return {"configOptions": config_options(e)}
        if method == "session/set_mode":
            if p.get("modeId") in MODES:
                e.mode = p["modeId"]
            return {}
        if method == "session/prompt":
            parts = []
            for b in p.get("prompt") or []:
                if b.get("type") == "text":
                    parts.append(b.get("text", ""))
                elif b.get("type") == "resource_link":
                    parts.append(f"[{b.get('name') or b.get('uri')}]({b.get('uri')})")
                elif b.get("type") == "resource":
                    r = b.get("resource") or {}
                    parts.append(f"\n<file {r.get('uri')}>\n{r.get('text', '')}\n</file>\n")
            return {"stopReason": await e.turn("\n".join(parts).strip())}
        raise RpcError(-32601, f"method not found: {method}")

    async def on_notify(self, method, p):
        if method == "session/cancel":
            e = self.sessions.get(p.get("sessionId"))
            if e:
                e.cancel()


async def serve():
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=64 * 1024 * 1024)
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    out = sys.stdout

    def write(line):
        out.write(line + "\n")
        out.flush()

    srv = Server()
    srv.peer = Peer(reader, write, srv.on_request, srv.on_notify, name="client")
    try:
        await srv.peer.run()
    finally:
        for e in srv.sessions.values():
            await e.close()
