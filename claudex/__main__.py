import argparse
import asyncio
import pathlib
import sys

USAGE = """claudex [options]            chat in this folder
claudex -p "message"         run one message, then exit (exit code 1 if it failed)
claudex --acp                run as an agent for Agents of Empires (or any ACP client)
claudex login [claude|codex] sign in to whichever isn't signed in (--device for a code, --again to redo)
claudex doctor [--live]      check logins, versions, connectors and every model in the rules
claudex models [--init|--edit] [--global]   who does what, and where that comes from (models.md)
claudex rules [--edit]       show (or edit) the full rules file
claudex log [-n N]           recent steps in this project
claudex update               update Claude, Codex and both connectors, then run doctor
claudex init                 set a project up: one house-rules file for both agents, memory, first map
claudex shared               what Claude and Codex share (house rules, skills, connectors) and what they don't
claudex memory [--files]     this project's Claudex memory
claudex permissions [--edit] the permission rules in force and every recent decision
claudex usage [--web]        tokens across both agents as a coloured chart (--web: a live page, --lan for your network)
claudex bench                race Claude, Codex and Claudex on small projects (tests, review score, time, tokens)
claudex demo                 preview the full-screen look (1 2 3 colours, q quits)"""


def chat_parser():
    ap = argparse.ArgumentParser(prog="claudex", usage=USAGE,
                                 description="One lead plans the job; Claude and Codex each do the parts they're "
                                             "best at, check each other's work, and share one project memory.")
    ap.add_argument("--acp", action="store_true", help="speak ACP on stdin/stdout (for Agents of Empires)")
    ap.add_argument("-p", "--print", metavar="MESSAGE", help="run one message and exit")
    ap.add_argument("--cwd", default=".", help="project folder (default: here)")
    ap.add_argument("--route", default=None, choices=["auto", "claude", "codex"],
                    help="who does the work: auto (Claudex decides), or one agent for everything")
    ap.add_argument("--mode", default=None,
                    choices=["ask", "auto", "unrestricted", "default", "acceptEdits", "bypassPermissions"],
                    help="ask: you approve everything · auto: Claudex's rules + a gatekeeper decide, you get "
                         "the hard calls · unrestricted: nothing asks (default; old names still work)")
    ap.add_argument("--unattended", action="store_true",
                    help="nobody's watching: anything that would ask you is refused instead")
    ap.add_argument("--effort", default="auto", choices=["auto", "low", "medium", "high", "xhigh"],
                    help="force one reasoning level for every step (default: per step)")
    ap.add_argument("--checks", default=None, choices=["full", "reviews", "off"],
                    help="full: second opinions + reviews; reviews: reviews only; off: no checks")
    ap.add_argument("--no-review", action="store_true", help="same as --checks off")
    ap.add_argument("--no-second-opinion", action="store_true", help="same as --checks reviews")
    ap.add_argument("--lead-model", help="model for the lead (default from rules.toml)")
    ap.add_argument("--lead-effort", choices=["low", "medium", "high", "xhigh"], help="reasoning for the lead")
    ap.add_argument("--rules", help="use a different rules file")
    ap.add_argument("--models", metavar="FILE", help="use this models.md (who does what) for this session")
    ap.add_argument("--dry-run", action="store_true", help="show the plan and who'd do each part; change nothing")
    ap.add_argument("--plain", action="store_true", help="simple line-by-line chat instead of the full screen")
    ap.add_argument("--resume", nargs="?", const="pick", metavar="N",
                    help="carry on an earlier conversation in this project (no number: show the list)")
    ap.add_argument("--version", action="version",
                    version=f"Claudex, {__import__('claudex.paths', fromlist=['x']).code_label()}")
    ap.add_argument("-c", "--continue", dest="cont", action="store_true", help="carry on the most recent conversation")
    return ap


def settings_from(args):
    checks = args.checks
    if args.no_second_opinion and checks in (None, "full"):
        checks = "reviews"
    if args.no_review:
        checks = "off"
    return {"route": args.route, "mode": args.mode, "effort": args.effort, "checks": checks,
            "lead_model": args.lead_model, "lead_effort": args.lead_effort, "rules": args.rules, "models": args.models,
            "dry_run": args.dry_run, "unattended": args.unattended,
            "resume": "1" if args.cont else args.resume}


def main():
    argv = sys.argv[1:]
    if argv and argv[0] == "demo":
        from . import ui_demo
        sys.exit(ui_demo.main())
    if argv and argv[0] in ("doctor", "rules", "log", "update", "login", "init", "shared", "memory",
                            "permissions", "usage", "bench", "models"):
        from . import tools
        sys.exit(tools.main(argv))
    args, _unknown = chat_parser().parse_known_args(argv)  # AoE may append agent flags; ignore them
    if args.models and not pathlib.Path(args.models).is_file():
        sys.exit(f"claudex: --models {args.models}: no such file (make one with `claudex models --init`)")
    settings = settings_from(args)
    try:
        if args.acp:
            from .server import serve
            asyncio.run(serve(settings))
        else:
            from .cli import run
            sys.exit(asyncio.run(run(args, settings)))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
