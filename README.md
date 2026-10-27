[简体中文](README.zh-CN.md) | **English**

# Better Retiming — Vegas-style velocity line for Blender VSE

![Velocity line demo](docs/screenshot/1.png)

A pure-Python add-on for **Blender 5.2.1+** (package name `seqvel`). It overlays a
**velocity line** on strips in the Video Sequence Editor and lets you edit playback speed
the way Vegas Pro does — drag the whole line for a constant speed, double-click to insert
speed points, and drag points apart to create ease-in / ease-out ramps.

The result is written as `speed_factor` keyframes on a **Speed Control** effect strip, so
preview, viewport playback, render and FFmpeg export all go through Blender's **native
pipeline**. You never need to enable native Retiming.

## Features

- **Direct manipulation, no edit mode** — press on a visible line or speed point and drag.
  Clicks on empty space pass straight through, so selection, box-select, playhead and
  spacebar playback are never hijacked.
- **Segment-wise editing** — drag a segment to move both of its endpoints, drag the tail
  extension to move only the last point, drag a point to change its speed and time.
- **Four segment curve types** — Linear, Smooth, Fast, Slow. The name describes *which end
  of the segment the change is concentrated in*, independent of whether speed rises or falls.
- **Ctrl fine-tune** — hold Ctrl during any drag for half-speed movement; point drags also
  freeze the time position. You can toggle it mid-drag without jumps.
- **Right-click menus** — point menu (delete point) and segment menu (curve type).
- **RAM preview (`Shift+B`)** — Vegas-style "build dynamic RAM preview": plays the current
  range once to fill Blender's own VSE cache, then returns the playhead.
- **Split with speed curve (`K`)** — divides the strip at the playhead and splits the curve
  so both halves join seamlessly, frame for frame; the split itself is a single undo step.
- **Per-action undo** — each drag, insert or delete is one undo step.
- **Bilingual UI** — English by default, 中文 switchable in the sidebar; the choice is stored
  in the add-on preferences and persists across `.blend` files.
- **Zero frame blending** — deliberately never mixes adjacent frames; no ghosting.

## Installation

