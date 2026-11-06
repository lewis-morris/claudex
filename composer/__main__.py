import argparse
import asyncio
import sys


def main():
    ap = argparse.ArgumentParser(prog="composer",
                                 description="One lead picks Claude or Codex (and the model) for each part of a job.")
    ap.add_argument("--acp", action="store_true", help="speak ACP on stdin/stdout (for Agents of Empires)")
    ap.add_argument("-p", "--print", metavar="MESSAGE", help="run one message and exit")
    ap.add_argument("--cwd", default=".", help="project folder (default: here)")
    ap.add_argument("--route", default="auto", choices=["auto", "claude", "codex"])
    ap.add_argument("--mode", default="auto", choices=["default", "acceptEdits", "auto", "bypassPermissions"])
    args, _unknown = ap.parse_known_args()  # AoE may append agent flags; ignore them
    try:
        if args.acp:
            from .server import serve
            asyncio.run(serve())
        else:
            from .cli import run
            asyncio.run(run(args))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
