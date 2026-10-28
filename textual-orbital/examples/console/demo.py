"""The demo world behind Mission Console: five fake sources over one simulated
team, moved along by a script so that every kind of alert happens within the
first two minutes:

======  ==============================================================
 t (s)  what happens
======  ==============================================================
     0  ``fix-flaky-checkout-test`` is already waiting (permission prompt)
    25  ``rate-limit-middleware`` finishes a plan and waits for approval
    45  ``fix-flaky-checkout-test`` gets going again
    50  the search release fails its tests (release failed)
    75  checkout's error rate pages the on-call (incident)
   100  a ticket is assigned to you
   120  the storefront release goes to production, and lands at 150
   170  the security scanner fails for a while (a red health dot)
  180+  random churn every 25 to 45 seconds
======  ==============================================================

``speed`` compresses the script and the poll intervals (tests use 10 to 40).
The sources report events for this week and the two before; older weeks come
from ``DemoProvider.history``, a slow call the Week page makes on demand.
Everything is made up: the services, the people, the tickets and the numbers.
"""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta

from textual_orbital import (
    AttentionItem,
    DoneTask,
    Entity,
    EntityState,
    Event,
    Goal,
    Source,
    SourceData,
    Task,
    TrackHealth,
    TrackItem,
    now,
)
from textual_orbital.model import monday_of

M = timedelta(minutes=1)
H = timedelta(hours=1)
D = timedelta(days=1)
WEEK = timedelta(weeks=1)
#: How long ``DemoProvider.history`` takes, like a slow API (seconds).
HISTORY_DELAY = 0.5

#: The release pipeline, first stage to last.
STAGES = ("BUILD", "TEST", "STAGE", "CANARY", "PROD")
BUILD, TEST, STAGE, CANARY, PROD = range(5)

ORG = "https://github.com/example-org"
TICKETS = "https://tickets.example.com/browse"
MONITOR = "https://monitor.example.com"
SCANNER = "https://scanner.example.com/findings"


@dataclass(frozen=True)
class Release:
    """The demo's own record for a release, carried in ``TrackItem.data``."""

    service: str
    version: str
    summary: str
    owner: str
    run: int  # the pipeline run, for the link


def release_item(rel: Release, stage: int, health: TrackHealth, status: str, stage_since: datetime,
                 opened_at: datetime) -> TrackItem:
    return TrackItem(key=f"GH:{rel.service}@{rel.version}", source="GH", title=f"{rel.service}: {rel.summary}",
                     label=rel.version, stage=stage, health=health, stage_since=stage_since, opened_at=opened_at,
                     url=f"{ORG}/{rel.service}/actions/runs/{rel.run}", detail=status, data=rel)


