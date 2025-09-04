
import os
from pathlib import Path

HOME = Path(os.environ.get("MCPANEL_HOME", Path.home() / ".mc-panel")).expanduser()
SERVERS = HOME / "servers"

def ensure_dirs():
    SERVERS.mkdir(parents=True, exist_ok=True)

def server_dir(name: str) -> Path:
    ensure_dirs()
    return SERVERS / name

def write_eula(dir: Path, accept: bool = True):
    (dir / "eula.txt").write_text(f"eula={'true' if accept else 'false'}\n", encoding="utf-8")

def read_properties(path: Path) -> dict:
    props = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line=line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k,v = line.split("=",1)
                props[k]=v
    return props

def write_properties(path: Path, updates: dict):
    props = read_properties(path)
    props.update({k:str(v) for k,v in updates.items()})
    lines = [f"{k}={v}" for k,v in props.items()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

def ensure_rcon_props(dir: Path, port: int = 25575, password: str = None):
    if password is None:
        password = os.environ.get("RCON_PASSWORD", "changeme123")
    write_properties(dir / "server.properties", {
        "enable-rcon": "true",
        "rcon.port": str(port),
        "rcon.password": password,
        "enable-query": "true",
    })

def pick_java() -> str:
    return os.environ.get("JAVA_BIN", "java")

def bytes_fmt(n: int) -> str:
    units=["B","KB","MB","GB","TB"]
    i=0; x=float(n)
    while x>=1024 and i<len(units)-1:
        x/=1024; i+=1
    return f"{x:.1f} {units[i]}"
