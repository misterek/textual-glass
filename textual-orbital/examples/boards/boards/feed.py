"""Fake data commands for the demo and the tests: stdlib only, deterministic from the time.

Values are smooth waves plus a little hashed noise, so they move as time passes, and a re-run in the same
second gives the same output. Each prints one of the formats a panel reads.

    python boards/feed.py series api web [--base 100] [--swing 30]   a metrics query API's JSON (a matrix) over
                                                                      $PANEL_START..$PANEL_END every $PANEL_STEP
    python boards/feed.py value [--base 50] [--swing 20]              one number
    python boards/feed.py labels api web db [--base 50] [--swing 40]  label number lines
    python boards/feed.py table                                       a services table that changes by the minute

Every command takes ``--at T`` (unix seconds) to pretend it's another time; otherwise it uses $PANEL_END,
or now.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import time


def _unit(*parts: object) -> float:
    """A stable number in [0, 1) from the parts (``hash()`` changes between runs; this doesn't)."""
    digest = hashlib.sha256(":".join(map(str, parts)).encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


def wave(name: str, t: float, base: float, swing: float) -> float:
    """The value of series ``name`` at unix time ``t``: two slow waves and a little noise, around base."""
    phase = _unit(name, "phase") * 2 * math.pi
    slow = math.sin(2 * math.pi * t / 600 + phase)
    faster = math.sin(2 * math.pi * t / 137 + 2 * phase)
    noise = _unit(name, int(t)) * 2 - 1
    return round(base + swing * (0.65 * slow + 0.25 * faster + 0.1 * noise), 2)


def _env_int(name: str) -> int | None:
    value = os.environ.get(name, "").strip()
    try:
        return int(float(value)) if value else None
    except ValueError:
        return None


def _now(args: argparse.Namespace) -> int:
    if args.at is not None:
        return int(args.at)
    end = _env_int("PANEL_END")
    return end if end is not None else int(time.time())


def series(args: argparse.Namespace) -> str:
    end = _now(args)
    start = _env_int("PANEL_START")
    if args.at is not None or start is None:
        start = end - 900
    step = max(1, _env_int("PANEL_STEP") or 15)
    first = -(-start // step) * step  # points on whole steps, so every run agrees on them
    times = range(first, end + 1, step)
    result = [{"metric": {"instance": name},
               "values": [[t, f"{wave(name, t, args.base, args.swing):g}"] for t in times]}
              for name in args.names]
    return json.dumps({"status": "success", "data": {"resultType": "matrix", "result": result}})


def value(args: argparse.Namespace) -> str:
    return f"{wave('value', _now(args), args.base, args.swing):g}"


def labels(args: argparse.Namespace) -> str:
    t = _now(args)
    return "\n".join(f"{name} {wave(name, t, args.base, args.swing):g}" for name in args.names)


def _age(seconds: int) -> str:
    """Short ages: 45s, 12m, 3h12m, 5d."""
    if seconds < 120:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 180:
        return f"{minutes}m" if minutes < 60 else f"{minutes // 60}h{minutes % 60}m"
    hours = minutes // 60
    return f"{hours}h" if hours < 48 else f"{hours // 24}d"


def table(args: argparse.Namespace) -> str:
    """Six services. ``billing-worker`` restarts every 10 minutes and is Starting for its first two; ``cache`` is
    Unhealthy for two minutes in every seven."""
    t = _now(args)
    minute = t // 60
    worker_age = t % 600
    cache_bad = minute % 7 in (3, 4)
    rows = [
        ("NAME", "VERSION", "STATUS", "RESTARTS", "UPTIME"),
        ("checkout-api", "2.14.1", "Running", "0", _age(3 * 86400 + t % 86400)),
        ("checkout-web", "2.14.1", "Running", "0", _age(3 * 86400 + t % 86400)),
        ("search", "1.8.0", "Running", "1", _age(26 * 3600 + t % 3600)),
        ("billing-worker", "3.2.0", "Starting" if worker_age < 120 else "Running", "0", _age(worker_age)),
        ("cache", "7.2.4", "Unhealthy" if cache_bad else "Running",
         f"{4 + (minute // 7) % 20}" + (" (40s ago)" if cache_bad else ""), _age(5 * 3600 + t % 3600)),
        ("db", "16.4", "Running", "0", _age(12 * 86400 + t % 86400)),
    ]
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    return "\n".join("   ".join(cell.ljust(w) for cell, w in zip(row, widths, strict=True)).rstrip() for row in rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="feed.py", description="Fake data commands for the boards demo.")
    commands = parser.add_subparsers(dest="command", required=True)

    def command(name: str, base: float, swing: float, names: bool) -> argparse.ArgumentParser:
        sub = commands.add_parser(name)
        if names:
            sub.add_argument("names", nargs="+", metavar="NAME")
        sub.add_argument("--base", type=float, default=base)
        sub.add_argument("--swing", type=float, default=swing)
        sub.add_argument("--at", type=float, help="unix seconds to pretend it is")
        return sub

    command("series", 100, 30, True).set_defaults(run=series)
    command("value", 50, 20, False).set_defaults(run=value)
    command("labels", 50, 40, True).set_defaults(run=labels)
    sub = commands.add_parser("table")
    sub.add_argument("--at", type=float, help="unix seconds to pretend it is")
    sub.set_defaults(run=table)
    args = parser.parse_args(argv)
    sys.stdout.write(args.run(args) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
