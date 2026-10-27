# Speed Control 效果条带作为"Vegas 式速度线"原生承载机制调研（Blender 5.2.1）

- 调研日期：2026-09-14
- 目标环境：`D:\Tools\blender-5.2.1-windows-x64\blender.exe`（5.2.1 LTS，实测 `bpy.app.version_string` = `5.2.1 LTS`）
- 方法（按优先级）：① 本机 5.2.1 二进制内省（`--background --python` 与 GUI 模式 `--python` 实测 bpy 类型/属性/工厂方法/关键帧存储；sequencer operator 在脚本上下文 poll 失败，见 1.1）；② 官方 Python API 文档（docs.blender.org/api/5.2：`SpeedControlStrip`、`StripsTopLevel`、`Strip`）；③ Blender 官方源码 `v5.2.1` tag（`sequencer/intern/effects/effect_speed.cc`、`sequencer/intern/strip_relations.cc`、`sequencer/intern/strip_retiming.cc`、`sequencer/intern/sound.cc`、`sequencer/intern/strip_time.cc`、`makesrna/intern/rna_sequencer.cc`、`rna_sequencer_api.cc`、`blenkernel/intern/fcurve.cc`、`render/intern/pipeline.cc`、`makesdna/DNA_sequence_types.h`）；④ 官方发布说明（5.1：Speed Control 负值 PR#152982）与官方手册内容镜像（vse-docs，源自官方 manual）佐证。
- 实证限制：**本机自动渲染（后台与 GUI 脚本）均不可靠**——`bpy.ops.render.render()` 输出的帧仅首像素有效/全黑（疑似虚拟化环境 GPU/驱动问题，与 Blender 无关）。因此**帧映射行为结论以 C 源码 + 官方文档为准**，未做逐帧像素级验证。条带创建/绑定/属性/关键帧存储均为实测。
- 服务对象：`.scratch/vegas-speed-line/map.md` Q6（数据落地策略）与票 04（承载机制验证）、票 03（原型）。

---

## 0. 结论速览（TL;DR）

- **推荐承载机制：Speed Control 效果条带（`SpeedControlStrip`），`speed_control='MULTIPLY'` + `speed_factor` 关键帧。** 这是唯一能连续表达"任意速度曲线"、且 Python 可直接全量读写的原生通道。
- **创建**：5.x 用工厂方法 `scene.sequence_editor.strips.new_effect(name, type='SPEED', channel, frame_start, length, input1=目标条带)`（官方 API `StripsTopLevel.new_effect`，实测可用、无需 operator 上下文）；绑定 = `input_1` 指针（实测可读写、可重绑定）。UI 路径 `bpy.ops.sequencer.effect_strip_add(type='SPEED')` 存在，但 poll 要求活动 SEQUENCE_EDITOR 区域 + 有效 sequencer scene，**脚本/后台上下文不可用**（与 01 票结论一致）。
- **关键帧**：`speed_factor` 可打关键帧（float，无有效范围限制，`is_animatable=True`）；**仅 MULTIPLY 模式读取关键帧**（STRETCH/LENGTH/FRAME_NUMBER 忽略）；关键帧插值（linear/bezier）**真正影响速度**——C 侧把每帧的 fcurve 求值**逐帧累加**成 `frameMap`，即 speed_factor 语义 = "每输出帧前进的源帧增量"，源映射 = 速度的积分。关键帧数据存于 scene 的 5.x slotted/layered action，Python 侧读回不便（见 2.3）。
- **负值**：`speed_factor` 负值 = 反向播放（5.1 起官方支持，PR#152982）。静态负值 = 从源末尾倒放；关键帧负值 = 积分反向（累加到 0 被 clamp 冻结）。
- **音频**：Speed Control **只处理视频**；对 SoundStrip 输入**不变速不变调、也无 pitch 补偿选项**。音频变速变调的正规通道是 **Retiming**（逐帧写 pitch；`Strip.pitch_correction` 可保音高）。
- **渲染/导出**：预览与 FFmpeg 导出走同一 `seq_render` 求值路径 → **渲染=预览一致**。帧精度注意：默认不做运动插值，按目标源位置取整选帧 → 非整数倍速产生**不均匀的重复/跳帧**；`use_frame_interpolate=True`（5.1+ 新增）改为相邻帧交叉淡化。
- **与 retiming 共存**：效果条带本身不能 retime（`retiming_is_allowed` 排除 effect）；**输入条带可以同时有 retiming keys，且二者叠乘（组合）**——插件落地时应先 `retiming_reset` 输入条带，避免双重变速。
- **框架影响**：5.x 已移除 `frame_step`/`use_frame_step`/`use_scale_to_length`（旧机制淘汰），新增 4 种 `speed_control` 模式 + `use_frame_interpolate`。

