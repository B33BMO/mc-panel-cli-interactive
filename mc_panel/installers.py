# mc_panel/installers.py
from __future__ import annotations

import io
import json
import os
import subprocess
import zipfile
from pathlib import Path
from typing import Callable, Optional
from urllib.request import Request, urlopen

from .util import (
    ensure_rcon_props,
    pick_java,
    server_dir,
    write_eula,
    write_properties,
)

Step = Callable[[str], None]
UA = "mc-panel/interactive-cli"

def detect_pack_from_bytes(zbytes: bytes) -> tuple[Optional[str], Optional[str], Optional[str]]:
    ml: Optional[str] = None
    mc: Optional[str] = None
    lb: Optional[str] = None

    with zipfile.ZipFile(io.BytesIO(zbytes)) as zf:
        names = set(zf.namelist())

        if "manifest.json" in names:
            try:
                data = json.loads(zf.read("manifest.json").decode("utf-8"))
                mc = (data.get("minecraft") or {}).get("version") or mc
                modloaders = ((data.get("minecraft") or {}).get("modLoaders") or [])
                if modloaders:
                    prim = next((m for m in modloaders if m.get("primary")), None) or modloaders[0]
                    ml_id = (prim.get("id") or "").lower()
                    if "fabric" in ml_id:
                        ml = "fabric"
                    elif "neoforge" in ml_id:
                        ml = "neoforge"
                    elif "forge" in ml_id:
                        ml = "forge"
                    if "-" in ml_id:
                        _, tail = ml_id.split("-", 1)
                        lb = tail.strip() or None
            except Exception:
                pass

        if "modrinth.index.json" in names and not ml:
            try:
                idx = json.loads(zf.read("modrinth.index.json").decode("utf-8"))
                deps = idx.get("dependencies") or {}
                mc = deps.get("minecraft") or mc
                if "fabric-loader" in deps:
                    ml = "fabric"
                    lb = deps.get("fabric-loader") or lb
                elif "neoforge" in deps:
                    ml = "neoforge"
                    lb = deps.get("neoforge") or lb
                elif "forge" in deps:
                    ml = "forge"
                    lb = deps.get("forge") or lb
            except Exception:
                pass

    return ml, mc, lb

def find_runnable_after_extract(dir: Path) -> tuple[Optional[str], Optional[Path]]:
    import glob

    def first_match(pattern: str) -> Optional[str]:
        for p in glob.glob(str(dir / pattern)):
            if "installer" not in os.path.basename(p).lower():
                return p
        return None

    jar_path = (
        first_match("fabric-server-launch.jar")
        or first_match("forge-*.jar")
        or first_match("neoforge-*.jar")
        or (str(dir / "server.jar") if (dir / "server.jar").exists() else None)
        or first_match("*server*.jar")
    )
    jar_name = os.path.basename(jar_path) if jar_path else None

    runner = None
    for rname in ("run.sh", "startserver.sh", "start.sh"):
        rp = dir / rname
        if rp.exists():
            runner = rp
            break

    return jar_name, runner

class Progress:
    def __init__(self, say: Step | None):
        self.say = say
        self.base = 0.0
        self.weight = 0.0

    def start(self, weight: float, msg: str | None = None):
        self.weight = max(0.0, min(1.0, weight))
        if msg:
            self.emit(0.0, msg)

    def emit(self, ratio: float, msg: str):
        pct = int(round((self.base + self.weight * max(0.0, min(1.0, ratio))) * 100))
        if self.say:
            self.say(f"{pct}% {msg}")

    def end(self, msg: str | None = None):
        self.base = min(1.0, self.base + self.weight)
        self.weight = 0.0
        if msg and self.say:
            self.say(f"{int(round(self.base * 100))}% {msg}")

def http_open(url: str):
    req = Request(url, headers={"User-Agent": UA})
    return urlopen(req)

def download_stream(url: str, dest: Path, prog: Optional[Progress] = None, label: str = "Downloading") -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with http_open(url) as r, open(dest, "wb") as f:
        total = int(r.headers.get("Content-Length", "0")) or 0
        seen = 0
        while True:
            chunk = r.read(1024 * 64)
            if not chunk:
                break
            f.write(chunk)
            seen += len(chunk)
            if total and prog:
                prog.emit(seen / total, f"{label}…")

def http_get_json(url: str):
    with http_open(url) as r:
        return json.loads(r.read().decode("utf-8"))

