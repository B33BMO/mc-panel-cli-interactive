
# mc_panel/servers.py — hardened
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

# ───────────────────────────── Jar + mem detection ─────────────────────────────

def _find_jar(d: Path) -> Optional[str]:
    def is_server_jar(name: str) -> bool:
        low = name.lower()
        return low.endswith(".jar") and "installer" not in low and "install" not in low and "shim" not in low and "client" not in low

    # Priority list: fabric launcher first, then common server jar patterns
    for fav in ("fabric-server-launch.jar", "fabric-server-launcher.jar"):
        p = d / fav
        if p.exists() and is_server_jar(p.name):
            return p.name

    jars = [p.name for p in d.glob("*.jar") if is_server_jar(p.name)]
    jars = sorted(jars)
    for pref in ("forge-", "neoforge-", "minecraft_server", "server"):
        for j in jars:
            if j.startswith(pref):
                return j
    return jars[0] if jars else None

def _mem_from_start_sh(d: Path) -> tuple[str, str]:
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

# ───────────────────────────── Runner wrapper ─────────────────────────────

def _ensure_runner_wrapper(d: Path) -> None:
    """
    If a pack generated run.sh (Forge/NeoForge), create a wrapper start.sh that:
      - ensures logs/console.log exists
      - backgrounds the process
      - writes server.pid immediately
    """
    start_sh = d / "start.sh"
    if start_sh.exists():
        return
    run_sh = d / "run.sh"
    if not run_sh.exists():
        return

    start_sh.write_text(
        '#!/usr/bin/env bash\n'
        'set -euo pipefail\n'
        'cd "$(dirname "$0")"\n'
        'mkdir -p logs\n'
        ': > logs/console.log\n'
        'chmod +x "./run.sh" 2>/dev/null || true\n'
        'nohup ./run.sh >> logs/console.log 2>&1 &\n'
        'echo $! > server.pid\n'
        'exit 0\n',
        encoding="utf-8",
    )
    os.chmod(start_sh, 0o755)

def _poll_pid_or_detect(d: Path, timeout: float = 15.0) -> Optional[int]:
    t0 = time.time()
    while time.time() - t0 < timeout:
        pid = read_pid(d)
        if pid and _proc_is_running(pid):
            return pid
        time.sleep(0.25)

    # Fallback: search for java whose cwd == d
    try:
        import psutil
        best: Optional[int] = None
        for p in psutil.process_iter(["pid", "name", "cwd"]):
            name = (p.info.get("name") or "").lower()
            if "java" not in name:
                continue
            try:
                if p.info.get("cwd") and Path(p.info["cwd"]) == d:
                    best = p.info["pid"]
            except Exception:
                continue
        if best:
            pid_path(d).write_text(str(best))
            return best
    except Exception:
        pass
    return None

# ───────────────────────────── Start/Stop/Stats ─────────────────────────────

def start(name: str) -> str:
    d = server_dir(name)
    if running(name):
        return "Already running."

    is_windows = platform.system() == "Windows"
    start_sh = d / "start.sh"
    start_bat = d / "start.bat"
    run_sh = d / "run.sh"

    (d / "logs").mkdir(parents=True, exist_ok=True)

    if not is_windows:
        # Always prepare wrapper if run.sh exists
        if run_sh.exists():
            _ensure_runner_wrapper(d)

        if start_sh.exists():
            try:
                os.chmod(start_sh, 0o755)
            except Exception:
                pass
            # Launch detached so it won't seize the user's terminal
            try:
                proc = subprocess.Popen(
                    ["/bin/bash", "./start.sh"],
                    cwd=d,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            except FileNotFoundError:
                proc = subprocess.Popen(
                    ["sh", "./start.sh"],
                    cwd=d,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            # Record something immediately; wrapper will overwrite with real Java pid
            pid_path(d).write_text(str(proc.pid))
            real = _poll_pid_or_detect(d, timeout=12.0)
            if real and real != proc.pid:
                pid_path(d).write_text(str(real))
            return "Started." if (real or _proc_is_running(proc.pid)) else "Started (pid pending)."

    else:
        if start_bat.exists():
            proc = subprocess.Popen(
                ["cmd", "/c", "start.bat"],
                cwd=d,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            pid_path(d).write_text(str(proc.pid))
            real = _poll_pid_or_detect(d, timeout=8.0)
            if real and real != proc.pid:
                pid_path(d).write_text(str(real))
            return "Started." if (real or _proc_is_running(proc.pid)) else "Started (pid pending)."

    # Fallback: launch jar directly (vanilla/fabric typical path)
        # Fallback: launch jar directly (vanilla/fabric typical path)
    jar = _find_jar(d)
    if not jar:
        return "No server jar found. Try reinstalling or check the server pack."
    xms, xmx = _mem_from_start_sh(d)

    out_path = d / "logs" / "console.log"
    with open(out_path, "ab") as out:
        low = jar.lower()
        is_fabric = (
            low.startswith("fabric-server-launch") or
            low.startswith("fabric-server-launcher") or
            low.startswith("fabric-installer")
        )
        if is_fabric:
            # Fabric rule: run installer/launcher with plain -jar (no nogui/heap flags)
            proc = subprocess.Popen(
                [pick_java(), "-jar", jar],
                cwd=d,
                stdout=out,
                stderr=out,
                start_new_session=True,
            )
        else:
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
            # stop pid and its group
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
