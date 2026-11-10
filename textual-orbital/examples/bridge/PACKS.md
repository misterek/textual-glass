# Crew packs

A crew pack is the art of the bridge: the crew members and their poses. The console and chair at each station,
the little helper "ensigns", the captain's chair and the viewscreen are drawn by the renderer; a pack can
recolour them. There are two ways to make a crew of your own:

- **Change the crew's look** with a short `pack.json` in the **smooth** style: names, uniform colours, skin and
  hair, a hair style, extras, a build. The built-in crew is a smooth pack (`bridge/crew/smooth/pack.json`);
  yours extends it, and the renderer draws your crew the same way, at the exact pixel size of each station,
  lit by the console lamp.
- **Use your own pictures** in the **picture** style: a folder of transparent PNGs, one per pose, one folder per
  member. Paint them, cut them out of photos, or make them with an image model. The renderer puts the station
  round them, scales them to each station's size and lights them with the lamp, so your characters sit in the
  room like the built-in ones. A pack needs only two pictures to work.

```sh
uv run python app.py                                       # the built-in crew
uv run python app.py --pack ~/my-crew                      # a pack of either style
uv run python tools/crew_sheet.py --pack ~/my-crew --out /tmp/crew   # contact sheets and GIFs of it
```

In code: `bridge.crew.load("~/my-crew")`, or `load()` for the built-in crew. A broken pack fails at load time
with a `PackError` that names the file and what is wrong, for example
`~/my-crew/kit/seated.png: 1024x1024 pixels; a picture has the canvas's proportions, 464x400 (116:100)`.

Keep private packs outside this folder (or anywhere that is never pushed) and load them by path: the built-in
crew is generic on purpose.

## Change the crew's look

A `pack.json` that **extends** the built-in crew and lists your members. Everything else (the poses, the
furniture, the ensigns, the viewscreen) comes from the built-in pack:

```json
{
  "name": "my-crew",
  "style": "smooth",
  "extends": "smooth",
  "members": [
    {"id": "kit", "name": "Kit", "uniform": "#ff9f43",
     "colors": {"skin": "#d9a27c", "hair": "#2b1d14"},
     "hair_style": "curly", "extras": ["glasses", "headset"], "build": {"wide": 1}},
    {"id": "rho", "name": "Rho", "uniform": "#54a0ff",
     "colors": {"skin": "#f6d5bd", "hair": "#e8e0d0", "beard": "#c8c0b0"},
     "hair_style": "crop", "extras": ["beard"], "build": {"tall": 1}}
  ]
}
```