class DemoWorld:
    """The simulated state every demo source reads from (thread-safe)."""

    def __init__(self, speed: float = 1.0, dynamics: bool = True, seed: int = 7) -> None:
        self.speed = max(0.01, speed)
        self.dynamics = dynamics
        self.rng = random.Random(seed)
        self.lock = threading.RLock()
        self.t0 = time.monotonic()
        t = now()
        self.sessions: dict[str, Entity] = {}
        for s in (
            Entity("fix-flaky-checkout-test", "fix-flaky-checkout-test", "storefront", EntityState.WAITING, t - 3 * M,
                   "permission prompt", "Wants to run: make test-e2e SUITE=checkout",
                   "claude://session/fix-flaky-checkout-test"),
            Entity("rate-limit-middleware", "rate-limit-middleware", "payments-api", EntityState.WORKING, t - 12 * M,
                   "", "Writing tests for the token bucket", "claude://session/rate-limit-middleware"),
            Entity("search-reindex-job", "search-reindex-job", "search", EntityState.WORKING, t - 4 * M, "",
                   "Profiling the batch indexer", "claude://session/search-reindex-job"),
            Entity("runbook-cleanup", "runbook-cleanup", "ops-docs", EntityState.IDLE, t - 70 * M, "",
                   "Finished: 9 runbooks updated", "claude://session/runbook-cleanup"),
            Entity("dependency-bumps", "dependency-bumps", "web-shell", EntityState.ERROR, t - 9 * M,
                   "API rate limited", "GitHub API returned 403 (secondary rate limit)",
                   "claude://session/dependency-bumps"),
        ):
            self.sessions[s.id] = s
        releases = [
            (Release("storefront", "4.12.0", "one-query cart totals", "omar", 8812), CANARY, TrackHealth.PENDING,
             "baking", 25 * M, D),
            (Release("payments-api", "2.31.1", "retry budget for webhooks", "dana", 8790), STAGE, TrackHealth.OK,
             "awaiting promotion", 20 * H, 2 * D),
            (Release("search", "1.8.0", "synonyms for product names", "mei", 8820), TEST, TrackHealth.PENDING,
             "tests running", 40 * M, 3 * H),
            (Release("auth-service", "3.2.0", "session keys rotation", "noor", 8701), TEST, TrackHealth.FAIL,
             "contract tests failing", 2 * D, 3 * D),
            (Release("mobile-gateway", "0.9.4", "gzip for list endpoints", "jules", 8824), BUILD,
             TrackHealth.PENDING, "building", 6 * M, 6 * M),
            (Release("notifications", "5.0.0", "templates v2", "sasha", 8650), STAGE, TrackHealth.IDLE,
             "held for the freeze", 5 * D, 6 * D),
            (Release("billing-worker", "1.4.2", "faster invoice batches", "dana", 8805), PROD, TrackHealth.OK,
             "rolled out", 3 * H, 2 * D),
            (Release("web-shell", "7.1.0", "new navigation", "omar", 8766), CANARY, TrackHealth.WARN,
             "error rate 0.8%", 3 * D, 4 * D),
        ]
        self.releases: dict[str, TrackItem] = {}
        for rel, stage, health, status, in_stage, age in releases:
            item = release_item(rel, stage, health, status, t - in_stage, t - age)
            self.releases[item.key] = item
        self.items: dict[str, AttentionItem] = {i.key: i for i in (
            AttentionItem("gh-pr-881", "GH", 7, "review", "Add a retry budget to the HTTP client", "payments-api",
                          "dana", t - 5 * H, url=f"{ORG}/payments-api/pull/881"),
            AttentionItem("gh-pr-412", "GH", 8, "review", "Cache product images at the edge", "storefront", "omar",
                          t - 2 * D - 3 * H, url=f"{ORG}/storefront/pull/412"),
            AttentionItem("TT-2207", "TT", 8, "respond", "Export times out after 30 seconds", "Support",
                          "a customer replied", t - 2 * H, url=f"{TICKETS}/TT-2207"),
            AttentionItem("TT-2190", "TT", 6, "due today", "Rotate the shared API keys", "Platform", "noor",
                          t - 26 * H, url=f"{TICKETS}/TT-2190"),
            AttentionItem("im-latency", "IM", 6, "warning", "p95 latency 640 ms on search", "prod-eu",
                          "threshold 500 ms", t - 35 * M, url=f"{MONITOR}/alerts/latency-search"),
            AttentionItem("ss-881", "SS", 7, "patch", "Heap overflow in image-decoder 2.3", "storefront",
                          "fix in 2.3.4", t - 20 * H, url=f"{SCANNER}/881"),
            # be aware
            AttentionItem("TT-2240", "TT", 5, "new ticket", "Dark mode loses the brand colours", "Web",
                          "filed by mei", t - H, "aware", f"{TICKETS}/TT-2240"),
            AttentionItem("gh-pr-77", "GH", 5, "team review", "Pin base images by digest", "build-images",
                          "platform team", t - 9 * H, "aware", f"{ORG}/build-images/pull/77"),
            AttentionItem("im-disk", "IM", 5, "disk 81%", "db-replica-3 data volume", "prod-eu", "grows 2% a day",
                          t - 4 * D, "aware", f"{MONITOR}/hosts/db-replica-3"),
            AttentionItem("TT-2185", "TT", 4, "closed", "Move cron jobs to the scheduler", "Platform", "done · jules",
                          t - 11 * H, "aware", f"{TICKETS}/TT-2185"),
            AttentionItem("im-cert", "IM", 4, "cert expires", "api.example.com certificate", "edge", "in 12 days",
                          t - 2 * D, "aware", f"{MONITOR}/certs/api"),
            AttentionItem("gh-pr-203", "GH", 3, "bot review", "Update the test runner to 9.4", "web-shell",
                          "deps-bot", t - 2 * H, "aware", f"{ORG}/web-shell/pull/203"),
            AttentionItem("ss-12", "SS", 3, "outdated", "14 dependencies behind", "payments-api", "2 major",
                          t - 6 * D, "aware", f"{SCANNER}/12"),
            AttentionItem("ss-4", "SS", 2, "secret", "Test token in a fixture", "web-shell", "revoked",
                          t - 30 * D, "aware", f"{SCANNER}/4"),
        )}
        #: The first day the sources report events for: the Monday two weeks before this one.
        self.first_day = monday_of(t.astimezone().date()) - 2 * WEEK
        self.events: list[Event] = _events(self.first_day, t.astimezone().date(), t)
        self.goals = [
            Goal("G-1", "Q4 reliability", "Dana", 0.72, f"{TICKETS}/G-1"),
            Goal("G-2", "Checkout v5", "Omar", 0.45, f"{TICKETS}/G-2"),
            Goal("G-3", "Search relevance", "Mei", 0.20, f"{TICKETS}/G-3"),
            Goal("G-4", "Cost down 15%", "Jules", 0.88, f"{TICKETS}/G-4"),
        ]
        self.tasks = [
            Task("TT-2101", "Move checkout to the new payments API", "Omar", "in progress", "Projects", t - 9 * D,
                 f"{TICKETS}/TT-2101", "G-2"),
            Task("TT-2102", "Search ranking experiment", "Mei", "in progress", "Projects", t - 12 * D,
                 f"{TICKETS}/TT-2102", "G-3"),
            Task("TT-2150", "Weekly patching", "Jules", "in progress", "Ongoing", t - 31 * D, f"{TICKETS}/TT-2150"),
            Task("TT-2151", "Dependency update triage", "Dana", "in progress", "Ongoing", t - 45 * D,
                 f"{TICKETS}/TT-2151"),
            Task("TT-2190", "Rotate the shared API keys", "Noor", "in review", "Small tasks", t - 2 * D,
                 f"{TICKETS}/TT-2190", "G-1"),
            Task("TT-2193", "Budget alerts per team", "Jules", "in progress", "Small tasks", t - 4 * D,
                 f"{TICKETS}/TT-2193", "G-4"),
            Task("TT-2196", "Remove unused service accounts", "Omar", "blocked", "Small tasks", t - 6 * D,
                 f"{TICKETS}/TT-2196", "G-1"),
        ]
        self.done = [
            DoneTask("TT-2088", "Retire the old status page", t - 2 * H, "done", f"{TICKETS}/TT-2088"),
            DoneTask("TT-2084", "Enable audit logs in staging", t - D, "done", f"{TICKETS}/TT-2084"),
            DoneTask("TT-2080", "Standardize log retention", t - 2 * D, "closed", f"{TICKETS}/TT-2080"),
            DoneTask("TT-2077", "Rightsize the staging database", t - 3 * D, "done", f"{TICKETS}/TT-2077"),
            DoneTask("TT-2071", "Document the DNS runbook", t - 5 * D, "done", f"{TICKETS}/TT-2071"),
        ]
        self.failing: dict[str, str] = {}
        self.script: list[tuple[float, Callable[[], object]]] = sorted([
            (25, lambda: self._session("rate-limit-middleware", EntityState.WAITING, "plan ready for review",
                                       "Plan: 5 files, 180 lines. Approve to continue?")),
            (45, lambda: self._session("fix-flaky-checkout-test", EntityState.WORKING, "", "Running the e2e suite")),
            (50, lambda: self._release("GH:search@1.8.0", TrackHealth.FAIL, "integration tests failed")),
            (75, self._incident),
            (100, self._assigned),
            (120, lambda: self._release("GH:storefront@4.12.0", TrackHealth.PENDING, "rolling out 20%", PROD)),
            (140, lambda: self._session("runbook-cleanup", EntityState.WORKING, "", "Updating the on-call guide")),
            (150, self._landed),
            (170, lambda: self.failing.__setitem__("SS", "scanner API: HTTP 503 (maintenance)")),
            (260, self._incident_resolved),
            (300, lambda: self.failing.pop("SS", None)),
        ], key=lambda e: e[0])
        self.next_random = 180.0

    # -- time ---------------------------------------------------------------------

    def elapsed(self) -> float:
        return (time.monotonic() - self.t0) * self.speed

    def advance(self) -> None:
        with self.lock:
            el = self.elapsed()
            while self.script and self.script[0][0] <= el:
                _, action = self.script.pop(0)
                if self.dynamics:
                    action()
            while self.dynamics and self.next_random <= el:
                self._random_event()
                self.next_random += self.rng.uniform(25, 45)

    # -- script actions ----------------------------------------------------------------

    def _session(self, sid: str, state: EntityState, reason: str, detail: str) -> None:
        self.sessions[sid] = replace(self.sessions[sid], state=state, since=now(), reason=reason, detail=detail)

    def _release(self, key: str, health: TrackHealth, status: str, stage: int | None = None) -> None:
        item = self.releases[key]
        self.releases[key] = replace(item, health=health, detail=status, stage_since=now(),
                                     stage=item.stage if stage is None else stage)

    def _incident(self) -> None:
        t = now()
        self.items["im-inc-417"] = AttentionItem(
            "im-inc-417", "IM", 10, "incident", "Checkout error rate at 6%", "prod-us", "paging the on-call: you",
            t, url=f"{MONITOR}/incidents/417")

    def _incident_resolved(self) -> None:
        item = self.items.pop("im-inc-417", None)
        if item is not None:
            self.events.append(Event(now(), "incident", "IM", item.title, item.where, item.url))

    def _assigned(self) -> None:
        self.items["TT-2231"] = AttentionItem(
            "TT-2231", "TT", 8, "assigned", "Saved cards fail at checkout", "Storefront", "assigned by dana",
            now(), url=f"{TICKETS}/TT-2231")

    def _landed(self) -> None:
        key = "GH:storefront@4.12.0"
        self._release(key, TrackHealth.OK, "rolled out", PROD)
        item = self.releases[key]
        self.events.append(Event(now(), "shipped", "GH", item.title, item.label, item.url))

    def _random_event(self) -> None:
        rng = self.rng
        roll = rng.random()
        if roll < 0.35:
            s = rng.choice(list(self.sessions.values()))
            if s.state is EntityState.WAITING:
                self._session(s.id, EntityState.WORKING, "", "Continuing")
            elif s.state is EntityState.WORKING:
                reason, detail = rng.choice([
                    ("permission prompt", "Wants to run: kubectl rollout restart deploy/api"),
                    ("question", "Keep the old endpoint for a week?"),
                    ("plan ready for review", "Plan: 3 files, 96 lines. Approve to continue?"),
                ])
                self._session(s.id, EntityState.WAITING, reason, detail)
            else:
                self._session(s.id, EntityState.WORKING, "", "Picked up a new task")
        elif roll < 0.65:
            moving = [r for r in self.releases.values() if r.health is not TrackHealth.IDLE]
            item = rng.choice(moving)
            if item.health is TrackHealth.PENDING:
                if rng.random() < 0.7:
                    self._release(item.key, TrackHealth.OK, "rolled out" if item.stage == PROD else "passed")
                else:
                    self._release(item.key, TrackHealth.FAIL, f"{STAGES[item.stage].lower()} failed")
            elif item.health in (TrackHealth.FAIL, TrackHealth.WARN):
                self._release(item.key, TrackHealth.PENDING, "retrying")
            elif item.stage < PROD:
                self._release(item.key, TrackHealth.PENDING, "in progress", item.stage + 1)
            else:  # shipped a while ago: off the track, and a new release starts building
                del self.releases[item.key]
                rel = item.data
                if isinstance(rel, Release):
                    major, minor, _ = rel.version.split(".")
                    nxt = replace(rel, version=f"{major}.{int(minor) + 1}.0", run=rel.run + 40)
                    t = now()
                    new = release_item(nxt, BUILD, TrackHealth.PENDING, "building", t, t)
                    self.releases[new.key] = new
        elif roll < 0.8:
            open_tickets = [i for i in self.items.values() if i.source == "TT" and i.section == "attention"]
            if open_tickets and rng.random() < 0.5:
                done = self.items.pop(rng.choice(open_tickets).key)
                self.events.append(Event(now(), "ticket closed", "TT", done.title, done.where, done.url))
            else:
                title, where = rng.choice(_TICKET_POOL)
                number = 2300 + rng.randint(0, 600)
                key = f"TT-{number}"
                self.items[key] = AttentionItem(key, "TT", 5, "new ticket", title, where, "filed by the team", now(),
                                                "aware", f"{TICKETS}/{key}")
                self.events.append(Event(now(), "ticket opened", "TT", title, where))
        else:
            kind = rng.choice(["merged", "reviewed", "ticket closed"])
            title, where = rng.choice(_TICKET_POOL if kind == "ticket closed" else _PR_POOL)
            self.events.append(Event(now(), kind, "TT" if kind == "ticket closed" else "GH", title, where))

    # -- history, on demand --------------------------------------------------------------

    def history(self, first: date, last: date) -> list[Event]:
        """Every event from ``first`` to ``last`` (local dates), as a slow API would
        return them: the days the sources report, then older ones, made up the
        same way each time."""
        time.sleep(HISTORY_DELAY)
        with self.lock:
            held = [e for e in self.events if first <= e.when.astimezone().date() <= last]
        return _events(first, min(last, self.first_day - timedelta(days=1)), now()) + held

    def release_log(self, key: str) -> str:
        """A release's pipeline run, stage by stage, as a slow API would return it."""
        time.sleep(HISTORY_DELAY)
        with self.lock:
            item = self.releases.get(key)
        if item is None:
            raise LookupError(f"no release {key}")
        last = {TrackHealth.FAIL: "✕ failed", TrackHealth.WARN: "! needs work", TrackHealth.PENDING: "… running",
                TrackHealth.IDLE: "‖ held"}.get(item.health, "✓ passed")
        steps = [f"{name.lower()} {'✓ passed' if i < item.stage else last}" for i, name in
                 enumerate(STAGES[:item.stage + 1])]
        rel = item.data if isinstance(item.data, Release) else None
        head = f"run {rel.run} by {rel.owner}" if rel else "run"
        return f"{head}: {item.detail}\n" + "  ·  ".join(steps)

    # -- what each source sees -----------------------------------------------------------

    def attention_for(self, source: str) -> list[AttentionItem]:
        out: list[AttentionItem] = []
        if source == "CC":
            for s in self.sessions.values():
                if s.state is EntityState.WAITING:
                    out.append(AttentionItem(f"cc:{s.id}", "CC", 10, "claude waiting", s.name, s.where, s.reason,
                                             s.since, url=s.url, ref=("entity", s.id)))
                elif s.state is EntityState.ERROR:
                    out.append(AttentionItem(f"cc:{s.id}", "CC", 6, "claude error", s.name, s.where, s.reason,
                                             s.since, url=s.url, ref=("entity", s.id)))
        if source == "GH":
            for r in self.releases.values():
                why = {TrackHealth.FAIL: ("fix release", 8), TrackHealth.WARN: ("canary", 7)}.get(r.health)
                if why is None and r.stage == STAGE and r.health is TrackHealth.OK:
                    why = ("promote", 7)
                if why is not None:
                    out.append(AttentionItem(f"{r.key}:{why[0]}", "GH", why[1], why[0], r.title, r.label, r.detail,
                                             r.stage_since, url=r.url, ref=("track", r.key)))
        out.extend(i for i in self.items.values() if i.source == source)
        return out


