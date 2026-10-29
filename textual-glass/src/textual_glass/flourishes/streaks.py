"""Streaks: flourishes that travel. No Textual imports."""

from __future__ import annotations

from . import Flourish, Stage
from ._draw import soft_flash


def comet(stage: Stage) -> tuple[list[bytes], list[int]]:
    """A comet flies from the anchor to the target (``stage.to``; across the anchor without one)."""
    return soft_flash(stage)


def shooting_star(stage: Stage) -> tuple[list[bytes], list[int]]:
    """One thin, quick streak over the anchor."""
    return soft_flash(stage)


def meteors(stage: Stage) -> tuple[list[bytes], list[int]]:
    """A shower of streaks across the whole window."""
    return soft_flash(stage)


def warp(stage: Stage) -> tuple[list[bytes], list[int]]:
    """Stars stretch into streaks out of the anchor's centre: the jump to hyperspace."""
    return soft_flash(stage)


KINDS = {
    "comet": Flourish(comet, grow=(4, 3), color="primary", accent="secondary",
                      about="something moved: it flies from one widget to another"),
    "shooting-star": Flourish(shooting_star, grow=(6, 2), color="foreground", accent="primary",
                              about="small news"),
    "meteors": Flourish(meteors, screen=True, color="primary", accent="warning",
                        about="a lot happened at once"),
    "warp": Flourish(warp, grow=(10, 4), color="primary", accent="secondary",
                     about="launch: a deploy or a release starts"),
}