---

## 1. Q(a) 如何创建、如何与目标条带绑定

### 1.1 创建（两条路径）

1. **程序化（插件推荐，实测可用，无需区域上下文）**：
   ```python
   ed = bpy.context.scene.sequence_editor
   sp = ed.strips.new_effect(name="SPD", type="SPEED", channel=2,
                             frame_start=1, length=200, input1=target_strip)
   ```
   - 官方 API：`StripsTopLevel.new_effect(name, type, channel, frame_start, *, length=0, input1=None, input2=None)`；`type` 含 `'SPEED'`。
   - 实测：创建后 `sp.input_1.name == target.name`，`sp.input_count == 1`；`sp.input_1 = 其他条带` 可**重绑定**。
2. **UI/operator（用户手动操作）**：选中目标条带 → Add ‣ Effect ‣ Speed Control → 效果条带自动生成在目标条带上方并绑定。`bpy.ops.sequencer.effect_strip_add(type='SPEED', move_strips=True, frame_start=0, length=0, channel=1, ...)`——**poll 失败于脚本/后台上下文**（需要活动 SEQUENCE_EDITOR 区域 + `CTX_data_sequencer_scene` 有效，实测 `poll()==False`、报 "Context missing sequencer scene"）。插件内部创建一律走工厂方法。

### 1.2 绑定与层关系

- 绑定 = **显式 `input_1` 指针**（不是通道自动吸附）。`SpeedControlStrip.input_1`（官方 API："First input for the effect strip (never None)"）。
- **通道关系**：效果条带必须位于输入条带**更高的通道**（channel 数值更大），否则合成时被输入条带盖住看不到效果。官方手册：效果条带放在目标上方、可视为目标的"变速副本"，可隐藏原始条带。
- **对齐**：无需帧对齐；效果条带自身的时间范围（`frame_start` + `length`，即 `left_handle`~`right_handle`）决定变速作用的输出区间。输入条带的内容范围（`content_start`~`content_end`）决定源区间。
- 新效果条带默认 `speed_control='STRETCH'`、`speed_factor=1.0`（实测）——**插件须显式设 `'MULTIPLY'`**。

---

## 2. Q(b) `speed_factor` 关键帧与插值是否生效

### 2.1 属性本身

- 实测 `bl_rna`：`speed_factor` = FLOAT，`default 0.0`，`hard_min/max = ±FLT_MAX`（即无有效范围限制，float32 连续精度），`is_animatable=True`（可打关键帧）。`speed_control` 不可动画（`PROP_ANIMATABLE` 被清除）；`use_frame_interpolate` 可动画。
- 语义（官方 API 文案）："Multiply the current speed of the strip with this number or remap current frame to this frame"。

### 2.2 关键帧是否生效、插值是否影响速度 —— **生效，且插值真正参与速度曲线**

C 源码 `effect_speed.cc`（v5.2.1）MULTIPLY 分支：

```cpp
case SEQ_SPEED_MULTIPLY: {
  const FCurve *fcu = strip_effect_speed_speed_factor_curve_get(scene, strip_speed);
  if (fcu != nullptr) {
    strip_effect_speed_frame_map_ensure(scene, strip_speed);
    target_frame = s->frameMap[frame_index];      // 有关键帧 → 用预计算积分表
  }
  else {
    target_frame = frame_index * s->speed_fader;  // 无关键帧 → 线性乘
    ...
  }
}
```