That is a complete pack: save it as `~/my-crew/pack.json` and run `app.py --pack ~/my-crew`. The app seats
members in this order, one per station, and uses `uniform` for the station's label too. Leave out `members`
to keep the built-in crew and change only `colors` (the console, the chair, the viewscreen's space). The
options are in [The smooth style](#the-smooth-style).

## Use your own pictures

1. **Export a starter pack.** The built-in crew drawn as pictures, every pose and frame, on the picture canvas:

   ```sh
   uv run python tools/export_pictures.py --out ~/my-pictures                       # all six members
   uv run python tools/export_pictures.py --out ~/my-pictures --members ash --stills  # one still per pose
   ```

   This writes `pack.json`, `guide.png` (the canvas with the console line marked) and `<member>/<pose>_<n>.png`
   for every frame (`<pose>.png` with `--stills`). It runs as it is: `app.py --pack ~/my-pictures`.

2. **Replace the pictures.** Paint over them, or drop in your own: a transparent PNG of the character alone,
   464 x 400 pixels (or any size with those proportions), drawn so that it sits where the starter's character
   sits (`guide.png` shows the console behind it). Start with `seated.png` and `hand_up.png`: those two are
   required, and every other pose falls back to one of them until you draw it (the chain is below). Delete the
   pictures you don't want to draw. A pose with one picture is a still; `typing_1.png`, `typing_2.png`... is an
   animation.

3. **Run and check.**

   ```sh
   uv run python app.py --pack ~/my-pictures
   uv run python tools/crew_sheet.py --pack ~/my-pictures --against smooth --out /tmp/crew --only sheets
   ```

   The sheet shows each of your members in every pose at the real station sizes, with the built-in member of
   the same id under it for comparison.

The smallest pack is a `pack.json` and two pictures:

```text
my-pictures/
  pack.json
  kit/
    seated.png
    hand_up.png
```

```json
{
  "name": "my-pictures",
  "style": "picture",
  "members": [{"id": "kit", "name": "Kit", "uniform": "#ff9f43"}]
}
```

The rest of this page is the reference: what every pose should show, the manifest keys of both styles, the
picture canvas, frames and timing, the fallbacks, the lamp's light, and the errors.

## The poses

Every pack shows every pose (`bridge.crew.POSES`). The renderer plays each at this pace; the built-in crew
shows them like this:

| Pose | When | What the built-in crew shows | Frames |
| --- | --- | --- | --- |
| `empty` | no session at this station | the chair and the console, the lamp bar dark (the renderer's, never a pack's) | 1 |
| `seated` | started, nothing asked yet | upright, hands on the console, a small smile; breathing, a blink | 6 at 500 ms |
| `typing` | working | eyes down on the console, hands taking turns, the console's lights flickering | 4 at 150 ms |
| `done` | finished its turn: your move | leaning back, arms folded, eyes closed in a happy curve, a sparkle | 2 at 900 ms |
| `hand_up` | waiting on you (a permission, a question) | one arm straight up, fingers spread, mouth an "o", a glowing "!" that blinks | 2 at 420 ms |
| `waving` | still waiting, minutes on | the raised hand swings side to side, a big grin | 2 at 260 ms |
| `standing` | waiting a long time | up out of the chair, both arms waving overhead, shouting, "!" marks | 2 at 240 ms |
| `jolt` | the turn died on an error | thrown back, eyes wide, arms flung out; the console sparks and smokes | 4 at 170 ms |
| `tea` | compacting its context | a striped mug held up in both hands, eyes closed, steam rising | 3 at 380 ms |
| `dozing` | idle a long time | head down on folded arms on the console, eyes shut, z's rising, the lamp dim | 3 at 650 ms |

The lamp is the state's colour (working, waiting, error, done, dim) and the renderer draws it in every pose:
`hand_up`, `waving` and `standing` must still read on their own at the smallest station (63 x 57 px): a raised
hand well above the head.

## pack.json: both styles

| Key | | What |
| --- | --- | --- |
| `name` | required | The pack's name (the app uses it in sprite keys). |
| `style` | required | `"smooth"` or `"picture"`. |
| `members` | required for a picture pack, and for a smooth pack without `extends` | The crew, in roster order. |
| `colors` | | Named colours of the scene (below). |
| `ensigns` | | A list, one per slot (used in turn): `{"colors": {"skin": ..., "hair": ...}, "hair_style": ...}`. They wear their member's uniform. |
| `about` | | A note for people; ignored. |

Unknown keys are errors, so typos are caught. A member, in both styles:

| Key | | What |
| --- | --- | --- |
| `id` | required | Unique; the app assigns sessions to members by id, in roster order. |
| `name` | | Shown under the station (defaults to the id). |
| `uniform` | required | `#rrggbb`: the member's identity colour: the uniform in the smooth style, the ensigns' uniform in both, and the label's colour. |

### Colours

A colour is an expression (no spaces):

| Form | Meaning | Example |
| --- | --- | --- |
| `#rrggbb`, `#rrggbbaa` | a colour | `#ffb238` |
| `name` | a named colour (below) | `skin` |
| `x*f` | scaled (darker below 1) | `skin*0.84` |
| `x+f` | toward white by f (0 to 1) | `uniform+0.55` |
| `x-f` | toward black by f | `console-0.3` |
| `x~c:f` | toward colour `c` (hex or name) by f | `lamp~console:0.45` |
| `x@a` | opacity a (0 to 1) | `#ffffff@0.7` |

Names may refer to other names (`"beard": "hair*0.8"`); a loop is an error. The scene's named colours, with
their defaults (a pack's `colors` overrides them):

| Name | Default | Used for |
| --- | --- | --- |
| `console`, `chair` | `#3c4766`, `#4b4569` | the station's console and the member's chair |
| `captain` | `#3d3a63` | the captain's chair |
| `waiting`, `waiting_dim`, `indicator_off` | `#ffb238`, `#c27a1e`, `#2a2738` | its three waiting lights (lit ones pulse between the first two) |
| `light1` to `light4` | cyan, green, pink, yellow | the console's small lights |
| `mug`, `mug_band`, `tea` | `#f1ece2`, `#e86a6a`, `#8a5530` | the tea pose |
| `space_top`, `space_bottom`, `nebula`, `star` | | the viewscreen's space |
| `planet`, `planet_dark`, `ring` | | its planet |
| `alert`, `bezel` | `#ff3048`, `#353d5a` | red alert, and the screen's frame |

## The smooth style

The built-in crew, and any pack that extends it: members described by parameters, drawn by code.

### pack.json

| Key | | What |
| --- | --- | --- |
| `extends` | | `"smooth"` (the built-in crew) or a path to another smooth pack (relative to this one): take its colours, crew and ensigns unless given here. Its crew is drawn in this pack's colours. |
| `members` | required unless `extends` | The crew, in roster order. Replaces the base pack's crew. |

A member, besides `id`, `name` and `uniform`:

| Key | | What |
| --- | --- | --- |
| `colors` | | `skin`, `hair`, `eyes`; also `beard` (default `hair*0.8`), `trim` (collar and cuffs, `uniform+0.55`), `mouth`, `cheek`, `freckle` (from `skin`), `frames` (glasses, `#262335`), `gear` (a headset, `#b8c2d8`). |
| `hair_style` | | `bob` (chin length, a fringe), `curly` (a big cloud of curls), `spiky`, `bun` (on top), `crop` (short), `ponytail`. Default `crop`. |
| `extras` | | Any of `glasses`, `headset` (with a microphone whose tip glows in the lamp's colour), `beard`, `freckles`. |
| `build` | | `{"wide": -1 to 2, "tall": -1 to 1}`: broader or slimmer shoulders, taller or shorter while seated. |

Unknown keys, hair styles and extras are errors at load time, named with the file.

### How it draws

- **At the station's own size.** Each sprite is drawn in a design box (116 x 100 units for a station and for
  the captain's chair) fitted into the sprite's pixels, bottom centred, with a transparent pixel on every
  side, at any scale (not only whole numbers). Glass asks for the station's true size, capped at 192 px a
  side: from 63 x 57 (7 x 3 cells at 9 x 19 px) through 126 x 114 (14 x 6, the usual 90 x 28 window) to
  180 x 152 (20 x 8 cells, or 10 x 4 on Retina's 18 x 38 px cells).
- **The member is the subject.** A seated head is 46 units across (nearly half the box's height), in front of
  a low, slim console (82 x 20 units, its top edge at 79 of 100) with the chair's back mostly hidden behind the
  member. Everything else is sized to fit round it: raised hands and the "!" in the top corners, the z's
  beside the bowed head, the ensigns on the floor either side of the console. Standing up, the member is
  drawn at 0.84 so big hair and both raised hands still fit.
- **Shapes, not pixels.** Every part (a head, a lock of hair, a sleeve, a hand, the console) is a signed
  distance field. Edges are anti-aliased from it on a canvas three times the output size each way (twice
  above 110 px), box-filtered down. No outlines: parts are separated by soft shadows and by light.
- **Shading and light.** Each part rounds over from its edge like a cushion and is lit by a soft key light
  from the upper left, a cool fill from above, a cool rim from behind (so dark hair reads on a dark nebula),
  and the console lamp: the screen set into the console's top glows in the lamp's colour and lights the
  face, chest and hands from below. State shows as light: amber faces wait, a red one has just had an error,
  a dim one has dozed off. The lamp itself is a bar of exactly the lamp's colour (to 6 bits) with its glow.
- **Frames.** The counts and gaps in the poses table. Frames keep 6 bits of colour where opaque and fewer in
  faint light, as glass's moving pictures do: about 40 % smaller once zlib'd (glass compresses every upload),
  with no banding to see.
- **Cost.** A station takes about 60 ms to draw at 63 x 57, 100 ms at 126 x 114 and 170 ms at 180 x 152 (all
  six frames of `seated`, the biggest; fewer frames cost less), in glass's worker thread, once per state
  change. Compressed, a station sprite is 7 to 22 KiB at 63 x 57, 22 to 68 KiB at 126 x 114 and 36 to 113 KiB
  at 180 x 152.

Ensigns are little figures in the member's uniform standing on the floor beside the console (the third in
front of its corner), bobbing out of step. The captain's chair (the user's, empty) is seen from behind: a
padded back with lit piping and quilting, a headrest, armrests with small glowing button panels, a pedestal on
a ring of light; three waiting lights across the top glow amber for each crew member waiting, pulsing. The
viewscreen is a pane of glass onto a nebula, star dust, twinkling stars, a moon and a ringed planet; red alert
tints it red with a pulsing glow round its edge (4 frames at 900 ms calm, 650 ms in alert). It is drawn at its
true size, up to about 1600 x 270 px: a bigger screen gets more stars and finer detail. Every size has the same
frames and gap, so tiles cut from one picture stay in step.

## The picture style

Your own PNGs. The renderer draws everything round them exactly as it does for the smooth crew: the chair
behind, the console in front with its lamp, the ensigns, the effects, the captain's chair, the viewscreen.

### Folder layout

```text
my-pictures/
  pack.json                 the manifest (required)
  guide.png                 anything at the top level that isn't pack.json is ignored
  kit/                      one folder per member, named after the member's id
    seated.png              a still: one picture
    hand_up_1.png           an animation: numbered frames, 1 to at most 8, no gaps
    hand_up_2.png
    tea.png
    notes.txt               files that aren't PNGs are left alone (notes, sources)
  rho/
    ...
```

Picture names are `<pose>.png` or `<pose>_<n>.png` for the poses in the table (not `empty`: the empty station
is the renderer's). Both `seated.png` and `seated_1.png` for one pose is an error, as is any other `.png` name.

### The canvas

A picture is the character alone on a transparent background, drawn on the **picture canvas: 464 x 400
pixels**, the station's 116 x 100 design box at 4 pixels a unit. Any size with the same proportions (116:100,
within 2 %) is accepted and scaled to it, so draw as big as you like; smaller than 116 x 100 is refused.
`guide.png` from the export tool shows the canvas with the station drawn round it. What matters on it:

| On the canvas (464 x 400) | |
| --- | --- |
| `y = 316` | The console's top edge. Hands rest on the plate just below it. |
| `y = 334` | Below this line the console hides your picture: the character sits behind it. What you paint between 316 and 334 shows on the console's top (hands, folded arms, a sleeping head). |
| `(232, 164)`, radius 92 | A seated head in the built-in crew: nearly half the picture's height across. Big heads read at 63 x 57. |
| `y = 44` | Where a raised hand reaches. The "!" is drawn by the renderer at the top right (x about 420, y 100). |
| the sides | Ensigns stand on the floor left and right of the console, in front of it, so a wide picture can go behind them. |

The character should fill the canvas: a station is 63 to 180 px wide on screen, so the whole picture is scaled
down by 2.5 to 7 times (premultiplied alpha, area-filtered), and small details go. The outer ring of every
frame is forced transparent on screen, whatever the picture has at its edge.

### Frames and timing

A pose with one picture is a still. `typing_1.png`, `typing_2.png`... (up to 8) is an animation, played at the
pose's gap from the table, or at `"gaps": {"typing": 120}` in `pack.json` (16 to 60000 ms).

The station keeps moving round a still: the console's lights and lamp flicker as the pose does, the "!"
blinks, the z's rise, the steam drifts, the sparks fly, the ensigns bob, and the still itself follows the
pose's head side to side (a `jolt` shakes it, `waving` and `standing` sway it). A pack's own frames bring their
own motion and are left alone. A pose where nothing moves (a still `seated`, no ensigns) is one frame, and
costs one upload.

### Fallbacks

A pack needs `seated.png` and `hand_up.png`. Every other pose it doesn't have shows the nearest one it has:

| Pose | Falls back to |
| --- | --- |
| `typing`, `done`, `jolt`, `tea` | `seated` |
| `waving` | `hand_up` |
| `standing` | `waving`, then `hand_up` |
| `dozing` | `done`, then `seated` |

So a pack with the two required pictures shows the seated picture for working, done, an error, compacting and
dozing (the lamp's colour and the effects tell them apart: green, blue, red with sparks, steam, z's) and the
raised hand for every kind of waiting. Add pictures for the poses you care about.

### The lamp's light

`"lamp_light": true` (the default) lights the picture with the console lamp the way the smooth crew is lit:
the lamp's colour falls on it from below in front, strongest on the chest and hands near the console's screen
and fading up the face, so the session's state shows as light on your character too (amber while waiting, red
after an error, dim while dozing). `false` keeps the pictures exactly as painted; a number (0 to 2) sets the
strength (1 is the default).

`"effects": true` (the default) draws the smooth style's effects over the picture: the glowing "!", the z's,
the steam from the mug's place, the sparkle, the sparks and smoke off the console. `false` keeps only the
console's own sparks and smoke. The motion lines beside a waving hand are never drawn over a picture: the
renderer doesn't know where your hand is.

### pack.json

| Key | | What |
| --- | --- | --- |
| `members` | required | The crew, in roster order; each `id` names a folder. A member takes `id`, `name`, `uniform` and `about` only. |
| `gaps` | | `{"<pose>": ms}`: the time between frames of a pose (default: the table's). |
| `lamp_light` | | `true` (default), `false`, or a strength from 0 to 2. |
| `effects` | | `true` (default) or `false`. |

With `colors`, `ensigns` and `about` as in both styles. A picture pack has no `extends`.

### Cost

Rendering is about the smooth crew's or less, since the member is a picture: a station takes about 50 ms at
63 x 57, 55 ms at 126 x 114 and 80 ms at 180 x 152 for six frames (a two-frame pose about a third of that), and
compresses to the same sizes as the smooth crew's (a six-frame `seated` is 21 KiB at 63 x 57, 64 KiB at 126 x 114,
107 KiB at 180 x 152). Loading reads every picture once and keeps it compressed: a full six-member starter pack
(168 pictures) loads in about a second.

### Errors

Every problem is reported at load time with the file and what is wrong: not a folder, no `pack.json`, invalid
JSON (with the line), an unknown key, a member without a folder, a folder without `seated.png` or
`hand_up.png`, an `empty.png`, a file that isn't a pose, a frame numbered from 0, a gap in the numbering, more
than 8 frames, a PNG without an alpha channel (a JPEG renamed, or an RGB export), a picture with no transparent
pixel at all (a background left in), the wrong proportions, too small, a file that isn't an image, a gap out of
range, a `lamp_light` or `effects` of the wrong kind.

## Checking a pack

```sh
uv run python tools/crew_sheet.py --pack ~/my-crew --out /tmp/crew-sheets
uv run python tools/crew_sheet.py --pack ~/my-pictures --against smooth --out /tmp/crew-sheets --only sheets
```

writes contact sheets of every member in every pose at the real station sizes (on dark navy and on a light
background), every frame of each pose, the ensigns, the chair and the viewscreen, a sheet of those "bits" as
glass really draws them, and animated GIFs at the real timing. `--sizes` picks the sheets' station sizes
(`small` 63x57, `medium` 90x76, `large` 126x114, `retina` 180x152, or `WxH`), `--gif-size` one size for the
GIFs, `--prefix` a prefix for every file name, `--only` which outputs, and `--against` a second pack whose
member of the same id is drawn under each of yours. It never opens a window: look at the files with an image
viewer.

`uv run pytest tests/test_crew.py` checks the built-in crew, synthetic picture packs and the export tool. To
check a pack of yours the same way, load it in a test and call the sprite makers: every frame must be
`width * height * 4` bytes with a fully transparent outer ring (the app relies on it).

## The code

`bridge/crew/__init__.py` is the contract the app uses (`load`, `Pack`, `POSES`, `Member`, `PackError`);
`_pack.py` what both styles share (the manifest helpers, the errors); `_smooth.py` the smooth style (loading,
the sprite makers, `member_picture` for the export tool), with `_figure.py` (the rig of every pose and frame,
the head, hair, faces and body), `_scene.py` (console, chairs, ensigns, effects), `_space.py` (the viewscreen)
and `_paint.py` (the canvas, shapes and light); `_picture.py` the picture style (loading and checking the
PNGs, scaling, the lamp's light, the station round a picture); `_colors.py` the colour expressions.
`tools/export_pictures.py` writes a starter picture pack, `tools/crew_sheet.py` the contact sheets and GIFs.
