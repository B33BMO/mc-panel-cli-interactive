
from __future__ import annotations
import socket, struct, os
from typing import Optional

def _pack(req_id: int, kind: int, body: str) -> bytes:
    data = struct.pack("<ii", req_id, kind) + body.encode("utf-8") + b"\x00\x00"
    return struct.pack("<i", len(data)) + data

def _recv(sock: socket.socket) -> tuple[int, int, str]:
    raw_len = sock.recv(4)
    if len(raw_len) < 4:
        raise ConnectionError("RCON short read")
    (ln,) = struct.unpack("<i", raw_len)
    data = b""
    while len(data) < ln:
        chunk = sock.recv(ln - len(data))
        if not chunk:
            raise ConnectionError("RCON closed")
        data += chunk
    req_id, kind = struct.unpack("<ii", data[:8])
    body = data[8:-2].decode("utf-8", "ignore")
    return req_id, kind, body

class RconClient:
    def __init__(self, host="127.0.0.1", port: Optional[int]=None, password: Optional[str]=None, timeout=5.0):
        self.host = host
        self.port = port or int(os.environ.get("RCON_PORT", "25575"))
        self.password = password or os.environ.get("RCON_PASSWORD", "changeme123")
        self.timeout = timeout

    def command(self, cmd: str) -> str:
        with socket.create_connection((self.host, self.port), timeout=self.timeout) as s:
            s.settimeout(self.timeout)
            s.sendall(_pack(1, 3, self.password))
            rid, kind, body = _recv(s)
            if rid == -1:
                raise PermissionError("RCON auth failed")
            s.sendall(_pack(2, 2, cmd))
            _, _, resp = _recv(s)
            return resp