- **有 fcurve（打了关键帧）时**：`strip_effect_speed_rebuild_map` 逐帧执行 `target_frame += evaluate_fcurve(fcu, strip->left_handle() + frame_index)` 并 `CLAMP(0, source.len)` → **frameMap 是 speed_factor 的积分**。因此：
  - speed_factor 语义 = 每输出帧的源帧增量（速度），源映射 = 累加和；
  - **linear/bezier 插值逐帧参与累加，直接塑形源映射** → 插值类型真实影响速度曲线（不是只影响数值显示）；
  - 恒值关键帧（如全程 2.0）与静态 `speed_factor=2.0` 结果一致（Σ2.0 = 2·i）。
- **无 fcurve（未打关键帧）时**：`target = frame_index × speed_factor`（常数斜率）。
- **仅 MULTIPLY 模式读取关键帧**：STRETCH（右柄比率）、LENGTH（百分比）、FRAME_NUMBER（帧号）分支都不读 fcurve。
- 动态一致性：`strip_relations.cc::relations_invalidate_cache` 在条带缓存失效时**重建 frameMap**（改关键帧 → RNA 更新 → 失效 → 重建），无需插件手动刷新。

### 2.3 Python 读回注意（5.x slotted action）

- 实测 `sp.keyframe_insert(data_path="speed_factor", ...)` 正常；关键帧数据存入 **scene 的 action**（`scene.animation_data.action`，名为 `SceneAction`，`is_action_layered=True`）——5.x 新动画系统。
- **Python 无 `action.fcurves`**（实测 AttributeError；旧 4.x API 失效）；读取需走 `layers`/`slots` 结构。
- **勘误（实现阶段实测，v1 已据此加了自动测试）**：走 `layers`/`slots` 是**完全可读**的，此前"遍历 keyframes 计数为 0"是遍历姿势不对。可用路径：
  `scene.animation_data.action.layers[*].strips[*].channelbags[*].fcurves[*]`，其中 `fcurve.data_path == 'sequence_editor.strips_all["<条带名>"].speed_factor'`，`fcurve.keyframe_points[*].co` 给 (帧, 数值)、`.interpolation` 给插值类型。清空关键帧用 `keyframe_points.remove(kp)` 逐个删（`keyframe_delete(data_path=...)` 不传 frame **只删当前帧的那一个**，不是全清）；`channelbag.fcurves.remove(fcurve)` 可整条摘掉曲线。
  插件仍保持"单向写、不读回"的事实源策略，读回只用于**测试断言与面板校验**。
- 插件策略：**自持影子模型，不读回原生关键帧**（与 01 票"必须自建影子模型"一致）；如需校验可只校验自己写入的数值（内存中已有）。

---

## 3. Q(c) 负值 = 反向播放？速度范围？

- **范围**：无硬性限制（float，±FLT_MAX）。
- **静态负值（无关键帧）**：`target_frame = frame_index × fader + source->length`（源码注释："Treat target_frame as a negative offset from the last frame"）→ **从源末尾开始倒放**（如 -1.0：199,198,197,…）。CLAMP 到 `[0, source.len]`。
- **关键帧负值**：frameMap 逐帧累加，负增量使 target 回退 → **真正反向**；但 `CLAMP(0, len)` 下限 0 → 累加到 0 后冻结，直到后续累加变正。
- 官方支持：**5.1 起 Multiply 模式允许负值**（发布说明 PR#152982；01 票已记录"反向播放另有通道：Speed Control 负值"）。
- 与 Retiming 对比：Retiming 结构上不支持负速度；**Speed Control 是 VSE 反向播放的正规原生通道**（map 中"反向播放——负 speed_factor 依赖 04"的待定项在此落地）。

---

## 4. Q(d) 音频条带 + Speed Control

- **只处理视频**：`effect_speed.cc` 的 `do_speed_effect` 仅产出视频 ImBuf；**对音频无任何处理**（不变速、不变调）。
- **无 pitch 补偿选项**：`SpeedControlStrip` 全部属性实测无 pitch 相关项（只有 `speed_control` / `speed_factor` / `speed_frame_number` / `speed_length` / `use_frame_interpolate`）。`Strip.pitch_correction`（= C 侧 `SEQ_AUDIO_PITCH_CORRECTION`）只作用于 **Retiming 的音频路径**（`strip_retiming.cc::retiming_sound_animation_data_set`：默认逐帧写 pitch=变速变调；开 correction 走 time-stretch 保音高）。
- 音频条带被 Speed Control 引用时，音频按 SoundStrip 自身时间轴播放（`sound_update_bounds` → `BKE_sound_move_scene_sound_defaults`），**pitch 恒为 1**。
- 结论：**音频变速变调必须走 Retiming**（或 Sound 条带自身机制）；插件 v1 应提示用户"Speed Control 不影响音频"，或对音频条带单独用 Retiming 落地（每段等速近似）。`SoundStrip.speed_factor`（旧 pitch 字段）在 5.2.1 中无实际调用方（死字段，勿依赖）。

