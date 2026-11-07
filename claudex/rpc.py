"""Minimal JSON-RPC 2.0 peer over newline-delimited JSON (the ACP transport)."""
import asyncio
import itertools
import json
import sys


class RpcError(Exception):
    def __init__(self, code, message, data=None):
        super().__init__(message)
        self.code, self.message, self.data = code, message, data


class Peer:
    """One end of an ACP connection. Handlers are async callables (method, params)."""

    def __init__(self, reader, write_line, on_request=None, on_notify=None, name="peer"):
        self.reader = reader
        self.write_line = write_line
        self.on_request = on_request
        self.on_notify = on_notify
        self.name = name
        self._ids = itertools.count(1)
        self._pending = {}
        self.closed = asyncio.Event()

    def _send(self, obj):
        self.write_line(json.dumps(obj, separators=(",", ":")))

    async def request(self, method, params):
        i = next(self._ids)
        fut = asyncio.get_running_loop().create_future()
        self._pending[i] = fut
        self._send({"jsonrpc": "2.0", "id": i, "method": method, "params": params})
        return await fut

    def notify(self, method, params):
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def _handle_request(self, msg):
        try:
            if not self.on_request:
                raise RpcError(-32601, f"method not found: {msg['method']}")
            result = await self.on_request(msg["method"], msg.get("params") or {})
            self._send({"jsonrpc": "2.0", "id": msg["id"], "result": result if result is not None else {}})
        except RpcError as e:
            err = {"code": e.code, "message": e.message}
            if e.data is not None:
                err["data"] = e.data
            self._send({"jsonrpc": "2.0", "id": msg["id"], "error": err})
        except Exception as e:  # never let one bad request kill the connection
            print(f"[{self.name}] request {msg.get('method')} failed: {e!r}", file=sys.stderr)
            self._send({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32603, "message": str(e)}})

    async def run(self):
        try:
            while True:
                line = await self.reader.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue  # stray log output on stdout
                if "method" in msg and "id" in msg:
                    asyncio.create_task(self._handle_request(msg))
                elif "method" in msg:
                    if self.on_notify:
                        try:
                            await self.on_notify(msg["method"], msg.get("params") or {})
                        except Exception as e:
                            print(f"[{self.name}] notify failed: {e!r}", file=sys.stderr)
                elif msg.get("id") in self._pending:
                    fut = self._pending.pop(msg["id"])
                    if fut.done():
                        continue
                    if "error" in msg:
                        e = msg["error"]
                        fut.set_exception(RpcError(e.get("code", -1), e.get("message", "error"), e.get("data")))
                    else:
                        fut.set_result(msg.get("result"))
        finally:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(RpcError(-32000, f"{self.name} connection closed"))
            self._pending.clear()
            self.closed.set()
