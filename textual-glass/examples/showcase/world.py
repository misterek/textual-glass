"""The showcase's fake world: services, incidents, deploys, a runbook and an
activity log. Every name here is made up.

``World`` holds the little that changes (a restart, a release shipping) and
``Tones`` turns the app's Textual theme into hex colours for Rich text and
pixel items, so a theme change from the command palette recolours everything.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

# state -> (label, beacon animation, tone name); tone names are Tones attributes
STATES: dict[str, tuple[str, str, str]] = {
    "down": ("down", "error", "error"),
    "degraded": ("degraded", "error", "warning"),
    "approval": ("approval", "waiting", "waiting"),
    "deploying": ("deploying", "working", "primary"),
    "restarting": ("restarting", "working", "primary"),
    "ok": ("healthy", "done", "success"),
    "paused": ("paused", "idle", "muted"),
}


@dataclass(frozen=True)
class Service:
    key: str
    state: str
    host: str
    version: str
    team: str
    region: str
    p95: int  # ms
    errors: float  # percent of requests
    cpu: float  # 0..1
    rps: int
    attention: int  # 0..10: how much it needs a person right now
    note: str
    latency: tuple[int, ...]  # p95 over the last 12 hours, oldest first

    @property
    def label(self) -> str:
        return STATES[self.state][0]


def _curve(base: int, *bumps: int) -> tuple[int, ...]:
    return tuple(base + b for b in bumps)


SERVICES: tuple[Service, ...] = (
    Service("search", "down", "api-02", "v1.9.2", "discovery", "eu-west", 2310, 18.4, 0.97, 140, 10,
            "Index shard 3 is not answering; the gateway is shedding search traffic.",
            _curve(180, 0, 10, 5, 20, 15, 30, 60, 140, 420, 900, 1600, 2130)),
    Service("checkout", "degraded", "web-03", "v2.13.4", "storefront", "us-east", 812, 2.1, 0.78, 1240, 8,
            "p95 above 800 ms since 13:40; payment provider calls are slow.",
            _curve(300, 10, 0, 20, 40, 30, 90, 150, 240, 380, 470, 500, 512)),
    Service("auth", "approval", "api-01", "v3.2.1", "identity", "us-east", 96, 0.1, 0.41, 2210, 6,
            "v3.2.1 is waiting for a production approval: token rotation fix.",
            _curve(90, 4, 2, 6, 3, 8, 5, 2, 7, 4, 6, 3, 6)),
    Service("ledger", "deploying", "db-02", "v0.9.0", "finance", "eu-west", 148, 0.3, 0.55, 380, 4,
            "Canary at 3 of 5 regions; error rate is flat.",
            _curve(130, 10, 8, 12, 20, 14, 9, 16, 22, 18, 12, 15, 18)),
    Service("media", "degraded", "edge-04", "v4.0.3", "content", "ap-south", 420, 1.2, 0.83, 960, 5,
            "Thumbnail workers are behind by 9 minutes.",
            _curve(210, 20, 30, 25, 60, 80, 110, 150, 170, 190, 205, 200, 210)),
    Service("gateway", "ok", "edge-01", "v5.1.2", "platform", "global", 38, 0.02, 0.62, 8400, 2,
            "Shipped v5.1.2 this morning; all regions green.",
            _curve(36, 2, 1, 3, 2, 4, 3, 1, 2, 3, 2, 1, 2)),
    Service("inventory", "ok", "api-03", "v2.4.0", "storefront", "us-east", 74, 0.05, 0.37, 1520, 1,
            "Nothing to report.",
            _curve(70, 2, 4, 3, 5, 2, 6, 4, 3, 5, 4, 2, 4)),
    Service("notify", "ok", "api-04", "v1.12.7", "platform", "eu-west", 61, 0.08, 0.29, 610, 1,
            "Push and email queues are empty.",
            _curve(55, 3, 6, 4, 2, 5, 7, 3, 4, 6, 5, 4, 6)),
    Service("billing", "ok", "db-01", "v6.0.1", "finance", "us-east", 122, 0.04, 0.48, 330, 1,
            "Month-end run is scheduled for Friday.",
            _curve(115, 5, 3, 7, 4, 6, 8, 5, 6, 4, 7, 5, 7)),
    Service("profile", "ok", "api-05", "v2.2.9", "identity", "us-east", 55, 0.03, 0.22, 1890, 1,
            "Nothing to report.",
            _curve(52, 1, 3, 2, 4, 3, 2, 3, 1, 2, 3, 2, 3)),
    Service("reports", "ok", "batch-01", "v0.18.2", "data", "eu-west", 640, 0.0, 0.66, 12, 1,
            "Nightly exports finished at 04:12.",
            _curve(600, 20, 10, 40, 30, 20, 50, 30, 20, 40, 30, 35, 40)),
    Service("scheduler", "ok", "batch-02", "v3.3.0", "platform", "global", 18, 0.0, 0.14, 90, 0,
            "Nothing to report.",
            _curve(17, 1, 0, 1, 2, 1, 0, 1, 1, 2, 1, 0, 1)),
    Service("mailer", "paused", "batch-03", "v1.4.4", "content", "eu-west", 0, 0.0, 0.0, 0, 0,
            "Scaled to zero until the newsletter on Monday.",
            _curve(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)),
    Service("archive", "paused", "batch-04", "v0.7.1", "data", "eu-west", 0, 0.0, 0.0, 0, 0,
            "Runs on the first of the month.",
            _curve(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)),
)


@dataclass(frozen=True)
class Incident:
    key: str
    severity: int  # 1 is the worst
    service: str
    title: str
    age: str
    owner: str


INCIDENTS: tuple[Incident, ...] = (
    Incident("INC-231", 1, "search", "Search returns 503 for a third of queries", "14m", "sam"),
    Incident("INC-230", 2, "checkout", "p95 above 800 ms on checkout", "38m", "priya"),
    Incident("INC-229", 3, "media", "Thumbnail queue backlog", "1h", "lee"),
    Incident("INC-226", 3, "notify", "Duplicate push notifications on Android", "3h", "dana"),
    Incident("INC-224", 4, "reports", "CSV export drops the last row", "1d", "alex"),
    Incident("INC-221", 4, "profile", "Avatar upload is slow on large files", "2d", "jo"),
)


@dataclass(frozen=True)
class Deploy:
    service: str
    version: str
    target: str
    status: str  # rolling, waiting, building, scheduled, done
    detail: str


DEPLOYS: tuple[Deploy, ...] = (
    Deploy("checkout", "v2.14.0", "prod", "waiting", "ready to ship"),
    Deploy("auth", "v3.2.1", "prod", "waiting", "needs approval"),
    Deploy("ledger", "v0.9.0", "prod", "rolling", "canary 3/5"),
    Deploy("search", "v1.9.3", "staging", "building", "hotfix build"),
    Deploy("billing", "v6.1.0", "prod", "scheduled", "Friday 09:00"),
    Deploy("gateway", "v5.1.2", "prod", "done", "live since 13:02"),
)

#: The release the modal ships, and how far its rollout is.
RELEASE = ("checkout", "v2.14.0")

DAYS = ("M", "T", "W", "T", "F", "S", "S")
TODAY = 3
DEPLOYS_WEEK = (6, 9, 4, 7, 0, 0, 0)
DEPLOYS_LAST_WEEK = (5, 7, 6, 4, 9, 1, 0)
ERRORS_HOURLY = (12, 9, 14, 8, 11, 7, 9, 22, 41, 37, 30, 26)
REQUESTS = (8.1, 7.4, 6.9, 7.8, 9.6, 11.2, 12.8, 13.5, 13.1, 12.4, 12.9, 13.6)  # thousands per minute

RUNBOOK = """\
## checkout: p95 over 800 ms