---

## 5. Q(e) 渲染/导出一致性与帧精度

- **一致**：预览与最终渲染/FFmpeg 导出走同一 `seq_render` 求值（`render_give_ibuf` → `strip_speed_effect_target_frame_get`）；`pipeline.cc::do_render_sequencer` 在渲染时调用同一路径。**渲染=预览**。
- **帧精度（关键注意点）**：
  - 默认（`use_frame_interpolate=False`）：**不做运动插值**，显示帧 = 目标源位置取整选帧（非整数倍速 → 不均匀的重复/跳帧）。官方手册示例（Stretch 1.67x）：`0 1 3 5 6 8 10 …`；0.5x → 每帧重复（0,0,1,1,…）；2x → 隔帧跳（0,2,4,…）。
  - `use_frame_interpolate=True`（官方 API：crossfade blending between current and next frame；5.1+ 新增，替代旧 frame_step 时代的插值）：输出 = floor/ceil 源帧的交叉淡化 → 平滑但产生**混合帧**（导出同样如此，非"运动矢量重采样"）。
  - 对"Vegas 式速度线"的影响：整数倍速下无精度问题；**非整数/渐变速度下若需精确帧采样就关插值（接受重复帧），要平滑就开插值（接受混合帧）**——这是插件要暴露给用户的选择。

---

## 6. Q(f) 与 Retiming 能否共存/互斥

- **效果条带本身不能 retime**：`strip_retiming.cc::retiming_is_allowed` 排除 effect 类型（实测 `retiming_key_add` 对该条带 poll 失败，报 "This strip type cannot be retimed"）。
- **输入条带可以 retime，且二者叠乘**：`strip_time.cc::give_frame_index` 对输入条带先应用其 retiming 求值（`frame_index = strip_retiming_evaluate(...) × len`），再叠加 Speed 映射 → **Speed Control 与输入条带上的 retiming 组合生效**（不是互斥）。
- 插件对策：自建模型落地到 Speed Control 时，**先对输入条带 `bpy.ops.sequencer.retiming_reset()`**（沿用 map "已有原生 keys 重置重建"策略），避免双重变速语义混乱。

---

## 7. 备选方案对比（各一句可行性结论）

| 方案 | 可行性结论 |
|---|---|
| 7a. 导出/渲染时把自建曲线近似写入原生 retiming（多段等速 + transition） | **可行但仅适合一次性导出**：段内恒定速度 + 固定切线圆弧过渡，自由曲线只能靠增加 key 密度逼近（无硬性上限，精度可控但数据量大）；Python 读不回段速度、OUT key 赋值被忽略、transition 不能与 freeze 叠加，**不适合作为编辑↔原生双向同步层**；且不支持负速度（反向仍要 Speed Control）。 |
| 7b. 老式 strip 速度机制（frame_step/use_frame_step 时代） | **不可用**：实测 5.2.1 的 SpeedControlStrip 已无 `frame_step`/`use_frame_step`/`use_scale_to_length`（RNA 与 API 文档均无）；唯一保留的老式项是 `use_reverse_frames`（整条反向），无法表达连续速度曲线。 |
| 7c. 直接改写条带源帧映射（time remap 类） | **无精确 Python 入口**：不存在通用的"源帧映射表"可写；可写的只有 `retiming_keys[i].timeline_frame`（仅时间轴锚点、源位置不可写不可读）、`Strip.frame_offset_start/end`（静态裁切）、`media_playback_rate`（静态 fps）。**最接近"连续源映射"的原生通道恰恰就是 Speed Control 的 `speed_factor` 关键帧（C 侧积分成 frameMap）**。 |

---

## 8. 推荐：承载机制 + 同步方案

### 8.1 承载机制（推荐结论）

