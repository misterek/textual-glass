"""What every crew pack shares: the poses, a ``Member``, ``PackError``, and the helpers that read ``pack.json``
and check its values with messages that name the file and the problem. See ``PACKS.md`` for the formats."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ._colors import RGBA, ColorError, parse_hex

POSES = ("empty", "seated", "typing", "done", "hand_up", "waving", "standing", "jolt", "tea", "dozing")
STYLES = ("smooth", "picture")
#: A pose plays at most this many frames (the contract: 1 to 8).
MAX_FRAMES = 8
MIN_GAP, MAX_GAP = 16, 60_000


class PackError(ValueError):
    """A crew pack that can't be loaded. The message names the file (and line) and what is wrong."""


@dataclass(frozen=True)
class Member:
    """One character in a pack: ``id``, ``name`` and ``uniform``, the identity colour (also the label's)."""

    id: str
    name: str
    uniform: tuple[int, int, int]


def read_manifest(folder: Path) -> tuple[dict[str, Any], str]:
    """The ``pack.json`` of a pack folder as a dict, and its path for messages."""
    root = folder.expanduser()
    manifest_path = root / "pack.json"
    if not root.is_dir():
        raise PackError(f"{root}: not a folder (a pack is a folder with a pack.json)")
    if not manifest_path.is_file():
        raise PackError(f"{root}: no pack.json in this folder")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise PackError(f"{manifest_path}:{e.lineno}: not valid JSON ({e.msg})") from None
    except (OSError, UnicodeDecodeError) as e:
        raise PackError(f"{manifest_path}: can't read it ({e})") from None
    m = str(manifest_path)
    expect(manifest, dict, m, "the manifest")
    return manifest, m


def expect(value: Any, kind: type | tuple[type, ...], where: str, what: str) -> Any:
    if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
        names = kind.__name__ if isinstance(kind, type) else " or ".join(k.__name__ for k in kind)
        raise PackError(f"{where}: {what} should be {_article(names)}, not {value!r}")
    return value


def _article(name: str) -> str:
    return {"dict": "an object", "list": "a list", "str": "a string", "int": "a whole number",
            "bool": "true or false"}.get(name, name)


def check_keys(obj: dict[str, Any], allowed: tuple[str, ...], where: str) -> None:
    extra = sorted(set(obj) - set(allowed))
    if extra:
        raise PackError(f"{where}: unknown key {', '.join(repr(e) for e in extra)} (allowed: {', '.join(allowed)})")


def hex_color(value: Any, where: str) -> RGBA:
    expect(value, str, where, "the colour")
    try:
        return parse_hex(value)
    except ColorError as e:
        raise PackError(f"{where}: {e}") from None


def color_table(value: Any, where: str) -> dict[str, str]:
    """A ``colors`` object: names to colour expressions (checked when they are resolved)."""
    expect(value, dict, where, "colors")
    for k, v in value.items():
        expect(v, str, f"{where}.{k}", "a colour")
    return dict(value)


def read_member(raw: Any, i: int, m: str, allowed: tuple[str, ...]) -> tuple[Member, dict[str, Any], str]:
    """The common part of a member entry (``id``, ``name``, ``uniform``): the ``Member``, the entry, and where it
    is for messages. ``allowed`` are the keys this style takes."""
    where = f"{m}: members[{i}]"
    expect(raw, dict, where, "a member")
    check_keys(raw, allowed, where)
    mid = expect(raw.get("id"), str, where, "id")
    if not mid:
        raise PackError(f"{where}: id is empty")
    where = f"{m}: member {mid!r}"
    name = expect(raw.get("name", mid), str, where, "name")
    if "uniform" not in raw:
        raise PackError(f"{where}: uniform (the identity colour, \"#rrggbb\") is required")
    uniform = hex_color(raw["uniform"], f"{where}.uniform")
    return Member(mid, name, (uniform[0], uniform[1], uniform[2])), raw, where


def check_ids(members: list[Member], m: str) -> None:
    ids = [member.id for member in members]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise PackError(f"{m}: member id {', '.join(dupes)} is used twice")