def http_get_bytes(url: str) -> bytes:
    with http_open(url) as r:
        return r.read()

def latest_vanilla() -> str:
    data = http_get_json("https://piston-meta.mojang.com/mc/game/version_manifest_v2.json")
    return data["latest"]["release"]

def vanilla_server_url(version: str) -> str:
    data = http_get_json("https://piston-meta.mojang.com/mc/game/version_manifest_v2.json")
    ver = next((v for v in data["versions"] if v["id"] == version), None)
    if not ver:
        raise RuntimeError(f"Unknown Minecraft version: {version}")
    ver_data = http_get_json(ver["url"])
    return ver_data["downloads"]["server"]["url"]

def fabric_installer_url() -> str:
    items = http_get_json("https://meta.fabricmc.net/v2/versions/installer")
    v = next((x for x in items if x.get("stable")), items[0])
    ver = v["version"]
    return f"https://maven.fabricmc.net/net/fabricmc/fabric-installer/{ver}/fabric-installer-{ver}.jar"

def forge_installer_url(mc: str) -> str:
    promos = http_get_json("https://files.minecraftforge.net/net/minecraftforge/forge/promotions_slim.json")
    p = promos.get("promos", {})
    build = p.get(f"{mc}-recommended") or p.get(f"{mc}-latest")
    if not build:
        raise RuntimeError(f"No Forge build for {mc}")
    ver = f"{mc}-{build}"
    return f"https://maven.minecraftforge.net/net/minecraftforge/forge/{ver}/forge-{ver}-installer.jar"

def neoforge_installer_url(mc: str) -> str:
    xml = http_get_bytes("https://maven.neoforged.net/releases/net/neoforged/neoforge/maven-metadata.xml").decode("utf-8", "ignore")
    import re
    versions = re.findall(r"<version>([^<]+)</version>", xml)
    if not versions:
        raise RuntimeError("NeoForge metadata empty")
    parts = mc.split(".")
    line = (parts[0] == "1" and parts[1] or parts[0]) + "."
    candidates = [v for v in versions if v.startswith(line)]
    chosen = candidates[-1] if candidates else versions[-1]
    return f"https://maven.neoforged.net/releases/net/neoforged/neoforge/{chosen}/neoforge-{chosen}-installer.jar"

def forge_installer_url_for_build(mc: str, build: str) -> str:
    ver = f"{mc}-{build}"
    return f"https://maven.minecraftforge.net/net/minecraftforge/forge/{ver}/forge-{ver}-installer.jar"

def neoforge_installer_url_for_build(build: str) -> str:
    return f"https://maven.neoforged.net/releases/net/neoforged/neoforge/{build}/neoforge-{build}-installer.jar"

def make_scripts(dir: Path, xmx: str, xms: str):
    sh = dir / "start.sh"
    content_sh = f"""#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

JAVA_BIN="${{JAVA_BIN:-{pick_java()}}}"
mkdir -p logs
: > logs/console.log

JAR=""

# Jar detection
shopt -s nullglob
candidates=(fabric-server-launch.jar fabric-server-launcher.jar fabric-installer*.jar forge-*.jar neoforge-*.jar server.jar *server*.jar *.jar)
for pat in "${{candidates[@]}}"; do
  for f in $pat; do
    low="${{f,,}}"
    if [[ "$low" == fabric-installer*.jar || "$low" == fabric-server-launch*.jar || "$low" == fabric-server-launcher*.jar ]]; then
      JAR="$f"; break 2
    fi
    if [[ "$low" != *install* ]]; then
      JAR="$f"; break 2
    fi
  done
done
shopt -u nullglob

if [[ -z "$JAR" ]]; then
  echo "[start.sh] No server jar found in $(pwd)" >> logs/console.log
  ls -1 *.jar 2>/dev/null >> logs/console.log || true
  exit 1
fi

XMS="${{XMS:-{xms}}}"
XMX="${{XMX:-{xmx}}}"

# Fabric rule
if [[ "${{JAR,,}}" == fabric-server-launch*.jar || "${{JAR,,}}" == fabric-server-launcher*.jar || "${{JAR,,}}" == fabric-installer*.jar ]]; then
  nohup "$JAVA_BIN" -jar "$JAR" >> logs/console.log 2>&1 &
  echo $! > server.pid
  exit 0
fi

nohup "$JAVA_BIN" -Xms"$XMS" -Xmx"$XMX" -jar "$JAR" nogui >> logs/console.log 2>&1 &
echo $! > server.pid
exit 0
"""
    sh.write_text(content_sh, encoding="utf-8")
    os.chmod(sh, 0o755)


