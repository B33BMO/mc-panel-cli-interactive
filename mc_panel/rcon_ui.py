# mc_panel/rcon_ui.py
from __future__ import annotations

import asyncio
import os
import contextlib
from pathlib import Path
from typing import Iterable
from prompt_toolkit.application import Application
from prompt_toolkit.layout import Layout, HSplit
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.widgets import TextArea, Label
from prompt_toolkit.styles import Style
from prompt_toolkit.document import Document
from prompt_toolkit.filters import has_focus

from .util import server_dir, read_properties
from .rcon import RconClient


TAIL_BOOT_BYTES = 64_000  # show last ~64KB from each log when opening
TAIL_POLL = 0.25          # seconds


async def run_rcon_ui(name: str) -> None:
    """Fullscreen RCON console with live logs + an input bar."""
    d = server_dir(name)
    props = read_properties(d / "server.properties")
    port = int(props.get("rcon.port", "25575"))
    password = props.get("rcon.password", os.environ.get("RCON_PASSWORD", "changeme123"))
    enable_rcon = (props.get("enable-rcon", "false").strip().lower() == "true")

    # Log area (scrollable) + input field + status
    log = TextArea(
        style="class:log",
        focusable=False,
        scrollbar=True,
        wrap_lines=False,
        read_only=True,
    )
    input_field = TextArea(height=1, prompt="> ", multiline=False)
    status = Label(text=f"RCON — {name} :{port}    (Ctrl-C / Esc to exit)", style="class:status")

    kb = KeyBindings()
    client = RconClient(port=port, password=password)

    @kb.add("enter", filter=has_focus(input_field))
    async def _(event) -> None:
        cmd = (input_field.text or "").strip()
        if not cmd:
            return
        try:
            out = await asyncio.to_thread(client.command, cmd)
            _append(log, f"$ {cmd}\n{out}\n")
        except Exception as e:
            _append(log, f"[rcon error] {e}\n")
        finally:
            input_field.buffer.document = Document(text="")

    @kb.add("c-c")
    @kb.add("escape")
    def _(event) -> None:
        event.app.exit()

    root = HSplit([status, log, input_field])
    app = Application(
        layout=Layout(root),
        key_bindings=kb,
        full_screen=True,
        style=Style.from_dict(
            {
                "log": "bg:#0e162b #d1d5db",
                "status": "reverse",
            }
        ),
    )

    async def tail_many(paths: Iterable[Path]) -> None:
        # Tail multiple files; read last TAIL_BOOT_BYTES once, then follow.
        offsets: dict[Path, int] = {}
        # Prime buffers with recent content
        for p in paths:
            p.parent.mkdir(parents=True, exist_ok=True)
            try:
                with p.open("rb") as f:
                    f.seek(0, os.SEEK_END)
                    end = f.tell()
                    start = max(0, end - TAIL_BOOT_BYTES)
                    f.seek(start)
                    if start > 0:
                        # drop partial first line
                        f.readline()
                    chunk = f.read()
                    if chunk:
                        try:
                            _append(log, chunk.decode("utf-8", "ignore"))
                        except Exception:
                            pass
                    offsets[p] = end
            except FileNotFoundError:
                offsets[p] = 0

        # Follow
        while True:
            for p in paths:
                try:
                    with p.open("rb") as f:
                        f.seek(offsets.get(p, 0))
                        data = f.read()
                        if data:
                            offsets[p] = f.tell()
                            try:
                                _append(log, data.decode("utf-8", "ignore"))
                            except Exception:
                                pass
                except FileNotFoundError:
                    # file may appear later
                    pass
            await asyncio.sleep(TAIL_POLL)

    async def rcon_probe() -> None:
        # Give a quick, helpful status line.
        if not enable_rcon:
            _append(log, "[hint] RCON appears disabled (enable-rcon=false). Stop the server, set enable-rcon=true in server.properties, and start again.\n")
            return
        try:
            out = await asyncio.to_thread(client.command, "list")
            _append(log, "[rcon] connected. Try commands like: list, say hello, time query daytime\n")
            if out.strip():
                _append(log, out.strip() + "\n")
        except Exception as e:
            _append(log, f"[rcon] cannot connect: {e}\n"
                         f"[hint] Check rcon.port ({port}), rcon.password, firewall, and that the server was started with those settings.\n")

    # Start background tasks
    paths = [
        d / "logs" / "console.log",
        d / "logs" / "latest.log",
        d / "logs" / "debug.log",
    ]
    tail_task = asyncio.create_task(tail_many(paths))
    probe_task = asyncio.create_task(rcon_probe())

    try:
        await app.run_async()
    finally:
        for t in (tail_task, probe_task):
            t.cancel()
        for t in (tail_task, probe_task):
            with contextlib.suppress(asyncio.CancelledError):
                await t


def _append(area: TextArea, text: str) -> None:
    # TextArea is read_only; but buffer is still writable.
    area.buffer.insert_text(text, move_cursor=True)