**Speed Control 效果条带：`speed_control='MULTIPLY'` + `speed_factor` 关键帧（linear/bezier 均可）。**
理由：唯一能连续表达任意速度曲线、Python 可全量读写、渲染/导出与预览一致、原生支持负值（反向）的原生机制；其 C 侧"逐帧累加求积分"的 frameMap 模型与 Vegas 速度线（速度=位置导数）天然同构——插件写"瞬时速度"，Blender 端自动积分成源映射。

### 8.2 同步方案（何时写、写什么、怎么保证一致）

**何时写**：
- 编辑操作完成时（modal 结束 / 条带切换 / 保存前）一次性全量重写 `speed_factor` 关键帧（推荐，避免编辑中高频写造成关键帧抖动与撤销栈噪音）；v1 不做增量 diff。

**写什么**（每目标条带一个 Speed 效果条带）：
1. `sp = ed.strips.new_effect(name, type='SPEED', channel=目标通道+1, frame_start=目标区间起点, length=区间长度, input1=目标条带)`；显式 `sp.speed_control='MULTIPLY'`。
2. 清旧关键帧（删除 scene action 中该条带 `speed_factor` 的 fcurve，或对已有效果条带先 `sp.keyframe_delete(...)`/重建）。
3. 把自建速度模型（时间, 速度）在效果条带时间窗内采样为关键帧：**关键帧值 = 模型的瞬时速度（源帧/输出帧）**；分段线性模型 → 每段端点各写一个关键帧（`frame`=时间轴帧，`value`=段速度），插值用 `LINEAR`；需要平滑渐变 → 加密度或 `BEZIER`。
4. 反向段：写负值关键帧（积分模型下直接反向；若希望"到 0 后保持"，让模型速度在该处为 0 即可——clamp 冻结天然等价）。
5. `use_frame_interpolate`：按用户选择设置（默认 False=精确帧采样）。
6. 同时保证：效果条带长度 = 模型时间窗长度；输入条带 `content_start/len`（裁切）与模型源区间一致；输入条带 `retiming_reset()` 清掉原生 retiming。

**怎么保证一致**：
- **单向写入**：自建模型为唯一事实源（shadow model），只写不读回（源位置/段速度原生读不回；5.x action 读取也不便）。写入用 `sp.keyframe_insert(data_path="speed_factor", frame=…)`（实测可用）。
- **失效自动处理**：写入即触发 RNA 更新 → `relations_invalidate_cache` → frameMap 重建，无需手动刷新缓存。
- **防漂移**：用户若直接改动输入条带（裁切/移动/加原生 retiming key），模型与落地可能不一致——插件监听条带变化（`msgbus` 或 depsgraph 更新钩子）后重新采样写入，或提示用户走插件操作。
- **一致性校验**：不读回原生；校验 = 比较模型采样值与刚写入的关键帧数值（内存内对比）+ 效果条带/输入条带的时间范围参数。

---

## 9. 关键 API / 属性速查表（5.2.1 实测/官方文档）

| 用途 | API | 备注 |
|---|---|---|
| 创建 Speed 效果条带 | `ed.strips.new_effect(name, type='SPEED', channel, frame_start, length=…, input1=…)` | 官方 API `StripsTopLevel.new_effect`；无需 operator 上下文（实测） |
| UI 创建（用户手动） | `bpy.ops.sequencer.effect_strip_add(type='SPEED', …)` | poll 需活动 SEQUENCE_EDITOR 区域 + sequencer scene；脚本/后台不可用（实测） |
| 输入绑定 | `sp.input_1`（可读写、可重绑定，实测） | `input_count` 只读 |
| 模式 | `sp.speed_control` ∈ {STRETCH(默认), MULTIPLY, FRAME_NUMBER, LENGTH} | **仅 MULTIPLY 读 speed_factor 关键帧** |
| 速度 | `sp.speed_factor`（float，±FLT_MAX，可动画） | 无 fcurve=线性乘；有 fcurve=逐帧积分 |
| 插值开关 | `sp.use_frame_interpolate`（bool，默认 False） | 相邻帧 crossfade |
| 静态帧号/长度 | `sp.speed_frame_number` / `sp.speed_length` | FRAME_NUMBER / LENGTH 模式用 |
| 条带时间范围 | `frame_start` / `frame_final_duration`（6.0 起改名，注意） | 5.2 仍有 `frame_final_duration`（DeprecationWarning 提示 6.0 移除） |
| 输入条带清 retiming | `bpy.ops.sequencer.retiming_reset()` | 需 UI 上下文；或 `retiming_keys[i].remove()`（首末 key 不可删） |
| 音频保音高 | `Strip.pitch_correction`（bool） | 仅 Retiming 音频路径有效；Speed Control 无 |
| 反向播放 | `speed_factor` 负值 | 5.1+；静态负值=末尾倒放，关键帧负值=积分反向 |
| 5.x 改名 | `Sequence`→`Strip`；`sequences`→`strips`；`sequences_all`→`strips_all`；`active_sequence`→`active_strip` | 实测 `bpy.types.Sequence` 不存在 |

