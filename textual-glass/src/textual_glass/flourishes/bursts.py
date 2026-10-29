"""Bursts: flourishes that explode out of (or fall into) a point. No Textual imports."""

from __future__ import annotations

from . import Flourish, Stage
from ._draw import soft_flash


def sunburst(stage: Stage) -> tuple[list[bytes], list[int]]:
    """Rays burst out of the anchor's centre, then fade."""
    return soft_flash(stage)


def supernova(stage: Stage) -> tuple[list[bytes], list[int]]:
    """A flash that swells into a shockwave ring and scatters debris."""
    return soft_flash(stage)


def pulsar(stage: Stage) -> tuple[list[bytes], list[int]]:
    """A bright star sends out a few sharp rings, then stops."""
    return soft_flash(stage)


def black_hole(stage: Stage) -> tuple[list[bytes], list[int]]:
    """Light swirls into the anchor's centre and vanishes."""
    return soft_flash(stage)


KINDS = {
    "sunburst": Flourish(sunburst, grow=(8, 4), color="warning", accent="accent",
                         about="a win, right here"),
    "supernova": Flourish(supernova, grow=(12, 5), color="secondary", accent="primary",
                          about="the big win"),
    "pulsar": Flourish(pulsar, grow=(6, 3), color="primary", accent="secondary",
                       about="look here"),
    "black-hole": Flourish(black_hole, grow=(4, 2), color="secondary", accent="primary",
                           about="gone: closed, archived or snoozed"),
}
