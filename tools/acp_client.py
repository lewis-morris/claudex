#!/usr/bin/env python3
"""A tiny stand-in for Agents of Empires: drives `claudex --acp` and prints what AoE would show.

Usage: acp_client.py PROJECT_DIR "message" [route]
Permission requests are answered with the first 'allow' option.
"""
import json
import pathlib
import queue
import subprocess
import sys
import threading
import time

COMPOSER = str(pathlib.Path(__file__).resolve().parent.parent / "bin" / "claudex")


def main():
    cwd, text = sys.argv[1], sys.argv[2]
    route = sys.argv[3] if len(sys.argv) > 3 else "auto"
    p = subprocess.Popen([COMPOSER, "--acp"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    n = [0]
    counts = {}
    lines = queue.Queue()  # read on a thread: select() can't see lines already in Python's buffer
    threading.Thread(target=lambda: [lines.put(x) for x in p.stdout] + [lines.put("")], daemon=True).start()

    def send(o):
        p.stdin.write(json.dumps(o) + "\n")
        p.stdin.flush()

    def call(method, params, timeout=1500):
        n[0] += 1
        i = n[0]
        send({"jsonrpc": "2.0", "id": i, "method": method, "params": params})
        end = time.time() + timeout
        while time.time() < end:
            try:
                line = lines.get(timeout=1)
            except queue.Empty:
                continue
            if not line:
                return {"error": "claudex exited"}
            m = json.loads(line)
            if m.get("id") == i and "method" not in m:
                return m
            if m.get("method") == "session/update":
                u = m["params"]["update"]
                k = u.get("sessionUpdate")
                counts[k] = counts.get(k, 0) + 1
                if k == "agent_message_chunk":
                    print(u["content"].get("text", ""), end="", flush=True)
                elif k == "tool_call":
                    print(f"\n  [tool] {u.get('toolCallId')}: {u.get('title')}", flush=True)
                elif k == "plan":
                    print("\n  [plan] " + " | ".join(f"{e['status']}: {e['content']}" for e in u["entries"]),
                          flush=True)
            elif m.get("method") == "session/request_permission":
                opts = m["params"]["options"]
                pick = next((o for o in opts if o.get("kind", "").startswith("allow")), opts[0])
                print(f"\n  [permission] {m['params']['toolCall'].get('title')} -> {pick['name']}", flush=True)
                send({"jsonrpc": "2.0", "id": m["id"],
                      "result": {"outcome": {"outcome": "selected", "optionId": pick["optionId"]}}})
        return {"error": "timeout"}

    init = call("initialize", {"protocolVersion": 1, "clientCapabilities": {}})
    print("agent:", init.get("result", {}).get("agentInfo"))
    new = call("session/new", {"cwd": cwd, "mcpServers": []})
    sid = new["result"]["sessionId"]
    print("options:", [(o["id"], o["currentValue"], [v["value"] for v in o["options"]])
                       for o in new["result"]["configOptions"]])
    if route != "auto":
        print("set route:", call("session/set_config_option",
                                 {"sessionId": sid, "configId": "model", "value": route}).get("error", "ok"))
    t = time.time()
    res = call("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": text}]})
    print(f"\n\nSTOP: {res.get('result') or res.get('error')} in {round(time.time() - t)}s")
    print("updates seen:", counts)
    p.stdin.close()
    p.wait(10)


if __name__ == "__main__":
    main()
