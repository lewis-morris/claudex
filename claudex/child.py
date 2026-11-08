"""A worker agent (Claude or Codex) driven over ACP as a child process."""
import asyncio
import os
import signal
import time

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
        self.decide = None         # Claudex's permission decider (rules, gatekeeper, you)
        self.usage = {}            # latest usage_update: context used/size and cost so far
        self.squashed = False      # the context shrank (the agent compacted it): Claudex re-sends its memory
        self.touched = set()       # files this turn's edits named (to spot work outside the watched folder)
        self._kinds = {}           # toolCallId -> kind: later updates often leave the kind out
        self._titles = {}          # toolCallId -> what it was, so a failure can say which action failed
        self.failed = []           # titles of actions that failed (for the run trace)
        self.tool_calls = 0        # actions the worker started, and how many failed (for the run trace)
        self.tool_failures = 0
        self.limit_hit = None      # the agent said this turn was refused for a usage limit
        self.loading = False       # replaying history during session/load: nothing to show
        self.resumed = False
        self.can_steer = False
        self.last_seen = time.monotonic()   # when it last showed any sign of work (for the idle limit)
        self.open_cards = {}       # action cards it opened and hasn't closed: closed for it if it's stopped

    async def start(self, resume_id=None):
        """Start the worker. With resume_id, reload that earlier Claude/Codex conversation (if the agent can),
        so it remembers everything it did; otherwise (or if that fails) start a fresh one."""
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
        init = await self.peer.request("initialize", {
            "protocolVersion": 1,
            "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False}})
        can_load = bool(((init or {}).get("agentCapabilities") or {}).get("loadSession"))
        # It takes messages mid-turn (the ACP steering extension), so something you add reaches it at once.
        self.can_steer = bool((((init or {}).get("_meta") or {}).get("steering") or {}).get("supported"))
        self.resumed = False
        if resume_id and can_load:
            self.loading = True             # the agent replays its history while loading: don't show it again
            try:
                res = await self.peer.request("session/load", {"sessionId": resume_id, "cwd": self.cwd,
                                                               "mcpServers": self.mcp_servers})
                self.session_id, self.resumed = resume_id, True
                self._load_options((res or {}).get("configOptions"))
                return
            except Exception:
                pass                        # couldn't reload it: start fresh (Claudex's notes still carry context)
            finally:
                self.loading = False
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
        self._text, self.busy, self.limit_hit = [], True, None
        self.touched = set()
        self.last_seen = time.monotonic()
        try:
            res = await self.peer.request("session/prompt", {
                "sessionId": self.session_id, "prompt": [{"type": "text", "text": text}]})
            return res.get("stopReason", "end_turn"), "".join(self._text).strip()
        finally:
            self.busy = False

    async def steer(self, text):
        """Put a message into the turn it's working on now. Returns the outcome: "injected" when it has it;
        anything else (it had just finished) means it wasn't delivered."""
        res = await self.peer.request("_session/steering", {
            "sessionId": self.session_id, "prompt": [{"type": "text", "text": text}],
            "_meta": {"steering": {"idleBehavior": "promptRequired"}}})
        return (res or {}).get("outcome")

    def quiet_for(self):
        """Seconds since the worker last showed any sign of work."""
        return time.monotonic() - self.last_seen

    async def close_cards(self):
        """Mark any action it left running as stopped, so the screen doesn't keep counting on them."""
        cards, self.open_cards = list(self.open_cards), {}
        for tid in cards:
            try:
                await self.ui.update({"sessionUpdate": "tool_call_update", "toolCallId": tid, "status": "failed"})
            except Exception:
                pass

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
        if method != "session/update" or self.loading:
            return
        upd = dict(params.get("update") or {})
        kind = upd.get("sessionUpdate")
        self.last_seen = time.monotonic()
        if kind == "usage_update":
            cost = upd.get("cost") or {}
            before = self.usage.get("used")
            if isinstance(before, (int, float)) and isinstance(upd.get("used"), (int, float)) and upd["used"] < before:
                self.squashed = True
            self.usage = {"used": upd.get("used"), "size": upd.get("size"), "cost": cost.get("amount")}
            rl = (upd.get("_meta") or {}).get("_claude/rateLimit") or {}
            if rl.get("status") == "rejected" and rl.get("overageStatus") not in ("allowed", "allowed_warning"):
                self.limit_hit = rl          # e.g. {"rateLimitType": "five_hour", "resetsAt": 1762300000}
            return
        if kind in ("tool_call", "tool_call_update"):
            tid = upd.get("toolCallId")
            if upd.get("kind"):
                self._kinds[tid] = str(upd["kind"]).lower()
            if upd.get("title"):
                self._titles[tid] = " ".join(str(upd["title"]).split())[:100]
            changes = self._kinds.get(tid) in ("edit", "delete", "move")
            for loc in (upd.get("locations") or []) if changes else []:
                if isinstance(loc, dict) and loc.get("path"):
                    self.touched.add(str(loc["path"]))
            for item in upd.get("content") or []:      # a diff always means a file was changed
                if isinstance(item, dict) and item.get("type") == "diff" and item.get("path"):
                    self.touched.add(str(item["path"]))
        if kind == "tool_call":
            self.tool_calls += 1
        elif kind == "tool_call_update" and upd.get("status") == "failed":
            self.tool_failures += 1
            self.failed.append(self._titles.get(upd.get("toolCallId"), "an action"))
            del self.failed[:-50]
        if kind == "agent_message_chunk":
            c = upd.get("content") or {}
            if c.get("type") == "text":
                self._text.append(c.get("text", ""))
        if kind in FORWARD:
            if "toolCallId" in upd:  # Claude and Codex ids could clash
                upd["toolCallId"] = f"{self.name}:{upd['toolCallId']}"
                if upd.get("status") in ("completed", "failed"):
                    self.open_cards.pop(upd["toolCallId"], None)
                elif kind == "tool_call":
                    self.open_cards[upd["toolCallId"]] = True
            await self.ui.update(upd)

    async def _on_request(self, method, params):
        if method == "session/request_permission":
            p = dict(params)
            tc = dict(p.get("toolCall") or {})
            if "toolCallId" in tc:
                tc["toolCallId"] = f"{self.name}:{tc['toolCallId']}"
            p["toolCall"] = tc
            try:
                if self.decide:
                    return await self.decide(p)
                return await self.ui.permission(p)
            finally:
                self.last_seen = time.monotonic()    # time spent waiting on an answer isn't the worker idling
        raise RpcError(-32601, f"claudex does not provide {method}")


def _group_alive(pgid):
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