1. Check the canary: `deploy status checkout`
2. Compare p95 with the last release
3. Roll back if errors pass **1%**:

```sh
deploy rollback checkout --to v2.13.4
```

> Page *storefront* if the payment provider is slow too.

## search: shard not answering

1. Find the shard: `search shards --unhealthy`
2. Drain it: `search drain shard-3`
3. Ship the hotfix once staging is green

- shard-1 and shard-2 are **healthy**
- shard-3 has been *down* since 14:03
"""

ACTIVITY: tuple[tuple[str, str, str, str], ...] = (
    # time, kind (a Tones attribute), source, message
    ("12:31", "primary", "github", "lee opened #488 on media: smaller thumbnails"),
    ("12:40", "success", "deploy", "gateway v5.1.2 canary healthy, promoting"),
    ("12:44", "muted", "monitor", "media thumbnail queue at 4 minutes"),
    ("12:58", "primary", "claude", "Claude Code finished #486: flaky search test"),
    ("13:02", "success", "deploy", "gateway v5.1.2 is live in all regions"),
    ("13:15", "primary", "github", "dana merged #482 into ledger: batch writes"),
    ("13:21", "primary", "deploy", "ledger v0.9.0 canary started (1 of 5 regions)"),
    ("13:40", "warning", "monitor", "checkout p95 above 800 ms for 5 minutes"),
    ("13:41", "warning", "pager", "INC-230 opened, priya is on it"),
    ("13:47", "primary", "claude", "Claude Code opened #491: checkout retry budget"),
    ("13:52", "success", "github", "checks passed on #491 (14 of 14)"),
    ("13:58", "primary", "deploy", "ledger v0.9.0 canary at 3 of 5 regions"),
    ("14:03", "error", "monitor", "search shard 3 stopped answering"),
    ("14:04", "error", "pager", "INC-231 opened, sam is on it"),
    ("14:09", "muted", "deploy", "search v1.9.3 hotfix building on staging"),
    ("14:12", "waiting", "claude", "Claude Code is waiting: ship checkout v2.14.0?"),
)


class World:
    """What changes while the showcase runs: service states and the release."""

    def __init__(self) -> None:
        self.services: list[Service] = list(SERVICES)
        self.rollout = 0.62  # ledger's canary
        self.release: float | None = None  # checkout v2.14.0 once shipped: 0..1

    def service(self, key: str) -> Service:
        return next(s for s in self.services if s.key == key)

    def update(self, key: str, **changes: Any) -> Service:
        for i, s in enumerate(self.services):
            if s.key == key:
                self.services[i] = replace(s, **changes)
                return self.services[i]
        raise KeyError(key)

    @property
    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for s in self.services:
            out[s.state] = out.get(s.state, 0) + 1
        return out


def _mix(a: str, b: str, t: float) -> str:
    ca = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    cb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(ca, cb, strict=True))


class Tones:
    """The current theme's colours as ``#rrggbb``: semantic ones for text and
    pixels, and a few without a theme variable (``waiting``)."""

    def __init__(self, variables: dict[str, str]) -> None:
        def pick(name: str, fallback: str) -> str:
            value = variables.get(name) or fallback
            return value[:7].lower() if value.startswith("#") else fallback

        self.background = pick("background", "#121212")
        self.foreground = pick("foreground", "#e0e0e0")
        self.primary = pick("primary", "#0178d4")
        self.secondary = pick("secondary", "#004578")
        self.accent = pick("accent", "#fea62b")
        self.success = pick("success", "#4ebf71")
        self.warning = pick("warning", "#fea62b")
        self.error = pick("error", "#b93c5b")
        self.text_primary = pick("text-primary", self.primary)
        self.text_success = pick("text-success", self.success)
        self.text_warning = pick("text-warning", self.warning)
        self.text_error = pick("text-error", self.error)
        self.waiting = "#ffd75e"  # "waiting on you": no theme variable for it
        self.muted = _mix(self.foreground, self.background, 0.45)
        self.faint = _mix(self.foreground, self.background, 0.62)

    def of(self, name: str) -> str:
        return str(getattr(self, name))

    def text(self, name: str) -> str:
        """The readable text variant of a tone, where the theme has one."""
        return str(getattr(self, f"text_{name}", None) or self.of(name))

    def state(self, state: str) -> str:
        return self.of(STATES[state][2])

    def state_text(self, state: str) -> str:
        return self.text(STATES[state][2])

    def load(self, fraction: float) -> tuple[str, str]:
        """A gradient for a load from 0 to 1: calm, then warm, then hot."""
        if fraction >= 0.9:
            return self.warning, self.error
        if fraction >= 0.75:
            return self.success, self.warning
        return self.primary, self.success

    def severity(self, severity: int) -> str:
        return {1: self.error, 2: self.warning, 3: self.waiting}.get(severity, self.secondary)
