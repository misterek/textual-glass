"""Colour expressions for crew packs: ``"#rrggbb"``, a named colour, and a few modifiers.

Grammar (no spaces)::

    expr  := base op* ["@" alpha]
    base  := "#rrggbb" | "#rrggbbaa" | name            name: a-z, 0-9, _
    op    := "*" f          scale the colour (0.8 is darker)
           | "+" f          lighten toward white by f (0 to 1)
           | "-" f          darken toward black by f (0 to 1)
           | "~" hex ":" f  mix toward another colour by f (0 to 1)
    alpha := f              opacity, 0 to 1

``"skin*0.82"``, ``"uniform+0.45"``, ``"skin~#ff6a6a:0.35"``, ``"#ffffff@0.6"``.
"""

from __future__ import annotations

import re
from collections.abc import Callable

RGBA = tuple[int, int, int, int]

_TOKEN = re.compile(r"([*+\-~@])")
_NAME = re.compile(r"^[a-z][a-z0-9_]*$")


class ColorError(ValueError):
    """A colour expression that can't be read or resolved."""


def parse_hex(text: str) -> RGBA:
    """``#rrggbb`` or ``#rrggbbaa`` (also ``#rgb``) as RGBA."""
    h = text.strip().lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) not in (6, 8) or not re.fullmatch(r"[0-9a-fA-F]+", h):
        raise ColorError(f"{text!r} is not a hex colour (#rrggbb or #rrggbbaa)")
    a = int(h[6:8], 16) if len(h) == 8 else 255
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), a


def to_hex(c: tuple[int, ...]) -> str:
    return "#" + "".join(f"{v:02x}" for v in c[:3])


def _number(text: str, expr: str) -> float:
    try:
        return float(text)
    except ValueError:
        raise ColorError(f"{expr!r}: {text!r} is not a number") from None


def _clamp(v: float) -> int:
    return max(0, min(255, int(round(v))))


def evaluate(expr: str, lookup: Callable[[str], RGBA]) -> RGBA:
    """Evaluate ``expr``; ``lookup(name)`` gives a named colour (and raises ``ColorError`` if unknown)."""
    if not isinstance(expr, str) or not expr:
        raise ColorError(f"{expr!r} is not a colour expression")
    parts = _TOKEN.split(expr.replace(" ", ""))
    base = parts[0]
    if base.startswith("#"):
        r, g, b, a = parse_hex(base)
    elif _NAME.match(base):
        r, g, b, a = lookup(base)
    else:
        raise ColorError(f"{expr!r}: {base!r} is neither #hex nor a colour name")
    rf, gf, bf, af = float(r), float(g), float(b), a / 255
    ops = parts[1:]
    if len(ops) % 2:
        raise ColorError(f"{expr!r}: an operator has no value")
    for op, arg in zip(ops[::2], ops[1::2], strict=True):
        if op == "*":
            f = _number(arg, expr)
            rf, gf, bf = rf * f, gf * f, bf * f
        elif op == "+":
            f = _number(arg, expr)
            rf, gf, bf = rf + (255 - rf) * f, gf + (255 - gf) * f, bf + (255 - bf) * f
        elif op == "-":
            f = _number(arg, expr)
            rf, gf, bf = rf * (1 - f), gf * (1 - f), bf * (1 - f)
        elif op == "~":
            target, _, amount = arg.partition(":")
            if not amount:
                raise ColorError(f"{expr!r}: a mix is ~#hex:amount, like ~#ff6a6a:0.35")
            if target.startswith("#"):
                tr, tg, tb, _ta = parse_hex(target)
            elif _NAME.match(target):
                tr, tg, tb, _ta = lookup(target)
            else:
                raise ColorError(f"{expr!r}: {target!r} is neither #hex nor a colour name")
            f = _number(amount, expr)
            rf, gf, bf = rf + (tr - rf) * f, gf + (tg - gf) * f, bf + (tb - bf) * f
        else:  # "@"
            af = _number(arg, expr)
    return _clamp(rf), _clamp(gf), _clamp(bf), _clamp(af * 255)


class Palette:
    """Named colours in layers: the first layer that names a colour wins. Values are expressions, which may
    name other colours (cycles are an error)."""

    def __init__(self, *layers: dict[str, str]) -> None:
        self.layers = layers
        self._cache: dict[str, RGBA] = {}

    def names(self) -> set[str]:
        return {k for layer in self.layers for k in layer}

    def get(self, name: str, _seen: tuple[str, ...] = ()) -> RGBA:
        if name in self._cache:
            return self._cache[name]
        if name in _seen:
            raise ColorError(f"colour {name!r} is defined in terms of itself ({' -> '.join((*_seen, name))})")
        for layer in self.layers:
            if name in layer:
                value = evaluate(layer[name], lambda n: self.get(n, (*_seen, name)))
                self._cache[name] = value
                return value
        raise ColorError(f"no colour named {name!r} (known: {', '.join(sorted(self.names()))})")

    def color(self, expr: str) -> RGBA:
        return evaluate(expr, self.get)
