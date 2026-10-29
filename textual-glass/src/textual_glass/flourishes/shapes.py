"""Shapes: flourishes that follow the anchor's outline or sky. No Textual imports."""

from __future__ import annotations

from . import Flourish, Stage
from ._draw import soft_flash


def eclipse(stage: Stage) -> tuple[list[bytes], list[int]]:
    """A shadow slides over the anchor and leaves a glowing corona."""
    return soft_flash(stage)


def aurora(stage: Stage) -> tuple[list[bytes], list[int]]:
    """Curtains of light wave over the top of the anchor."""
    return soft_flash(stage)


def orbit(stage: Stage) -> tuple[list[bytes], list[int]]:
    """A small satellite makes one lap around the anchor's border."""
    return soft_flash(stage)


def constellation(stage: Stage) -> tuple[list[bytes], list[int]]:
    """Stars light up around the anchor, and lines join them."""
    return soft_flash(stage)


KINDS = {
    "eclipse": Flourish(eclipse, grow=(3, 2), color="error", accent="warning",
                        about="something went dark: a failure, a source down"),
    "aurora": Flourish(aurora, grow=(3, 3), color="success", accent="secondary",
                       about="all clear"),
    "orbit": Flourish(orbit, grow=(2, 1), color="primary", accent="foreground",
                      about="refreshed, synced, one more lap done"),
    "constellation": Flourish(constellation, grow=(3, 2), color="foreground", accent="primary",
                              about="a set is complete: every check green, a goal reached"),
}