_PR_POOL = [
    ("Add a retry budget to the HTTP client", "payments-api"), ("Cache product images at the edge", "storefront"),
    ("Fix the flaky login test", "web-shell"), ("Batch the indexer writes", "search"),
    ("Pin base images by digest", "build-images"), ("Split the settings page", "web-shell"),
    ("Paginate the orders endpoint", "mobile-gateway"), ("Faster invoice batches", "billing-worker"),
]
_TICKET_POOL = [
    ("Export times out after 30 seconds", "Support"), ("Dark mode loses the brand colours", "Web"),
    ("Rotate the shared API keys", "Platform"), ("Invoice PDF has the wrong locale", "Billing"),
    ("Search ignores accents", "Search"), ("Push notifications arrive twice", "Mobile"),
    ("Saved cards fail at checkout", "Storefront"), ("Move cron jobs to the scheduler", "Platform"),
]
_RATES = {"shipped": 2.5, "merged": 5.0, "reviewed": 3.5, "ticket closed": 4.0, "ticket opened": 3.0,
          "incident": 0.4}
_SOURCES = {"shipped": "GH", "merged": "GH", "reviewed": "GH", "ticket closed": "TT", "ticket opened": "TT",
            "incident": "IM"}


def _events(first: date, last: date, until: datetime) -> list[Event]:
    """Made-up events for the weekdays from ``first`` to ``last``, up to ``until``.
    Each day has its own seed, so a day is the same every time it is asked for."""
    local = until.astimezone()
    out: list[Event] = []
    day = first
    while day <= last:
        rng = random.Random(day.toordinal())
        if day.weekday() < 5:
            for kind, rate in _RATES.items():
                for _ in range(max(0, round(rng.gauss(rate, rate * 0.4)))):
                    when = datetime.combine(day, datetime.min.time(), tzinfo=local.tzinfo) + timedelta(
                        hours=rng.uniform(8, 18.5))
                    pool = _TICKET_POOL if _SOURCES[kind] == "TT" else _PR_POOL
                    title, where = rng.choice(pool)
                    if kind == "incident":
                        title, where = rng.choice([("Checkout error rate high", "prod-us"),
                                                   ("Search latency over budget", "prod-eu")])
                    if when <= local:
                        out.append(Event(when, kind, _SOURCES[kind], title, where))
        day += timedelta(days=1)
    return out


