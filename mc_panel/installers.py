from __future__ import annotations
import os, json, subprocess, zipfile, io
from pathlib import Path
from typing import Callable, Optional
from urllib.request import urlopen, Request

from .util import server_dir, write_eula, write_properties, ensure_rcon_props, pick_java

Step = Callable[[str], None]
UA = "mc-panel/interactive-cli"
# --- pack detection helpers --------------------------------------------------

def detect_pack_from_bytes(zbytes: bytes) -> tuple[Optional[str], Optional[str]]:
    """
    Return (loader, mc_version) where loader in {"fabric","forge","neoforge"} if detected.
    Looks inside CurseForge 'manifest.json' or Modrinth 'modrinth.index.json'.
    """
    import re
    ml = None
    mc = None
    with zipfile.ZipFile(io.BytesIO(zbytes)) as zf:
        names = set(zf.namelist())

def detect_pack_from_bytes(zbytes: bytes) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Return (loader, mc_version, loader_build) where:
      loader in {"fabric","forge","neoforge"} if detected,
      mc_version like "1.20.1" if detected,
      loader_build like "47.3.5" (forge) / "20.4.192" (neoforge) / "0.15.11" (fabric) if present.
    Looks inside CurseForge 'manifest.json' or Modrinth 'modrinth.index.json'.
    """
    ml: Optional[str] = None
    mc: Optional[str] = None
    lb: Optional[str] = None  # loader build/version

    with zipfile.ZipFile(io.BytesIO(zbytes)) as zf:
        names = set(zf.namelist())

        # 1) CurseForge manifest.json
        if "manifest.json" in names:
            try:
                data = json.loads(zf.read("manifest.json").decode("utf-8"))
                mc = (data.get("minecraft") or {}).get("version") or mc
                modloaders = ((data.get("minecraft") or {}).get("modLoaders") or [])
                if modloaders:
                    prim = next((m for m in modloaders if m.get("primary")), None) or modloaders[0]
                    ml_id = (prim.get("id") or "").lower()  # e.g. 'forge-47.3.5'
                    if "fabric" in ml_id:
                        ml = "fabric"
                    elif "neoforge" in ml_id:
                        ml = "neoforge"
                    elif "forge" in ml_id:
                        ml = "forge"
                    # extract build after the hyphen if present
                    if "-" in ml_id:
                        _, tail = ml_id.split("-", 1)
                        lb = tail.strip() or None
            except Exception:
                pass

        # 2) Modrinth modrinth.index.json
        if not ml and "modrinth.index.json" in names:
            try:
                idx = json.loads(zf.read("modrinth.index.json").decode("utf-8"))
                deps = idx.get("dependencies") or {}
                mc = deps.get("minecraft") or mc
                # Modrinth deps may include pinned loader versions
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
    """
    Look for a runnable server jar or a runner script in an extracted pack.
    Return (jar_name, runner_script_path) where one of them may be None.
    """
    import glob
    # Known jars first
    candidates = []
    candidates += glob.glob(str(dir / "fabric-server-launch.jar"))
    candidates += glob.glob(str(dir / "forge-*.jar"))
    candidates += glob.glob(str(dir / "neoforge-*.jar"))
    # vanilla fallback
    if not candidates:
        if (dir / "server.jar").exists():
            candidates = [str(dir / "server.jar")]
        else:
            candidates = glob.glob(str(dir / "*server*.jar"))

    jar_name = os.path.basename(candidates[0]) if candidates else None

    # Runner scripts some packs include
    runner = None
    for rname in ("run.sh", "startserver.sh", "start.sh"):
        rp = dir / rname
        if rp.exists():
            runner = rp
            break

    return jar_name, runner

# ------------------------ progress ------------------------

class Progress:
    def __init__(self, say: Step | None):
        self.say = say
        self.base = 0.0
        self.weight = 0.0
    def start(self, weight: float, msg: str | None = None):
        self.weight = max(0.0, min(1.0, weight))
        if msg: self.emit(0.0, msg)
    def emit(self, ratio: float, msg: str):
        pct = int(round((self.base + self.weight * max(0.0, min(1.0, ratio))) * 100))
        if self.say: self.say(f"{pct}% {msg}")
    def end(self, msg: str | None = None):
        self.base = min(1.0, self.base + self.weight)
        self.weight = 0.0
        if msg and self.say: self.say(f"{int(round(self.base*100))}% {msg}")

# ------------------------ http ----------------------------

def http_open(url: str):
    req = Request(url, headers={"User-Agent": UA})
    return urlopen(req)  # caller closes

def download_stream(url: str, dest: Path, prog: Progress | None = None, label: str = "Downloading") -> None:
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

# ------------------------ meta urls -----------------------

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
    # Forge artifact format: {mc}-{build}, e.g. 1.20.1-47.3.5
    ver = f"{mc}-{build}"
    return f"https://maven.minecraftforge.net/net/minecraftforge/forge/{ver}/forge-{ver}-installer.jar"

def neoforge_installer_url_for_build(build: str) -> str:
    # NeoForge artifact directory is just the build number (e.g. 20.4.192)
    return f"https://maven.neoforged.net/releases/net/neoforged/neoforge/{build}/neoforge-{build}-installer.jar"

# ------------------------ scripts -------------------------

def make_scripts(dir: Path, xmx: str, xms: str):
    sh = dir / "start.sh"
    bat = dir / "start.bat"
    content_sh = f'''#!/usr/bin/env bash
cd "$(dirname "$0")"
JAVA_BIN="${{JAVA_BIN:-{pick_java()}}}"
JAR=""
# try explicit order first
for C in "fabric-server-launch.jar" "forge-*.jar" "neoforge-*.jar" "server.jar"; do
  CAND=$(ls -1 $C 2>/dev/null | grep -vi installer | head -n1)
  if [ -n "$CAND" ]; then JAR="$CAND"; break; fi
done
# generic fallback: any *server*.jar that isn't an installer
if [ -z "$JAR" ]; then
  JAR=$(ls -1 *server*.jar 2>/dev/null | grep -vi installer | head -n1)
fi

mkdir -p logs
nohup "$JAVA_BIN" -Xms{xms} -Xmx{xmx} -jar "$JAR" nogui >> logs/console.log 2>&1 &
echo $! > server.pid
exit 0
'''
    sh.write_text(content_sh, encoding="utf-8"); os.chmod(sh, 0o755)
    content_bat = (
        '@echo off\r\n'
        'cd /d %~dp0\r\n'
        f'set "JAVA_BIN={pick_java()}"\r\n'
        'for %%f in (fabric-server-launch.jar forge-*.jar neoforge-*.jar server.jar) do (\r\n'
        '  if exist "%%f" set "JAR=%%f"\r\n'
        ')\r\n'
        'if not defined JAR for %%f in (*server*.jar) do ( if exist "%%f" set "JAR=%%f" )\r\n'
        'if not defined JAR echo No server jar found.& exit /b 1\r\n'
        'mkdir logs 2>nul\r\n'
        'start "" /min "%JAVA_BIN%" -Xms' + xms + ' -Xmx' + xmx + ' -jar "%JAR%" nogui\r\n'
    )
    bat.write_text(content_bat, encoding="utf-8")

# ------------------------ create -------------------------------------------

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
    """
    Emit progress as 'NN% message' so the CLI draws a progress bar.
    If a CurseForge/Modrinth server ZIP is provided:
      - extract it
      - if no runnable jar/runner found, detect modloader + mc version and install that loader automatically
      - if detection fails, fall back to the passed `flavor`
    """
    p = Progress(say)
    def tell(m: str):
        if say: say(m)

    dir = server_dir(name)
    dir.mkdir(parents=True, exist_ok=True)

    # Base setup
    p.start(0.05, f'Preparing “{name}”…')
    if version.lower() == "latest":
        version = latest_vanilla()
    write_eula(dir, accept=eula)
    ensure_rcon_props(dir)
    write_properties(dir / "server.properties", {
        "server-port": str(port),
        "motd": f"{name} on mc-panel",
    })
    p.end(f"Using Minecraft {version}.")

    detected_loader: Optional[str] = None
    detected_mc: Optional[str] = None
    detected_build: Optional[str] = None

    # If a server pack is provided, extract + try to run as-is
    if curseforge_server_zip_url:
        p.start(0.25, "Fetching server pack…")
        zip_path = dir / "cf-server-pack.zip"
        # Read bytes (lets us sniff metadata)
        with http_open(curseforge_server_zip_url) as r:
            data = r.read()
        zip_path.write_bytes(data)
        p.end("Server pack downloaded.")

        # Detect loader/mc/build
        try:
            detected_loader, detected_mc, detected_build = detect_pack_from_bytes(data)
        except Exception:
            detected_loader, detected_mc, detected_build = None, None, None

        # Extract
        p.start(0.15, "Extracting server pack…")
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            zf.extractall(dir)
        p.end("Server pack extracted.")

        # Try to find runnable jar or a pack-provided runner
        jar_name, runner = find_runnable_after_extract(dir)
        if jar_name:
            p.start(0.10, f"Creating launch scripts for {jar_name}…")
            make_scripts(dir, xmx=xmx, xms=xms)
            p.end("Launch scripts ready.")
            p.start(1.0 - p.base, "Finalizing…")
            p.end("Finished setup.")
            tell("100% Done.")
            return str(dir)

        # No jar found. Prefer installing the detected modloader rather than executing unknown runners.
        if detected_loader:
            if detected_mc:
                version = detected_mc
            tell(f"No server jar found; installing detected {detected_loader} ({version}{' / '+detected_build if detected_build else ''})")
            flavor = detected_loader
        else:
            tell("No server jar found; could not detect pack loader — falling back to chosen flavor.")

    # Installer path (either no pack, or pack had no jar and we fell back / detected)
    if flavor == "vanilla":
        url = vanilla_server_url(version)
        jar = dir / "server.jar"
        p.start(0.40, "Downloading vanilla server…")
        download_stream(url, jar, p, "Downloading server")
        p.end("Vanilla server downloaded.")

    elif flavor == "fabric":
        inst = fabric_installer_url()
        inst_jar = dir / "fabric-installer.jar"
        p.start(0.10, "Fetching Fabric installer…")
        download_stream(inst, inst_jar, p, "Fetching installer")
        p.end("Installer ready.")
        p.start(0.25, "Running Fabric installer…")
        # NOTE: We don't pin loader version here (works fine for most packs). 
        # If you want to enforce a specific loader build (detected_build), we can add '-loader <ver>'.
        r = subprocess.run(
            [pick_java(), "-jar", str(inst_jar), "server", "-mcversion", version, "-downloadMinecraft"],
            cwd=dir
        )
        if r.returncode != 0:
            raise RuntimeError("Fabric installer failed")
        p.end("Fabric installed.")

    elif flavor == "forge":
        # If pack pinned a Forge build (e.g. 47.3.5), use it; else choose recommended for mc.
        if detected_build:
            url = forge_installer_url_for_build(version, detected_build)
        else:
            url = forge_installer_url(version)
        inst_jar = dir / "forge-installer.jar"
        p.start(0.10, "Fetching Forge installer…")
        download_stream(url, inst_jar, p, "Fetching installer")
        p.end("Installer ready.")
        p.start(0.25, "Running Forge installer…")
        r = subprocess.run([pick_java(), "-jar", str(inst_jar), "--installServer"], cwd=dir)
        if r.returncode != 0:
            raise RuntimeError("Forge installer failed")
        p.end("Forge installed.")

    elif flavor == "neoforge":
        # If pack pinned a NeoForge build (e.g. 20.4.192), use it; else pick latest line for mc.
        if detected_build:
            url = neoforge_installer_url_for_build(detected_build)
        else:
            url = neoforge_installer_url(version)
        inst_jar = dir / "neoforge-installer.jar"
        p.start(0.10, "Fetching NeoForge installer…")
        download_stream(url, inst_jar, p, "Fetching installer")
        p.end("Installer ready.")
        p.start(0.25, "Running NeoForge installer…")
        r = subprocess.run([pick_java(), "-jar", str(inst_jar), "--installServer"], cwd=dir)
        if r.returncode != 0:
            raise RuntimeError("NeoForge installer failed")
        p.end("NeoForge installed.")

    else:
        raise RuntimeError(f"Unknown flavor: {flavor}")

    # Write launchers after installer
    p.start(0.10, "Writing start scripts…")
    make_scripts(dir, xmx=xmx, xms=xms)
    p.end("Launch scripts ready.")

    p.start(1.0 - p.base, "Finalizing…")
    p.end("Finished setup.")
    tell("100% Done.")
    return str(dir)

