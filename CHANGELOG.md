# Changelog

## textual-glass 0.5.0 and textual-orbital 0.3.0 (2026-10-29)

Scenes, and the week in review built on them: a show that plays the week's work as ships arriving at a planet, then holds its last frame as the week's summary. Nothing changes for an app that doesn't use them. Upgrade both together: orbital 0.3 needs glass 0.5.

### textual-glass

**Scenes** (`textual_glass.scenes`, `player.py`)
- `app.glass.play_scene(make, at=None, start=0.0, on_cue=None, on_end=None)` plays a scene over a widget, a region or the whole window, and returns a `Showing` (`stop()`, `elapsed`, `holding`, `ended`), or None while glass is off or the anchor isn't shown. Glass never plays one by itself, and the player is only made on the first call.
- A scene is `Actor`s on `Route`s, not a movie. Each actor is a small image uploaded once: its frames loop in the terminal by itself, or glass steps through them once and holds the last. Glass moves it by re-placing it at pixel offsets inside the cells, cropped at the stage's edge. `cells=(cols, rows)` stretches a big, soft picture over whole cells instead.
- Actors whose `frames` is the same object share one image, with a placement each, so a week of 150 lights of five kinds is five images.
- `make(stage) -> Scene` runs in the worker, and `scenes.check` holds it to the limits (`MAX_ACTORS` 1024, `MAX_FRAMES` 64, `MAX_PIXELS` 16M, `MAX_SECONDS` 300). The `Stage` carries the anchor's size in terminal pixels, the theme's colours by role, its space colours, and `still` for `minimal` motion.
- After its duration a scene **holds**: what is on stage stays, only terminal-played loops move, and glass sends nothing. `Cue`s reach `on_cue(data)` on the app thread, once and in order.
- `on_end(reason)` says why a scene went: `stopped`, `replaced`, `moved`, `hidden` (a page switch counts), `resized`, `theme`, `off` or `failed`. An app puts the summary back after a resize with `start=math.inf`. At `minimal` motion a scene plays as a still: its end, with the loops on frame 1.
- A new layer, `Z_SCENE` (Z0 + 15M): over the ambient effects, under flourishes and the next popup's scrim. `stats()` counts `scenes`, `actors` and `scene_images`.
- `kitty.place_px` (an image at its own pixels, with X/Y offsets and a source rect), `kitty.play`, `kitty.animation(start=False)` and `kitty.unplace(image_id, placement_id)`. The kitty model checks offsets against the cell size and applies source rects. `Output.model(cell)` tracks the cursor.

**The week in review, as a scene** (`textual_glass.scenes.space`)
- `space.make(stage, week, look="halo", length=20.0)`: one ship per `Work(kind, hour)` (`merged`, `review`, `deploy`, `ticket`, `release`), arriving in the order the work happened, each leaving a light. A cue `("landed", i, kind)` marks each landing and `space.END` the end. It runs about `length` seconds, from a dozen items to 150 (busy weeks come in waves).
- Three looks (`space.LOOKS`): `halo` (an eclipse, one orbit per kind, the hour as the angle), `nightfall` (towns light up on a planet's night side, the day as the longitude) and `harbour` (a ringed giant, ships docking on a rail of seven days). Each light keeps its kind's shape in every look.
- Space is always night: on a light theme the show paints its own night sky behind itself, with the theme's hues brightened to glow.
- `examples/scenes/` is a small show in plain placeholder art; `examples/week/` is a made-up week in all three looks (`1` `2` `3`, `+` `-` for a busier or quieter week). `tests/scene_sheet.py` draws contact sheets.

### textual-orbital

- `WeekReplay(look="halo", ships=None, length=20.0)`: the week in review as a dashboard widget. Feed it `set_week(snapshot.week())`. It plays when its page shows, `replay()` plays it again, `set_look()`/`next_look()` change the look, a caption follows each landing ("Tue 14:02 · merged · Fix the flaky test (GH)"), and the tally follows at the end ("this week: 6 merged · 5 reviews · 3 deploys · 4 tickets · 1 release · 2 other"). Without glass it shows the same counts as a table by day.
- `ships` maps an app's `Event.kind` words to the five ship classes (`SHIP_CLASSES`); `SHIPS` knows the common words. Other kinds count as "other". `week_hours(when, monday)` converts by the local wall clock, DST weeks included.
- `REPLAY_LEGEND`: the legend row for an app with the page (`HelpScreen(keys, [*LEGEND, REPLAY_LEGEND])`). The default `LEGEND` is unchanged.
- The console's page 5, Replay: `p` plays it again, `l` changes the look.
- `AGENTS.md` has a new recipe: "Add a week-in-review page".

## textual-glass 0.4.0 (2026-10-28)

Flourishes: short celestial animations your app plays when something happens. Nothing changes for an app that doesn't call them. textual-orbital stays at 0.2.0 and works with either glass.

### textual-glass

**Flourishes**
- `app.glass.flourish(name, at=None, to=None, color=None) -> bool` plays a short celestial animation once (a comet, a sunburst, an eclipse...) over a widget, a region or the whole window. A comet flies from `at` to `to`. `textual_glass.FLOURISHES` lists the twelve kinds, each with what it could mean on a dashboard.
- Glass never plays one by itself: an app that doesn't call it sees no change.
- It plays at `subtle` and `full` motion, on the anchor's screen, at a new z (`Z_FLOURISH`: over a level's glass, under its float layer and the next popup's scrim). It returns False and does nothing while glass is off, at `minimal` motion, for an anchor that isn't shown, and while four (`MAX_FLOURISHES`) play.
- The frames are drawn in the worker, uploaded while stopped, stepped by glass and deleted after the last one. A resize, a theme change, `minimal` motion and the anchor's popup closing stop every flourish.
- `stats()` counts `flourishes`.
- `examples/flourishes/`: every kind over a small made-up dashboard, to try them in Ghostty. `tests/flourish_sheet.py` draws contact sheets and GIFs of each kind.

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