class DemoSource:
    """One demo source reading the shared world."""

    def __init__(self, world: DemoWorld, key: str, name: str, base_interval: float,
                 fetch: Callable[[DemoWorld], SourceData]) -> None:
        self.world = world
        self.key = key
        self.name = name
        self.interval = max(0.25, base_interval / world.speed)
        self._fetch = fetch

    def fetch(self) -> SourceData:
        world = self.world
        world.advance()
        with world.lock:
            if self.key in world.failing:
                raise RuntimeError(world.failing[self.key])
            return self._fetch(world)


def _cc(w: DemoWorld) -> SourceData:
    return SourceData(entities=list(w.sessions.values()), attention=w.attention_for("CC"))


def _gh(w: DemoWorld) -> SourceData:
    return SourceData(track=list(w.releases.values()), attention=w.attention_for("GH"),
                      events=[e for e in w.events if e.source == "GH"])


def _tt(w: DemoWorld) -> SourceData:
    return SourceData(attention=w.attention_for("TT"), goals=list(w.goals), tasks=list(w.tasks), done=list(w.done),
                      events=[e for e in w.events if e.source == "TT"])


def _attention_and_events(key: str) -> Callable[[DemoWorld], SourceData]:
    def fetch(w: DemoWorld) -> SourceData:
        return SourceData(attention=w.attention_for(key), events=[e for e in w.events if e.source == key])
    return fetch