---

## 来源

**主来源（primary）：**
- 本机 Blender 5.2.1 LTS 二进制内省（`--background`/GUI `--python` 实测）：`strips.new_effect` 工厂签名与 `input_1` 绑定/重绑定、`SpeedControlStrip` 全属性 bl_rna（类型/范围/animatable，确认无 frame_step）、关键帧插入与 5.x slotted action（`SceneAction`、`is_action_layered`）、sequencer ops poll 失败（"Context missing sequencer scene"）、retiming_key_add 对效果条带失败（"cannot be retimed"）、`Strip.pitch_correction` 存在性。渲染逐帧验证受环境限制未完成（见头部"实证限制"）。
- Blender 官方 Python API 文档（docs.blender.org/api/5.2）：`bpy.types.SpeedControlStrip`（input_1/input_count/speed_control 枚举/speed_factor/speed_frame_number/speed_length/use_frame_interpolate）、`bpy.types.StripsTopLevel`（new_image/new_movie/new_sound/new_effect 签名，type 含 'SPEED'）。
- Blender 官方源码 `v5.2.1`（GitHub mirror raw）：
  - `source/blender/sequencer/intern/effects/effect_speed.cc`（MULTIPLY 分支 fcurve/frameMap 积分、静态负值末尾倒放、STRETCH 比率、interpolation 分支、fcurve 定位）
  - `source/blender/sequencer/intern/strip_relations.cc`（`relations_invalidate_cache` 重建 frameMap）
  - `source/blender/sequencer/intern/strip_time.cc`（`give_frame_index`：输入条带 retiming 先应用 → 叠乘；media_playback_rate_factor）
  - `source/blender/sequencer/intern/strip_retiming.cc`（`retiming_is_allowed` 排除 effect；音频 pitch/time-stretch 路径）
  - `source/blender/sequencer/intern/sound.cc`（`sound_pitch_get`/`Strip.speed_factor` 死字段）
  - `source/blender/makesrna/intern/rna_sequencer.cc`（speed_factor 无范围、speed_control 不可动画、use_frame_interpolate 标志）
  - `source/blender/makesrna/intern/rna_sequencer_api.cc`（StripsTopLevel.new_* 实现）
  - `source/blender/blenkernel/intern/fcurve.cc`（`id_data_find_fcurve` → `animrig::fcurve_find_in_action_slot`，适配 slotted action）
  - `source/blender/render/intern/pipeline.cc`（`do_render_sequencer`：渲染走同一 seq_render）
  - `source/blender/makesdna/DNA_sequence_types.h`（SpeedControlVars、Strip::speed_factor 注释、eEffectSpeedControlFlags）
- 官方发布说明（developer.blender.org）：5.1 Sequencer —— Speed Control Multiply 负值（PR#152982）。

**辅助（manual/社区佐证）：**
- 官方手册内容镜像 vse-docs（源自 docs.blender.org/manual）：Speed 章节——效果条带放目标上方、可视作副本可隐藏原条带；Stretch 帧序列示例 `0 1 3 5 6 8 10 …`；公式 `Speed[i] = Target[(i/speed length) * target length]`。
- 社区（blog.tenp-kukan.com，Blender 5.0 实测文章）：Speed Control 只处理视频、音频不变速；Multiply 负值=逆再生（静态负值"从最初就负"有怪癖，关键帧负值可靠——与源码行为一致）。
