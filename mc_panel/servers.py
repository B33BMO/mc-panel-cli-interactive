# mc_panel/servers.py — simplified direct runner
from __future__ import annotations

import os
import re
import signal
import time
import platform
import subprocess
from pathlib import Path
from typing import Optional, Dict, Any

from .util import server_dir, pick_java, bytes_fmt  # bytes_fmt may be used by callers

# ───────────────────────────── PID helpers ─────────────────────────────

def pid_path(dir: Path) -> Path:
    return dir / "server.pid"

def read_pid(dir: Path) -> Optional[int]:
    p = pid_path(dir)
    if p.exists():
        try:
            return int((p.read_text().strip() or "0"))
        except Exception:
            return None
    return None

def _proc_is_running(pid: int) -> bool:
    try:
        import psutil
        proc = psutil.Process(pid)
        return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
    except Exception:
        return False

def running(name: str) -> bool:
    d = server_dir(name)
    pid = read_pid(d)
    if not pid:
        return False
    if _proc_is_running(pid):
        return True
    try:
        pid_path(d).unlink(missing_ok=True)
    except Exception:
        pass
    return False

# ───────────────────────────── Detection helpers ─────────────────────────────

def _find_fabric_launcher(d: Path) -> Optional[str]:
    """
    Return a Fabric launcher/installer jar name if present, else None.
    We consider these in order:
      - fabric-launcher.jar               (your requested name)
      - fabric-server-launcher.jar
      - fabric-server-launch.jar
      - fabric-installer*.jar             (explicitly allowed)
    """
    prefs = [
        "fabric-launcher.jar",
        "fabric-server-launcher.jar",
        "fabric-server-launch.jar",
    ]
    for name in prefs:
        p = d / name
        if p.exists():
            return p.name

    # fallback: any fabric-installer*.jar
    for p in sorted(d.glob("fabric-installer*.jar")):
        if p.is_file():
            return p.name
    return None

def _find_jar(d: Path) -> Optional[str]:
    """Find a reasonable server jar if not Fabric."""
    def is_server_jar(name: str) -> bool:
        low = name.lower()
        # exclude known non-server
        if low.startswith("fabric-installer"):
            return False  # Fabric handled separately above
        return (
            low.endswith(".jar")
            and "installer" not in low
            and "install" not in low
            and "shim" not in low
            and "client" not in low
        )

    # common priorities
    prefs = ("forge-", "neoforge-", "minecraft_server", "server")
    jars = sorted([p.name for p in d.glob("*.jar") if is_server_jar(p.name)])
    for pref in prefs:
        for j in jars:
            if j.startswith(pref):
                return j
    return jars[0] if jars else None

def _mem_from_env() -> tuple[str, str]:
    xms = os.environ.get("XMS", "1G")
    xmx = os.environ.get("XMX", "12G")
    return xms, xmx

# ───────────────────────────── Start/Stop/Stats ─────────────────────────────

def start(name: str) -> str:
    d = server_dir(name)
    if running(name):
        return "Already running."

    is_windows = platform.system() == "Windows"
    run_sh = d / "run.sh"
    run_bat = d / "run.bat"

    # Ensure log dir/file
    log_dir = d / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    out_path = log_dir / "console.log"

    # 1) Fabric: run the launcher/installer jar with plain -jar (no nogui/heap flags)
    fabric_jar = _find_fabric_launcher(d)
    if fabric_jar:
        with open(out_path, "ab") as out:
            proc = subprocess.Popen(
                [pick_java(), "-jar", fabric_jar],
                cwd=d,
                stdout=out,
                stderr=out,
                start_new_session=True,
            )
        pid_path(d).write_text(str(proc.pid))
        return f"Started Fabric via {fabric_jar}."

    # 2) Forge/NeoForge: prefer run.sh / run.bat if present
    if not is_windows and run_sh.exists():
        # make sure it is executable; run as-is (script usually contains memory flags)
        try:
            os.chmod(run_sh, 0o755)
        except Exception:
            pass
        with open(out_path, "ab") as out:
            proc = subprocess.Popen(
                ["bash", "run.sh"],
                cwd=d,
                stdout=out,
                stderr=out,
                start_new_session=True,
            )
        pid_path(d).write_text(str(proc.pid))
        return "Started Forge/NeoForge (run.sh)."

    if is_windows and run_bat.exists():
        proc = subprocess.Popen(
            ["cmd", "/c", "run.bat"],
            cwd=d,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        pid_path(d).write_text(str(proc.pid))
        return "Started Forge/NeoForge (run.bat)."

    # 3) Vanilla / generic jar fallback
    jar = _find_jar(d)
    if jar:
        xms, xmx = _mem_from_env()
        with open(out_path, "ab") as out:
            proc = subprocess.Popen(
                [pick_java(), f"-Xms{xms}", f"-Xmx{xmx}", "-jar", jar, "nogui"],
                cwd=d,
                stdout=out,
                stderr=out,
                start_new_session=True,
            )
        pid_path(d).write_text(str(proc.pid))
        return f"Started generic jar ({jar})."

    return "No runnable jar or script found."

def stop(name: str, force: bool = False) -> str:
    d = server_dir(name)
    pid = read_pid(d)
    if not pid:
        return "Not running."
    try:
        if platform.system() == "Windows":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F" if force else "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            try:
                os.kill(pid, signal.SIGTERM)
            except Exception:
                pass
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except Exception:
                pass
            time.sleep(1.0)
            if force and _proc_is_running(pid):
                try:
                    os.kill(pid, signal.SIGKILL)
                except Exception:
                    pass
                try:
                    os.killpg(os.getpgid(pid), signal.SIGKILL)
                except Exception:
                    pass
    except ProcessLookupError:
        pass
    except Exception:
        pass
    try:
        pid_path(d).unlink(missing_ok=True)
    except Exception:
        pass
    return "Stopped."

def restart(name: str) -> str:
    stop(name)
    time.sleep(0.5)
    return start(name)

def stats(name: str) -> Dict[str, Any]:
    import psutil
    cpu = psutil.cpu_percent(interval=0.05)
    vm = psutil.virtual_memory()
    d = server_dir(name)
    pid = read_pid(d)
    out: Dict[str, Any] = {
        "cpu": cpu,
        "ramUsed": vm.used,
        "ramTotal": vm.total,
        "running": False,
        "procRss": None,
    }
    if pid:
        try:
            p = psutil.Process(pid)
            out["running"] = p.is_running()
            out["procRss"] = p.memory_info().rss
        except Exception:
            pass
    return out