Download the latest `seqvel-<version>.zip` from
[Releases](https://github.com/sefFumyc/Better_Retiming/releases), then go to
**Edit → Preferences → Add-ons → Install**, pick the zip and enable the add-on.

You can also build it yourself, or copy the `seqvel/` folder into
`%APPDATA%\Blender Foundation\Blender\5.2\scripts\addons\`:

```bash
python tools/build_zip.py          # produces dist/seqvel-<version>.zip
```

> **After updating, make Blender reload the add-on.** Blender does not re-import modules
> whose files changed on disk, so a reinstall alone keeps the old code in memory. Either
> restart Blender, toggle the add-on off and on, or run **Reload Scripts** (`F3`).
> The bottom of the sidebar panel always shows the running version (e.g. `Add-on v1.6.6`);
> if that line is missing or stale, you are still running old code.

## Quick start

1. Open the Video Sequence Editor and select a video or image strip — the velocity line
   appears immediately at 1.0x.
2. Hover over the strip; the cursor becomes a pointing hand where the line can be grabbed.
3. Press and drag. Release to commit the curve as `speed_factor` keyframes on an
   `SPD_<strip name>` Speed Control strip.
4. Press on empty space and the event goes to Blender untouched.

| Action | Effect |
|---|---|
| Drag a segment | Move that segment only (both endpoints together) |
| Drag the tail extension | Move the last point only |
| Double-click a segment | Insert a speed point |
| Drag a speed point | Change speed vertically, time horizontally (first point is x-locked) |
| **Ctrl + drag** | Fine-tune: half the speed change; point drags freeze the time position |
| Right-click a point | Point menu (delete point) |
| Right-click a segment | Segment menu: Linear / Smooth / Fast / Slow |
| Right-click elsewhere on the strip | Native context menu (not intercepted) |
| **`Shift+B`** | Build RAM preview for the current range |
| **`K`** | Split the strip at the playhead, splitting the curve too |
| Ctrl+Z / Ctrl+Shift+Z | Undo / redo, one action per step |

### Segment curve types

The name says *where inside the segment the change happens*, not whether speed goes up or down:

| Type | Shape | Rising (100%→300%) | Falling (300%→100%) |
|---|---|---|---|
| Linear | Constant rate | Straight | Straight |
| Smooth | Slow at both ends, change in the middle | S-curve | S-curve |
| Fast | Fast start, gentle finish | Convex up | Convex down |
| Slow | Gentle start, fast finish | Convex down | Convex up |

The default type for new curves is **Smooth** (Vegas-like out-of-the-box behaviour); it is
read once, when the curve is created, and never retroactively applied to existing curves.

## How it works

- Each target strip gets one `SPD_` **Speed Control** effect strip, bound through `input_1`
  and de-duplicated by that binding (renaming does not create duplicates).
- Mode `speed_control='MULTIPLY'` with `use_frame_interpolate=False` — **never blend frames**.
- The shadow curve model is the single source of truth. On commit, segments are sampled
  according to their curve type and written as **LINEAR** keyframes. Eased segments are
  written per frame; constant-speed segments only write their endpoints.
- Before writing, existing native Retiming data on the input strip is reset, so the two
  mechanisms never multiply.
- The curve is persisted in the strip's custom property `seqvel_curve`, so it survives
  saving and reopening the `.blend`.
- RAM preview does not touch a single pixel: it fills Blender's own VSE cache, so preview,
  render and export stay on the same path.

Detailed rationale lives in `docs/adr/` and `research/`.

## Compatibility and limitations

- Blender **5.2.1 or newer** (declared `blender: (5, 2, 0)`).
- **Audio is not retimed.** Speed Control affects images only; pure audio strips are
  rejected with an explanation, and audio on a video strip keeps its original speed and pitch.
- **Frame sampling depends on strip type** and the panel always shows which one is active:
  - **Scene strips → subframe rendering (stepless)**: fractional source positions are passed
    to the renderer, so slow motion stays smooth and never ghosts.
  - **Movie / image / movie clip → frame snap**: source positions are rounded to the nearest
    frame, so non-integer speeds repeat or skip frames (same as native Retiming).
- **No frame blending**, by design — blending adjacent frames just looks like ghosting.
- On scene strips, Blender's **source image cache** rounds time before building cache keys,
  which can drop subframe precision. The panel detects this and offers a one-click toggle for
  that scene's raw cache. This is an upstream limitation; see
  `research/source-image-cache-subframe.md`.
- The first speed point is locked to the strip start and cannot be deleted.
- Not in v1: reverse playback (negative speeds), multi-strip batch editing, Blender
  extension-store packaging.

## Sidebar panel

The sidebar tab is called **Velocity**. From top to bottom:

1. **Language** — English / 中文, default English (stored in add-on preferences).
2. **Settings** — hide native Retiming overlay while editing, show speed percentages,
   default curve type for new curves, and speed scale cap (300% or 1000%).
3. **Target strip** — the active or selected strip, with a remove button and explicit
   reasons when no editable target exists.
4. **RAM preview** — range and source, estimated memory, current cache state.
5. **Warnings** — audio, scene mismatch, subframe limitations.
6. **Diagnostics** — timeline region size, hit-test result, carrier status, curve type
   distribution, and the running version.

## Development

The code is layered; do not skip layers:

```
seqvel/curve.py        pure-Python shadow model (single source of truth, no bpy)
seqvel/geometry.py     strip coordinate mapping and hit testing (pure Python)
seqvel/rules.py        strip type rules (pure Python)
seqvel/regions.py      locate the timeline region (pure Python) — context.region is unreliable
seqvel/editing.py      interaction primitives: double-click, right-click targets
seqvel/ram.py          RAM preview rules: range, memory budget, watermark
seqvel/store.py        model cache and persistence (strip custom properties)
seqvel/carrier.py      landing: Speed Control strip + speed_factor keyframes
seqvel/watch.py        strip span listener: end carry, carrier re-landing, preview invalidation
seqvel/preview.py      RAM preview operator (Shift+B)
seqvel/overlay.py      overlay drawing (draw handler + gpu/blf)
seqvel/hover.py        hover cursor (MOUSEMOVE keymap → HAND_POINT)
seqvel/interaction.py  modal operators and point / segment menus
seqvel/status.py       runtime state: single exit for panel text and diagnostics
seqvel/ui.py           sidebar panel and settings
```

`seqvel/__init__.py` must not touch `bpy` at module level (not even decorators), because the
pure-Python test path imports the package with `bpy = None`.

Run the full suite with Blender's bundled Python (replace `blender` with your install path):

```bash
blender --factory-startup --background --python tests/run_tests.py
```

Fast path without Blender (skips bpy-dependent cases by `HAVE_BPY`):

```bash
python tests/run_tests.py
```

GUI smoke test (opens a temporary Blender window, expands the VSE sidebar, no input injected):

```bash
blender --factory-startup --python tools/gui_smoke.py -- --report %TEMP%\seqvel_gui_smoke.txt
```

Static check for undefined names:

```bash
python tools/check_undefined_names.py   # requires pyflakes
```

`dist/` and `__pycache__` are not committed. Packaging check:
`python tools/build_zip.py --check`.

## Documentation

- [`CONTEXT.md`](CONTEXT.md) — domain glossary (speed line, speed point, segment, shadow
  model, speed carrier, and more)
- [`docs/adr/`](docs/adr/) — architecture decision records
- [`research/`](research/) — upstream data model, source image cache, and interaction notes

## License

GPL-2.0-or-later. See [LICENSE](LICENSE).
