"""Shared pilot-test helpers: capture what the engine writes, and let it settle."""

from __future__ import annotations

import asyncio
import re
from typing import Any

from textual_glass.detection import Support
from textual_glass.testing.kitty_emu import KittyModel


class Output:
    """Collects what is written through the driver and replays it into a kitty model."""

    def __init__(self) -> None:
        self.chunks: list[str] = []

    def __call__(self, app: object, data: str) -> bool:
        self.chunks.append(data)
        return True

    @property
    def text(self) -> str:
        return "".join(self.chunks)

    def mark(self) -> int:
        return len(self.chunks)

    def since(self, mark: int) -> str:
        return "".join(self.chunks[mark:])

    def model(self) -> KittyModel:
        model = KittyModel()
        for body in re.findall(r"\x1b_G([^\x1b]*)\x1b\\", self.text):
            model.apc(body.encode(), (0, 0))
        return model


async def settle(app: Any, pilot: Any, rounds: int = 8) -> None:
    """Let Textual render, run a pixel frame, and wait for worker-thread renders."""
    for _ in range(rounds):
        await pilot.pause()
        app.glass.on_frame()
        app.glass.tick()
        await asyncio.sleep(0.05)
        if not app.glass.pending_jobs:
            await pilot.pause()
            app.glass.on_frame()
            break


async def start(app: Any, pilot: Any) -> None:
    app.glass.support = Support(True, "test")
    app.glass.activate(force=True)
    await idle(app, pilot)


async def idle(app: Any, pilot: Any, timeout: float = 20.0) -> None:
    """Like ``settle``, but waits for slow worker jobs too (big uploads)."""
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    await settle(app, pilot)
    while app.glass.pending_jobs and loop.time() < end:
        await asyncio.sleep(0.05)
        await pilot.pause()
    await settle(app, pilot)
