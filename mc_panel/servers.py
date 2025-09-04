from __future__ import annotations
import os, signal, time, platform, subprocess, re
from pathlib import Path
from typing import Optional, Dict, Any

from .util import server_dir, pick_java, bytes_fmt

def pid_path(dir: Path) -> Path:
    return dir / "server.pid"

def read_pid(dir: Path) -> Optional[int]:
    p = pid_path(dir)
    if p.exists():
        try:
            return int(p.read_text().strip() or "0")
        except Exception:
            return None
    return None

def running(name: str) -> bool:
    d = server_dir(name)
    pid = read_pid(d)
    if not pid:
        return False
    try:
        import psutil
        proc = psutil.Process(pid)
        return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
    except Exception:
        return False

def _find_jar(d: Path) -> Optional[str]:
    # Prefer known server jars, then any *server*.jar
    for pat in ["fabric-server-launch.jar", "forge-*.jar", "neoforge-*.jar", "server.jar", "*server*.jar"]:
        for p in sorted(d.glob(pat)):
            if p.is_file():
                return p.name
    return None

def _mem_from_start_sh(d: Path) -> tuple[str, str]:
    # Default to 1G/4G if we can't parse start.sh
    xms, xmx = "1G", "4G"
    sh = d / "start.sh"
    if not sh.exists():
        return xms, xmx
    try:
        s = sh.read_text(encoding="utf-8", errors="ignore")
        m1 = re.search(r"-Xms(\S+)", s)
        m2 = re.search(r"-Xmx(\S+)", s)
        if m1: xms = m1.group(1)
        if m2: xmx = m2.group(1)
    except Exception:
        pass
    return xms, xmx

def start(name: str) -> str:
    d = server_dir(name)
    if running(name):
        return "Already running."
    jar = _find_jar(d)
    if not jar:
        return "No server jar found. Try `mccli.py create` again or check the server pack."
    xms, xmx = _mem_from_start_sh(d)

    log_dir = d / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    out_path = log_dir / "console.log"

    with open(out_path, "ab") as out:
        proc = subprocess.Popen(
            [pick_java(), f"-Xms{xms}", f"-Xmx{xmx}", "-jar", jar, "nogui"],
            cwd=d,
            stdout=out, stderr=out,
            start_new_session=True
        )
    (d / "server.pid").write_text(str(proc.pid))
    time.sleep(1.0)
    return "Started."

def stop(name: str, force: bool=False) -> str:
    d = server_dir(name)
    pid = read_pid(d)
    if not pid:
        return "Not running."
    try:
        if platform.system() == "Windows":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F" if force else "/F"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        else:
            os.kill(pid, signal.SIGTERM)
        time.sleep(1.0)
    except Exception:
        pass
    try:
        (d / "server.pid").unlink(missing_ok=True)
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
        "procRss": None
    }
    if pid:
        try:
            p = psutil.Process(pid)
            out["running"] = p.is_running()
            out["procRss"] = p.memory_info().rss
        except Exception:
            pass
    return out
