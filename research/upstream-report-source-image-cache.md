# 上游缺陷报告草稿：VSE source image cache rounds the timeline frame before keying, breaking subframe rendering of slowed scene strips

> 状态：草稿（未提交）。英文正文按 projects.blender.org 的报告模板写；
> 证据与数字来自本仓库 `tools/probe_scene_strip_time.py` 的实测，
> 机制分析见 `research/source-image-cache-subframe.md`。

**Blender version:** 5.2.1 LTS（`hash 9e2066aef7ef`）
**Short description of the error:** Scene strips slowed down via a Speed Control
effect strip no longer render at subframe precision when the VSE source image
cache is enabled (default): the output stutters (repeated frames), while native
retiming stays perfectly smooth at the same speed.

## Exact steps to reproduce

1. Factory startup. In the source scene, add a Python driver that records the
   frame it receives (e.g. an object driver with expression `log(frame)`), a
   camera, frame range 1–60.
2. In the main scene create a sequence editor and add the source scene as a
   scene strip (30 frames).
3. Add a Speed Control effect strip in MULTIPLY mode, `speed_factor = 0.25`
   keyframed flat, `use_frame_interpolate = False`.
4. Ensure the VSE cache flags are at their defaults
   (`use_cache_raw = True`, `use_prefetch = True`).
5. Render frames 1–10 of the main scene with `--background`.
6. Compare with `use_cache_raw = False`.

A ready-made repro script lives in
`tools/probe_scene_strip_time.py` (this repository):

```
blender --factory-startup --background --python tools/probe_scene_strip_time.py -- --cache both
```

## Expected result

At 0.25x, the source scene is evaluated at 1.0, 1.25, 1.5, … 3.25 — one
subframe-accurate evaluation per output frame (`seq_render_scene_strip_ex`
already computes the fractional frame and passes it to `RE_RenderFrame` as
`floorf(frame)` + `subframe`).

## Actual result

With `use_cache_raw = True` (the default), the source scene is evaluated only
**3 times for 10 output frames**: `[1.0, 1.5, 2.5]`. Every output frame whose
`round(target_frame)` lands in the same integer bucket reuses the image that
the *first* frame to hit that bucket happened to render — so playback
stutters and the reused image content is essentially arbitrary.

With `use_cache_raw = False`, the evaluation sequence matches the expectation
exactly: `[1.0, 1.25, 1.5, 1.75, 2.0, 2.25, 2.5, 2.75, 3.0, 3.25]`.

## Analysis

`cache/source_image_cache.cc` rounds the timeline frame **before** using it as
a cache key, in both paths:

```cpp
SeqResult source_image_cache_get(const RenderData *context, ..., float timeline_frame)
{
  timeline_frame = math::round(timeline_frame);   // L143

void source_image_cache_put(const RenderData *context, ..., float timeline_frame, ...)
{
  timeline_frame = math::round(timeline_frame);   // L193
```

The cache is written when `SEQ_CACHE_STORE_RAW` is set
(`render.cc` L757 / L1529, RNA: `use_cache_raw`).

The comment in that file says the cache intends to support "slowed down scene
strips need fractional frame index" — but the pre-key rounding defeats it:

- **Native retiming** produces its fractional source position inside
  `give_frame_index` (i.e. *after* the cache lookup happens on the strip's own
  integer timeline frame), so keys stay unique per output frame and every frame
  renders for real.
- **Speed Control** produces its fractional source position in
  `strip_speed_effect_target_frame_get`, which lands in the input strip's
  `timeline_frame` argument — exactly the value that gets rounded away before
  keying. All output frames inside one integer source bucket collapse onto one
  cache entry.

Since v5.x, `use_frame_interpolate` also *floors* the target frame
(`seq_render_effect_strip_impl`), which is why this was previously masked by
the crossfade — disabling interpolation exposes the stepped cache behavior.

## Fix suggestion

In `source_image_cache_get` / `source_image_cache_put`, do not round the
timeline frame for scene strips (the fractional frame is meaningful for them);
equivalently, key by the value `give_cache_frame_index()` produces instead of
`math::round(timeline_frame)` when the strip renders subframes. Round-keying
remains correct for movie/image inputs, which decode discrete frames anyway.

## Workaround

Turn off `use_cache_raw` for the scene (via Python:
`scene.sequence_editor.use_cache_raw = False`). Cost: raw-cache benefits are
lost for all strips in that scene.
