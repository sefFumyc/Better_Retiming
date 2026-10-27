# 源图缓存把子帧取整 —— 掉帧根因取证（Blender 5.2.1）

**结论先行**：关掉混合帧后暴露的"掉帧、不是无级变速"，根因是 VSE 的
**源图缓存（Raw cache）在建键前把时间取整**，而 Speed Control 承载恰好是唯一把
"小数时间"放在被取整位置上的通道。这是纯上游缺陷，插件范围内只能提示 +
让用户关缓存；上游报告草稿见 `upstream-report-source-image-cache.md`。

## 机制

`source/blender/sequencer/intern/cache/source_image_cache.cc`（v5.2.1）：

```cpp
SeqResult source_image_cache_get(const RenderData *context, ..., float timeline_frame)
{
  timeline_frame = math::round(timeline_frame);   // L143：取整发生在建键之前
```

```cpp
void source_image_cache_put(const RenderData *context, ..., float timeline_frame, ...)
{
  timeline_frame = math::round(timeline_frame);   // L193：同样
```

写入受 `render.cc` 的 `SEQ_CACHE_STORE_RAW`（RNA `use_cache_raw`）控制：

```cpp
if (orig_scene->ed->cache_flag & SEQ_CACHE_STORE_RAW) {   // render.cc L757 / L1529
  source_image_cache_put(context, strip, timeline_frame, input);
```

这个键本是给**条带自己的整数时间**设计的，代码注释里还特意给场景条带留了小数
（"slowed down scene strips need fractional frame index"）——但 get/put 的 round
又把它抹平了。`--factory-startup` 下 `use_cache_raw` / `use_cache_final` /
`use_prefetch` **默认全开**。

### 为什么恰好 Speed Control 中招

| 通道 | 小数在哪产生 | 相对取整的位置 | 结果 |
|---|---|---|---|
| 原生 Retiming | `give_frame_index` 内部 | 取整**之后** | 键每帧唯一 → 每帧真渲染 → 无论多慢都流畅 |
| Speed Control | `strip_speed_effect_target_frame_get` 的返回值，放在 `timeline_frame` | 取整**之前** | 同一整数源帧内的输出帧撞同一个键 → 掉帧 |

每个整数桶里的图是"第一个撞进这个桶的小数时间"渲染出来的——桶里其余输出帧
全部复用它。所以不只是"帧少了"，桶内画面内容还带任意性。

### 为什么 v1.0.4 之前没暴露

插值开（混合帧）时 `render.cc` 会先 `floor(target)` 再传下去，本来就在整数桶里——
是交叉淡化（重影）把阶梯盖住了。关掉混合没有让画面变差，只是把原来被溶解遮住的
阶梯露出来。

## 实测（本机 Blender 5.2.1，`tools/probe_scene_strip_time.py`）

0.25x 慢放（源位置逐帧 +0.25），渲染主场景 10 帧，用源场景里的 Python 驱动器
当记录仪（先自检 `frame_set(5, subframe=0.25)` 能记到 5.25，证明小数可见）：

```
# 关缓存（use_cache_raw=False use_cache_final=False use_prefetch=False）
[probe] SRC 被求值的 frame 序列（去重相邻）： [1.0, 1.25, 1.5, 1.75, 2.0, 2.25, 2.5, 2.75, 3.0, 3.25]
[probe] 期望（0.25x 慢放，逐帧 +0.25）：      [1.0, 1.25, 1.5, 1.75, 2.0, 2.25, 2.5, 2.75, 3.0, 3.25]  ✓ 逐帧吻合

# 开缓存（默认状态）
[probe] SRC 被求值的 frame 序列（去重相邻）： [1.0, 1.5, 2.5]
```

开缓存时 **10 个输出帧只求值 3 次**：键 `round(源位置)` 落 1/2/3 三个桶
（1.25→1、1.5 与 2.25→2、2.5 起→3），桶内的输出帧全部复用第一次撞桶时的那张图。

复现命令：

```
"D:\Tools\blender-5.2.1-windows-x64\blender.exe" --factory-startup --background \
    --python tools/probe_scene_strip_time.py -- --cache both
```

退出码 0 = 与预期一致（off 无级、on 掉帧）。上游修复后该工具会以
"MISMATCH" 退出——那是好消息。

## 插件侧对策（决定，见 ADR-0002）

- 承载**仍走 Speed Control**，不迁回原生 Retiming（见 ADR-0002 里的否决理由）。
- 面板在"场景素材 + Raw 缓存开"时显示**无级受限**并给一键开关
  「关闭 Raw 缓存（恢复无级）」——由用户点击，不自动改；缓存是场景级设置，
  关掉会连带影响同场景无关条带的性能。
- 缓存关掉即恢复无级（上面第一组实测就是证据）。