def make_scripts(dir: Path, xmx: str, xms: str):
    sh = dir / "start.sh"
    content_sh = f'''#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

JAVA_BIN="${{JAVA_BIN:-{pick_java()}}}"
mkdir -p logs
: > logs/console.log

JAR=""

shopt -s nullglob
candidates=(fabric-server-launch.jar fabric-server-launcher.jar fabric-installer*.jar forge-*.jar neoforge-*.jar server.jar *server*.jar *.jar)
for pat in "${{candidates[@]}}"; do
  for f in $pat; do
    low="${{f,,}}"
    if [[ "$low" == fabric-installer*.jar || "$low" == fabric-server-launch*.jar || "$low" == fabric-server-launcher*.jar ]]; then
      JAR="$f"; break 2
    fi
    if [[ "$low" != *install* ]]; then
      JAR="$f"; break 2
    fi
  done
done
shopt -u nullglob

if [[ -z "$JAR" ]]; then
  echo "[start.sh] No server jar found in $(pwd)" >> logs/console.log
  ls -1 *.jar 2>/dev/null >> logs/console.log || true
  exit 1
fi

XMS="${{XMS:-{xms}}}"
XMX="${{XMX:-{xmx}}}"

if [[ "${{JAR,,}}" == fabric-server-launch*.jar || "${{JAR,,}}" == fabric-server-launcher*.jar || "${{JAR,,}}" == fabric-installer*.jar ]]; then
  nohup "$JAVA_BIN" -jar "$JAR" >> logs/console.log 2>&1 &
  echo $! > server.pid
  exit 0
fi

nohup "$JAVA_BIN" -Xms"$XMS" -Xmx"$XMX" -jar "$JAR" nogui >> logs/console.log 2>&1 &
echo $! > server.pid
exit 0
'''
    sh.write_text(content_sh, encoding="utf-8"); os.chmod(sh, 0o755)

    bat = dir / "start.bat"
    bat_content = (
        '@echo off\r\n'
        'cd /d %~dp0\r\n'
        'if not exist logs mkdir logs\r\n'
        'type NUL >> logs\\console.log\r\n'
        f'set "JAVA_BIN={pick_java()}"\r\n'
        'set "JAR="\r\n'
        'for %%f in (fabric-server-launch.jar fabric-server-launcher.jar fabric-installer*.jar forge-*.jar neoforge-*.jar server.jar) do if not defined JAR if exist "%%f" set "JAR=%%f"\r\n'
        'if not defined JAR for %%f in (*server*.jar) do if not defined JAR if exist "%%f" set "JAR=%%f"\r\n'
        'if not defined JAR for %%f in (*.jar) do if not defined JAR if exist "%%f" (\r\n'
        '  echo %%f | find /I "install" >nul || set "JAR=%%f"\r\n'
        ')\r\n'
        'if not defined JAR (\r\n'
        '  echo [start.bat] No server jar found.> logs\\console.log\r\n'
        '  dir /b *.jar >> logs\\console.log\r\n'
        '  exit /b 1\r\n'
        ')\r\n'
        'set "L=__"\r\n'
        'echo %JAR% | find /I "fabric-server-launch" >nul && set "L=fabric"\r\n'
        'echo %JAR% | find /I "fabric-server-launcher" >nul && set "L=fabric"\r\n'
        'echo %JAR% | find /I "fabric-installer" >nul && set "L=fabric"\r\n'
        'if "%L%"=="fabric" (\r\n'
        '  start "" /min "%JAVA_BIN%" -jar "%JAR%"\r\n'
        ') else (\r\n'
        f'  start "" /min "%JAVA_BIN%" -Xms{xms} -Xmx{xmx} -jar "%JAR%" nogui\r\n'
        ')\r\n'
    )
    bat.write_text(bat_content, encoding="utf-8")

def create_server(
    name: str,
    flavor: str = "vanilla",
    version: str = "latest",
    xmx: str = "4G",
    xms: str = "1G",
    port: int = 25565,
    eula: bool = True,
    curseforge_server_zip_url: Optional[str] = None,
    optimize: bool = False,
    say: Step | None = None,
) -> str:
    # (body omitted for brevity in this snippet)
    return ""