class DemoProvider:
    """Five demo sources over one simulated world.

    Args:
        speed: Run the script and the polls this many times faster (1 = real time).
        dynamics: False freezes the world (nothing changes after the first fetch).
        seed: For the random churn after the script.
    """

    def __init__(self, speed: float = 1.0, dynamics: bool = True, seed: int = 7) -> None:
        self.world = DemoWorld(speed=speed, dynamics=dynamics, seed=seed)
        w = self.world
        self._sources: list[Source] = [
            DemoSource(w, "CC", "Claude Code", 10, _cc),
            DemoSource(w, "GH", "GitHub", 60, _gh),
            DemoSource(w, "TT", "TicketTracker", 60, _tt),
            DemoSource(w, "IM", "InfraMonitor", 60, _attention_and_events("IM")),
            DemoSource(w, "SS", "SecurityScanner", 60, _attention_and_events("SS")),
        ]

    def sources(self) -> Sequence[Source]:
        return self._sources

    def history(self, first: date, last: date) -> list[Event]:
        """Events from ``first`` to ``last``, for weeks older than the sources
        report: the console's Week page fetches them with ``OrbitalApp.fetch``."""
        return self.world.history(first, last)

    def release_log(self, key: str) -> str:
        """A release's pipeline run: the console's release detail loads it lazily."""
        return self.world.release_log(key)
