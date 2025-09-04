# mc_panel/servers.py
from __future__ import annotations

import os
import re
import signal
import time
import platform
import subprocess
from pathlib import Path
from typing import Optional, Dict, Any

from .util import server_dir, pick_java, bytes_fmt


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
    # stale pid file, clean it up
    try:
        pid_path(d).unlink(missing_ok=True)
    except Exception:
        pass
    return False


def _find_jar(d: Path) -> Optional[str]:
    """Prefer known, non-installer jars, then any *server*.jar (not *installer*)."""
    def is_server_jar(name: str) -> bool:
        low = name.lower()
        return low.endswith(".jar") and "installer" not in low and "install" not in low

    # 1) explicit favorites
    favs = ["fabric-server-launch.jar"]
    for f in favs:
        p = d / f
        if p.exists() and is_server_jar(p.name):
            return p.name

    # 2) collect all jars in dir (skip installers)
    jars = [p.name for p in d.glob("*.jar") if is_server_jar(p.name)]

    # 3) prefer forge/neoforge/versioned, then vanilla
    prefs = ("forge-", "neoforge-", "minecraft_server", "server")
    for pref in prefs:
        for j in sorted(jars):
            if j.startswith(pref):
                return j

    # 4) fallback
    return jars[0] if jars else None


def _mem_from_start_sh(d: Path) -> tuple[str, str]:
    """Parse Xms/Xmx from start.sh; default to 1G/4G if not found."""
    xms, xmx = "1G", "4G"
    sh = d / "start.sh"
    if not sh.exists():
        return xms, xmx
    try:
        s = sh.read_text(encoding="utf-8", errors="ignore")
        m1 = re.search(r"-Xms(\S+)", s)
        m2 = re.search(r"-Xmx(\S+)", s)
        if m1:
            xms = m1.group(1)
        if m2:
            xmx = m2.group(1)
    except Exception:
        pass
    return xms, xmx


def _ensure_runner_wrapper(d: Path) -> None:
    """
    If only run.sh exists (Forge/NeoForge installer output), create start.sh wrapper
    that backgrounds run.sh and writes server.pid. Idempotent.
    """
    start_sh = d / "start.sh"
    if start_sh.exists():
        return
    run_sh = d / "run.sh"
    if not run_sh.exists():
        return
    start_sh.write_text(
        '#!/usr/bin/env bash\n'
        'cd "$(dirname "$0")"\n'
        'mkdir -p logs\n'
        'chmod +x "./run.sh" 2>/dev/null || true\n'
        'nohup ./run.sh >> logs/console.log 2>&1 &\n'
        'echo $! > server.pid\n'
        'exit 0\n',
        encoding="utf-8",
    )
    os.chmod(start_sh, 0o755)


def _poll_pid_or_detect(d: Path, timeout: float = 10.0) -> Optional[int]:
    """
    Poll for server.pid to appear, else try to detect a java process with cwd==d.
    Returns PID or None.
    """
    t0 = time.time()
    # first, poll for pid file
    while time.time() - t0 < timeout:
        pid = read_pid(d)
        if pid and _proc_is_running(pid):
            return pid
        time.sleep(0.25)

    # fallback: try to find a java process started in this dir
    try:
        import psutil
        for p in psutil.process_iter(["pid", "name", "cwd", "cmdline"]):
            name = (p.info.get("name") or "").lower()
            if "java" not in name:
                continue
            try:
                cwd = p.info.get("cwd")
                if cwd and Path(cwd) == d:
                    pid = int(p.info["pid"])
                    pid_path(d).write_text(str(pid))
                    return pid
            except Exception:
                continue
    except Exception:
        pass
    return None


def start(name: str) -> str:
    """
    Start logic:
      • If pack/installer produced a run.sh (Forge/NeoForge), we use/ensure our wrapper start.sh and spawn it non-blocking.
      • If vanilla/Fabric jar exists, launch the jar directly (non-blocking).
      • Never hang the CLI; we poll briefly for a PID and return.
    """
    d = server_dir(name)
    if running(name):
        return "Already running."

    is_windows = platform.system() == "Windows"
    start_sh = d / "start.sh"
    start_bat = d / "start.bat"
    run_sh = d / "run.sh"

    (d / "logs").mkdir(parents=True, exist_ok=True)

    # ── Unix: prefer our wrapper that backgrounds run.sh (Forge/NeoForge path)
    if not is_windows:
        if run_sh.exists():
            _ensure_runner_wrapper(d)

        if start_sh.exists():
            try:
                os.chmod(start_sh, 0o755)
            except Exception:
                pass
            # Use Popen (never wait) in case a foreign start.sh would block.
            try:
                subprocess.Popen(
                    ["/bin/bash", "./start.sh"],
                    cwd=d,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            except FileNotFoundError:
                subprocess.Popen(
                    ["sh", "./start.sh"],
                    cwd=d,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            pid = _poll_pid_or_detect(d, timeout=12.0)
            return "Started." if pid else "Started (pid unknown yet). Check logs/console.log."

    # ── Windows: use start.bat if present
    else:
        if start_bat.exists():
            subprocess.Popen(
                ["cmd", "/c", "start.bat"],
                cwd=d,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            pid = _poll_pid_or_detect(d, timeout=8.0)
            return "Started." if pid else "Started (pid unknown yet)."

    # ── Fallback: launch a server jar directly (vanilla / Fabric path)
    jar = _find_jar(d)
    if not jar:
        return "No server jar found. Try `mccli.py create` again or check the server pack."
    xms, xmx = _mem_from_start_sh(d)

    out_path = d / "logs" / "console.log"
    with open(out_path, "ab") as out:
        proc = subprocess.Popen(
            [pick_java(), f"-Xms{xms}", f"-Xmx{xmx}", "-jar", jar, "nogui"],
            cwd=d,
            stdout=out,
            stderr=out,
            start_new_session=True,
        )
    pid_path(d).write_text(str(proc.pid))
    time.sleep(1.0)
    return "Started."


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
            os.kill(pid, signal.SIGTERM)
        time.sleep(1.0)
        if force and _proc_is_running(pid):
            try:
                os.kill(pid, signal.SIGKILL)
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

    cpu = psutil.cpu_percent(interval=0.1)
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
