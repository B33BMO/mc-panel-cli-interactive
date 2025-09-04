#!/usr/bin/env python3
from __future__ import annotations
import argparse, sys, time, re, asyncio
from typing import Optional, Callable
from mc_panel.util import ensure_dirs, SERVERS, server_dir, bytes_fmt
from mc_panel.installers import create_server
from mc_panel.servers import start as srv_start, stop as srv_stop, restart as srv_restart, running, stats

# --- list / basic helpers ----------------------------------------------------

def prompt(text: str, default: Optional[str]=None) -> str:
    s = f"{text}"
    if default is not None:
        s += f" [{default}]"
    s += ": "
    val = input(s).strip()
    return val if val else (default or "")

def yesno(text: str, default: bool=True) -> bool:
    d = "Y/n" if default else "y/N"
    val = input(f"{text} ({d}): ").strip().lower()
    if not val: return default
    return val in ("y","yes","true","1")

def do_list(_args):
    ensure_dirs()
    names = [p.name for p in sorted(SERVERS.iterdir()) if p.is_dir()]
    if not names:
        print("No servers yet. Use: mccli.py create", flush=True)
        return
    for n in names:
        print(f"{'*' if running(n) else ' '} {n}  ({server_dir(n)})")

# --- progress bar for create -------------------------------------------------

def _render_bar(pct: int, msg: str) -> None:
    pct = max(0, min(100, int(pct)))
    bar_len = 32
    filled = int(bar_len * pct / 100)
    bar = "#" * filled + "." * (bar_len - filled)
    sys.stdout.write(f"\r[{bar}] {pct:3d}% {msg[:60]:<60}")
    sys.stdout.flush()
    if pct == 100:
        sys.stdout.write("\n")
        sys.stdout.flush()

def _progress_sink() -> Callable[[str], None]:
    """Parses 'NN% message' lines to draw a single-line progress bar."""
    pat = re.compile(r"^(\d{1,3})%[ ]+(.*)$")
    def sink(m: str) -> None:
        m = m.rstrip("\n")
        mo = pat.match(m)
        if mo:
            _render_bar(int(mo.group(1)), mo.group(2))
        else:
            # move to new line, print plain log, redraw bar next time
            sys.stdout.write("\n" + m + "\n")
            sys.stdout.flush()
    return sink

# --- create/start/stop/restart/logs/rcon/stats ------------------------------

def do_create(args):
    name = args.name or prompt("Name", "mc-new")
    flavor = args.flavor or prompt("Flavor (vanilla/fabric/forge/neoforge)", "vanilla").lower()
    version = args.version or prompt("Minecraft version (or 'latest')", "latest")
    xmx = args.xmx or prompt("Max heap Xmx", "4G")
    xms = args.xms or prompt("Initial heap Xms", "1G")
    port = args.port or int(prompt("Server port", "25565"))
    eula = args.eula if args.eula is not None else yesno("Accept EULA?", True)
    modpack = args.modpack or prompt("CurseForge server ZIP URL (optional)", "")
    modpack = modpack or None

    say = _progress_sink()
    path = create_server(name, flavor, version, xmx, xms, port, eula, modpack, False, say)
    print(f"\nCreated at: {path}")

def do_start(args):
    print(srv_start(args.name))

def do_stop(args):
    print(srv_stop(args.name, force=args.force))

def do_restart(args):
    print(srv_restart(args.name))

def do_logs(args):
    import os
    d = server_dir(args.name)
    paths = [d/'logs'/'latest.log', d/'logs'/'console.log']
    p = next((x for x in paths if x.exists()), paths[-1])
    if not p.exists():
        print("No logs yet.")
        return
    if args.follow:
        print(f"--- Tailing {p} (Ctrl+C to quit) ---")
        with p.open("r", encoding="utf-8", errors="ignore") as f:
            f.seek(0, os.SEEK_END)
            while True:
                line = f.readline()
                if not line:
                    time.sleep(0.25)
                    continue
                sys.stdout.write(line)
                sys.stdout.flush()
    else:
        print(p.read_text(encoding="utf-8", errors="ignore"))

def do_rcon(args):
    """Opens the prompt_toolkit RCON console with live logs + input bar."""
    try:
        from mc_panel.rcon_ui import run_rcon_ui
    except Exception as e:
        print(f"prompt_toolkit UI not available ({e}); falling back to plain RCON.", flush=True)
        return _fallback_rcon(args)

    # Run the async UI
    try:
        asyncio.run(run_rcon_ui(args.name))
    except KeyboardInterrupt:
        pass

def _fallback_rcon(args):
    from mc_panel.rcon import RconClient
    client = RconClient()
    if args.command:
        print(client.command(args.command))
        return
    print("Interactive RCON. Type /quit to exit.")
    while True:
        cmd = input("> ").strip()
        if cmd.lower() in ("/quit","quit","exit"): break
        if not cmd: continue
        try:
            out = client.command(cmd)
            print(out)
        except Exception as e:
            print(f"[rcon error] {e}")

def do_stats(args):
    s = stats(args.name)
    print(f"CPU: {int(s['cpu'])}%  RAM: {bytes_fmt(s['ramUsed'])} / {bytes_fmt(s['ramTotal'])}")
    if s.get("procRss"):
        print(f"Server RSS: {bytes_fmt(s['procRss'])}")
    print("Running:", "yes" if s.get("running") else "no")

# --- argparse ----------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(prog="mccli.py", description="Minecraft multi-server CLI.")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="List servers").set_defaults(func=do_list)

    pc = sub.add_parser("create", help="Create a server (prompts for missing fields)")
    pc.add_argument("--name")
    pc.add_argument("--flavor", choices=["vanilla","fabric","forge","neoforge"])
    pc.add_argument("--version")
    pc.add_argument("--xmx")
    pc.add_argument("--xms")
    pc.add_argument("--port", type=int)
    pc.add_argument("--eula", action=argparse.BooleanOptionalAction)
    pc.add_argument("--modpack")
    pc.set_defaults(func=do_create)

    ps = sub.add_parser("start", help="Start a server")
    ps.add_argument("name")
    ps.set_defaults(func=do_start)

    pstop = sub.add_parser("stop", help="Stop a server")
    pstop.add_argument("name")
    pstop.add_argument("--force", action="store_true")
    pstop.set_defaults(func=do_stop)

    pr = sub.add_parser("restart", help="Restart a server")
    pr.add_argument("name")
    pr.set_defaults(func=do_restart)

    pl = sub.add_parser("logs", help="Show or tail logs")
    pl.add_argument("name")
    pl.add_argument("-f","--follow", action="store_true")
    pl.set_defaults(func=do_logs)

    prc = sub.add_parser("rcon", help="Open RCON console (prompt_toolkit)")
    prc.add_argument("name")
    prc.add_argument("command", nargs="?")
    prc.set_defaults(func=do_rcon)

    pst = sub.add_parser("stats", help="Show server/system stats")
    pst.add_argument("name")
    pst.set_defaults(func=do_stats)

    return p

def main(argv=None):
    argv = argv or sys.argv[1:]
    args = build_parser().parse_args(argv)
    return args.func(args)

if __name__ == "__main__":
    raise SystemExit(main())
