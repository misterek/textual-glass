# Changelog

## textual-glass 0.3.0 and textual-orbital 0.2.0 (2026-10-28)

Hardening for a dashboard that stays open all day, on a laptop that sleeps and wakes, plus the features a corner window needed. Each new invariant is a rule in [`AGENTS.md`](AGENTS.md), with tests that guard it: rules 21 to 24 for glass, 25 to 30 for orbital. Upgrade both together: orbital 0.2 needs glass 0.3.

### textual-orbital

**Fetching**
- Each fetch runs in a daemon thread of its own, with a deadline. `Source.timeout` defaults to `max(2 * interval, 10 s)` (`fetch_timeout`).
- Past the deadline, the source fails with "timed out after N s", and the late result is ignored. A source with `OrbitalApp.HUNG_LIMIT` (2) hung fetches pauses until one returns. Quitting never waits on a fetch. Rule 25.
- `fetch()` must set its own network and subprocess timeouts, and must be safe to run twice at once: after a timeout, the hung call may still be running.
- Any `BaseException` from a fetch is a failure, except `KeyboardInterrupt` on the main thread.
- The next poll is scheduled an interval after the fetch ends. Failures back off `interval * 2**n`, capped at `max(interval, 15 min)`, and `poll_all()` skips the wait. New: `SourceStatus.failures` and `next_poll`, which the health screen shows.
- `OrbitalApp.fetch(func, *args, then=, timeout=30.0)`: an on-demand fetch, with a thread, a deadline, and `then(data, error)` called on the app thread.

**Errors and bad data**
- `OrbitalApp.report_error(where, error)` logs the error, keeps it in `app.errors`, and shows a toast at most every 30 s. Show the list with `SourcesScreen(..., errors=app.errors)`.
- Worker callbacks, each publish step, each widget hook, the timers and the minute tick are all guarded, so one failure never skips the others. Rule 26.
- `Store.apply` fails *that* source on a naive datetime ("naive datetime in AttentionItem.since") or on a key another source already uses ("duplicate key 'X' (also from GH)"). Keys must be unique across sources. Rule 27.
- A resolved alert closes only when it is on top. In Textual 8.2.8, `dismiss()` on a covered screen pops whatever is on top. Rule 28.

**Snapshots**
- Every fetch re-stamps `app.snapshot.taken_at`. Snooze, wake and expiry re-publish with `taken_at = now()`.
- A new day re-publishes, so `week()` rolls over.
- `Snapshot.week(on=date)` returns any week.

**Alerts and snoozes**
- `OrbitalApp(alert_style="modal" | "toast", toast_timeout=8.0)`, and `Alert.style` to override it per alert.
- `AlertToast` sits bottom-right on glass's float layer. It takes no focus or keys and shows "+N more". A click jumps to its target, and a timeout counts as dismissed. Every alert view closes through one path. Rule 30.
- `OrbitalApp(snooze_file=PATH)` keeps item and alert snoozes across restarts. The file is written atomically, and a broken one is reported and treated as empty. Rule 29.
- `unsnooze(key)`. `app.snoozed` and `alert_queue.snoozed` are documented.
- `WaitingRule`'s docs say `since` must be stable.

**Widgets, notifications and links**
- `DetailScreen` values can be zero-argument callables, loaded in a thread with a deadline (`LOAD_TIMEOUT`) and showing "loading…" until they return.
- `PriorityList.RowsLeft` is posted with the items that left after `set_items`.
- Notifications pass through tmux (DCS passthrough; needs `allow-passthrough on`).
- `Notifier.sent` is bounded. The window title is also cleared on SIGTERM and SIGHUP.
- `open_link` opens the browser off the UI thread, and `OrbitalApp(open_hosts=...)` is an optional allow-list.
- `SourceStatus.items` counts events.

### textual-glass

**Image and timer lifetimes**
- A sprite's image is freed once no widget shows it. Rule 21.
- While glass is off, nothing runs for it: the 60 Hz timer stays paused, and late worker results are dropped. Rule 22.

**Errors and recovery**
- More than 5 errors in 60 s turn glass off. It retries after 1, 5 and 30 minutes, and the retry count resets after 10 good minutes. Every worker callback counts towards the limit.
- A sprite whose maker raises is retried later instead of staying blank.
- Textual's border table is patched only when its shape matches.

**Terminal state**
- Suspend and resume (ctrl+Z, `fg`) hand the terminal back clean and redraw everything. Rule 24.
- `on_hard_exit(key, data)` writes registered strings to the terminal on SIGTERM, SIGHUP or an unclean interpreter exit. Glass uses it to reset the background colour and delete its images.
- No sync markers are written inside Textual's frame. Rule 23.
- Late terminal replies to the detection query are drained.

**Widget API**
- Instrument colours can change with each update: `StatTile.set(colors=, color=)`, `ProgressArc.set(colors=)`, `SpriteView.set_sprite(fallback_color=)`.
- `writer_backlog` is removed.
