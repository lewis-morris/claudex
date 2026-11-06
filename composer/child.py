"""A worker agent (Claude or Codex) driven over ACP as a child process."""
import asyncio
import os

from .rpc import Peer, RpcError

NODE_BIN = os.path.expanduser("~/.nvm/versions/node/v24.18.0/bin")
FORWARD = {"agent_message_chunk", "agent_thought_chunk", "tool_call", "tool_call_update"}


class Child:
    def __init__(self, name, cmd, cwd, ui, mcp_servers=None):
        self.name, self.cmd, self.cwd, self.ui = name, cmd, cwd, ui
        self.mcp_servers = mcp_servers or []
        self.proc = self.peer = self.session_id = None
        self.options = {}          # config id -> list of allowed values
        self._text = []            # agent message text of the current step
        self.busy = False

    async def start(self):
        env = dict(os.environ)
        env["PATH"] = NODE_BIN + os.pathsep + env.get("PATH", "")
        self.proc = await asyncio.create_subprocess_exec(
            *self.cmd, cwd=self.cwd, env=env, limit=64 * 1024 * 1024,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL)

        def write(line):
            self.proc.stdin.write(line.encode() + b"\n")

        self.peer = Peer(self.proc.stdout, write, self._on_request, self._on_notify, name=self.name)
        asyncio.create_task(self.peer.run())
        await self.peer.request("initialize", {
            "protocolVersion": 1,
            "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False}})
        res = await self.peer.request("session/new", {"cwd": self.cwd, "mcpServers": self.mcp_servers})
        self.session_id = res["sessionId"]
        self._load_options(res.get("configOptions"))

    def _load_options(self, opts):
        for o in opts or []:
            self.options[o["id"]] = [v.get("value") for v in o.get("options", []) if isinstance(v, dict)]

    async def set(self, config_id, value):
        """Set a model/effort/mode option if this agent offers that value. Returns True if set."""
        if value is None or value not in self.options.get(config_id, []):
            return False
        try:
            res = await self.peer.request("session/set_config_option",
                                          {"sessionId": self.session_id, "configId": config_id, "value": value})
            self._load_options((res or {}).get("configOptions"))
            return True
        except RpcError:
            return False

    async def prompt(self, text):
        self._text, self.busy = [], True
        try:
            res = await self.peer.request("session/prompt", {
                "sessionId": self.session_id, "prompt": [{"type": "text", "text": text}]})
            return res.get("stopReason", "end_turn"), "".join(self._text).strip()
        finally:
            self.busy = False

    def cancel(self):
        if self.peer and self.session_id:
            self.peer.notify("session/cancel", {"sessionId": self.session_id})

    async def close(self):
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.stdin.close()
                await asyncio.wait_for(self.proc.wait(), 3)
            except Exception:
                self.proc.kill()

    # --- messages from the worker ---
    async def _on_notify(self, method, params):
        if method != "session/update":
            return
        upd = dict(params.get("update") or {})
        kind = upd.get("sessionUpdate")
        if kind == "agent_message_chunk":
            c = upd.get("content") or {}
            if c.get("type") == "text":
                self._text.append(c.get("text", ""))
        if kind in FORWARD:
            if "toolCallId" in upd:  # Claude and Codex ids could clash
                upd["toolCallId"] = f"{self.name}:{upd['toolCallId']}"
            await self.ui.update(upd)

    async def _on_request(self, method, params):
        if method == "session/request_permission":
            p = dict(params)
            tc = dict(p.get("toolCall") or {})
            if "toolCallId" in tc:
                tc["toolCallId"] = f"{self.name}:{tc['toolCallId']}"
            p["toolCall"] = tc
            return await self.ui.permission(p)
        raise RpcError(-32601, f"composer does not provide {method}")
