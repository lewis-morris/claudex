"""A worker agent (Claude or Codex) driven over ACP as a child process."""
import asyncio
import os
import signal

from .rpc import Peer, RpcError

from .paths import chrome_launcher, tool_env, which
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
        env = tool_env()
        if self.name == "claude_browser" and "CLAUDE_CODE_EXECUTABLE" not in env:
            launcher = chrome_launcher()   # Claude with Claude in Chrome switched on
            if not launcher:
                raise RuntimeError("couldn't find Claude Code to start with Chrome")
            env["CLAUDE_CODE_EXECUTABLE"] = launcher
        exe = which(self.cmd[0])
        if not exe:
            raise RuntimeError(f"{self.cmd[0]} isn't installed (see `claudex doctor`)")
        self.proc = await asyncio.create_subprocess_exec(
            exe, *self.cmd[1:], cwd=self.cwd, env=env, limit=64 * 1024 * 1024,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, start_new_session=True)

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

    async def close(self, grace=3):
        """Stop the worker and everything it started (its whole process group), and wait until all are gone.

        The connector exiting isn't enough: anything it launched (a shell, a test run) can outlive it, so the
        group is always signalled and checked, whatever the connector did.
        """
        if not self.proc:
            return
        if self.proc.returncode is None:
            try:
                self.proc.stdin.close()
                await asyncio.wait_for(self.proc.wait(), grace)
            except Exception:
                pass
        pgid = self.proc.pid
        for sig in (signal.SIGTERM, signal.SIGKILL):
            if not _group_alive(pgid):
                break
            try:
                os.killpg(pgid, sig)
            except ProcessLookupError:
                break
            for _ in range(50):
                if not _group_alive(pgid):
                    break
                await asyncio.sleep(0.1)
        if self.proc.returncode is None:
            await self.proc.wait()


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
        raise RpcError(-32601, f"claudex does not provide {method}")


def _group_alive(pgid):
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
