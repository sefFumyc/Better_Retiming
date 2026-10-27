# VSE Retiming 数据模型能力边界调研（Blender 5.2.1）

- 调研日期：2026-09-14
- 目标环境：`D:\Tools\blender-5.2.1-windows-x64\blender.exe`（5.2.1 LTS，实测 `bpy.app.version_string` = `5.2.1 LTS`）
- 方法（按优先级）：① 本机 5.2.1 二进制内省（`--background --python`，实测 bpy 类型/属性/操作符）；② 官方源码（Blender GitHub `v5.2.1` tag：`makesdna/DNA_sequence_types.h`、`sequencer/intern/strip_retiming.cc`、`editors/space_sequencer/sequencer_retiming.cc`、`makesrna/intern/rna_sequencer.cc`）；③ 官方文档（docs.blender.org API 5.2、developer.blender.org 5.1 发布说明、官方手册中文镜像 5.0）；④ 社区仅作佐证。
- 服务对象：`.scratch/vegas-speed-line/map.md` Q6（数据关系策略）与 `03-*`（原型票）

---

## 0. 结论速览（TL;DR）

- **原生模型不能直接表示 Vegas 式"自由倾斜速度线"（段内连续变速/速度线性渐变）**。5.2.1 的 Retiming 是**"分段恒定速度 + 段间过渡"**模型：任意两个相邻 key 之间速度恒定（映射是线性插值），速度只能在 key 处跳变或经"速度过渡"（transition，切线圆弧）平滑拐角。
- 5.x 数据模型相比 4.x 有重大变化：key 内部用 `retiming_factor`（0–1，归一化**源**位置）＋ `strip_frame_index`（double，**时间轴**锚点）两个量，但 **RNA 只暴露 `timeline_frame`（int）一个可读写属性** + `remove()` 方法；**源位置（retiming_factor）与段速度均不可从 Python 读出**。
- key 集合**没有 `add()/new()`**（实测调用无效果）；建 key 必须走 `bpy.ops.sequencer.retiming_key_add(timeline_frame=N)`（作用于活动条带/选中条带）。
- **负速度：Retiming 不支持**（`retiming_segment_speed_set.speed` RNA 下限 0.001%；key 之间禁止交叉，`retiming_key_frame_set` 有 clamp）。反向播放另有通道：旧式 Speed Control 效果条带的 Multiply 模式自 5.1 起支持负值。
- **音频条带支持 Retiming**（SoundStrip 有 `retiming_keys`；内部用逐帧 pitch / time-stretch 实现变速）。
- 未调整条带默认 **0 个 key**；首次加 key 时自动补左右两个端点 key（共 3 个）。"清空/重置"= `bpy.ops.sequencer.retiming_reset()`。
- 对插件数据策略的影响：**沿用 map 已定决策"重置重建"**——插件自持 Vegas 式速度模型（每点速度），需要"倾斜/渐变"时把原生模型近似为"多段恒定速度＋transition"，需要"等速"时直接缩放 key 的 `timeline_frame`。因为段速度读不出来，**插件必须自建影子模型，不能依赖读回原生数据**。

---

## 1. Q(a) retiming keys 的确切数据结构

### 1.1 挂在哪个属性上（5.2.1 实测）

- 条带基类 `bpy.types.Strip`（4.x 叫 `Sequence`，**5.x 已改名，`bpy.types.Sequence` 不存在**）。
- `retiming_keys` 集合**只注册在 4 类条带上**（rna_sequencer.cc 源码确认 + 本机 `bl_rna` 实测一致）：
  - `ImageStrip`、`MovieStrip`、`SceneStrip`、`SoundStrip`。
  - C 层 `retiming_is_allowed()` 还允许 `MetaStrip`/`MovieClipStrip`/`MaskStrip`（及 `strip->len > 1`），但**这 3 类没有 RNA 集合 → Python 拿不到 retiming 数据**（UI 里仍可用）。
- 显示开关：`Strip.show_retiming_keys`（bool，默认 False；官方 API 文档："Show retiming keys, so they can be moved"）。
- 选择状态：`SequenceEditor.selected_retiming_keys`（bool，**只读**，实测赋值报错）；key 的选中标志 `SEQ_KEY_SELECTED` 是 C 侧 flag，**RNA 未暴露** → Python 无法直接选中 key（key 选择/删除 op 主要面向 UI 交互）。

### 1.2 key 的字段（官方源码 DNA，`v5.2.1` `DNA_sequence_types.h`）

```c
struct SeqRetimingKey {
  double strip_frame_index = 0;   /* 时间轴锚点（以"源媒体帧"为单位，见下） */
  eSeqRetimingKeyFlag flag;       /* TRANSITION_IN/OUT、FREEZE_FRAME_IN/OUT、SELECTED */
  float retiming_factor = 0;      /* 0-1，归一化到原始内容范围 → 源位置 */
  double original_strip_frame_index = 0; /* 仅 transition 用：原 key 的时间轴位置 */
  float original_retiming_factor = 0;    /* 仅 transition 用：原 key 的源位置 */
};
```

语义（由 `sequencer/intern/strip_retiming.cc` 推导，5.2.1）：

- **源位置** = `retiming_factor × strip.len`（源帧）；key 0 固定源 0，末 key 固定源 `strip.len`。
- **时间轴位置** = `timeline_frame = content_start + sound_offset + strip_frame_index / media_playback_rate_factor`（内部函数 `retiming_key_frame_get`）。
- RNA 的 `key.timeline_frame`（官方 API 文档：int，"Position of retiming key in timeline"）就是上面这个值（RNA getter 简化为 `content_start + strip_frame_index`，对 image/sound 与内部公式一致；对 fps 与场景不同的 movie 条带两者有出入——插件读写 movie 条带时注意）。

### 1.3 相邻两 key 如何决定该段速度

```cpp
/* strip_retiming.cc: retiming_key_speed_get() */
speed = (segment_content_frame_count) / (segment_length)
      = (round(factor[i]*len) - round(factor[i-1]*len))
        / (strip_frame_index[i] - strip_frame_index[i-1])
```

即 **段速度 = 源帧增量 ÷ 时间轴增量**（`1.0` = 100% 原速）。渲染时同一段内映射是**线性插值**（`strip_retiming_evaluate`：`factor = factor_start + step * (frame - frame_start)`，step 段内恒定）。

---

## 2. Q(b) 一段之内速度是否恒定？能否表达"段内变速"？

**恒定。段内速度不可变，原生模型 = 分段恒定速度 + 段间过渡。**

- 段内映射为直线（线性插值），速度 = 常数。
- 速度变化的唯一途径：
  1. **key 处跳变**（两条斜率不同的直线在 key 处相接，形成尖角）；
  2. **速度过渡（transition）**：`retiming_transition_add` 把一个 key 拆成一对 key（`TRANSITION_IN`+`TRANSITION_OUT`），中间用**与相邻两线段相切的圆弧**（`strip_retiming_evaluate_arc_segment`，两直线间求切线圆）平滑连接；若相邻两段速度几乎相同（差 < 0.01）则退化为直线。
- 因此：
  - **不能表示 Vegas 式"一段内速度线性渐变"（倾斜的直线速度线）**——原生"段"永远等速；Vegas 的 ramp 在原生里只能近似为"多段等速＋transition"，且过渡形状由切线圆唯一确定（无缓动类型可选，`original_*` 字段仅存原 key 位置）。
  - "过渡"本身在过渡窗口内速度是连续变化的（圆弧导数），这是原生最接近"渐变"的形态。
- 补充：**速度过渡/冻结帧不能叠加**（`retiming_add_transition` 对 FREEZE 或已有 TRANSITION 的 key 返回 nullptr；`retiming_add_freeze_frame` 对 transition key 返回 nullptr）；transition 不能建在首/末 key 上。

---

## 3. Q(c) key 数量限制与新增/移动/删除

### 数量限制
- **源码中无硬性上限**（数组动态 realloc，`MEM_new_array` 逐次扩容；仅受内存与 `int` 帧范围限制）。实测尝试 `new()` 无效（见下），故未做万级实测；4.x 亦无此限制。

### 新增（Python 只能走 operator）
- `bpy.ops.sequencer.retiming_key_add(timeline_frame=0)`（官方 API 文档确认；源码：给**所有选中条带**或活动条带加 key；未传 `timeline_frame` 时用当前播放帧）。
- 集合 **无 `add()/new()`**：实测 `strip.retiming_keys.new()` → `attribute "new" not found`；`RetimingKeys` 集合的 collection_funcs 没有 new/remove 回调（rna_sequencer.cc），只有 begin/next/end/get/length。
- 冻结帧/过渡的追加：`retiming_freeze_frame_add(duration)`、`retiming_transition_add(duration)`（duration 默认 1，实为"在原 key 两侧各偏移 duration 帧"的语义，见 7 对照表）。

### 移动（可直接赋值）
- `key.timeline_frame = N`（int，可写；官方 API 文档 + RNA setter `rna_Strip_retiming_key_frame_set` 确认）。
- 赋值经 `retiming_key_frame_set` 处理，**三种特殊行为**：
  1. 中间 key：只动自己，且**被 clamp 在相邻 key 之间（±1 帧），禁止交叉**（`strip_retiming_clamp_offset`）；
  2. 末 key（`frame_old == right_handle`）：连同其右侧所有 key 一起平移 → **改变条带时长**（拖整条速度线 = 动末 key）；
  3. 首 key（`frame_old == left_handle` 或源 0）：平移条带 `start` + 其余 key 反向平移 → **改变条带位置/裁切**。
- transition 的 OUT key 直接赋值被忽略（源码 `retiming_key_frame_set` 对 `SEQ_SPEED_TRANSITION_OUT` 直接 return）。
- UI 中移动 key 是 G 拖拽 / `transform.seq_slide`（模态），Python 直接赋值即等价。

### 删除
- `bpy.ops.sequencer.retiming_key_delete()` —— 但**要求 key 已选中**（`retiming_keys_are_selected`），而 key 选中状态 RNA 不可写 → 该 op 对纯 Python 脚本基本不可用（留给 UI 或插件内模拟选择）。
- **推荐：`strip.retiming_keys[i].remove()`**（官方 API 文档确认 `RetimingKey.remove()`；底层 `rna_Strip_retiming_key_remove` → `retiming_remove_key`）。首 key（源 0）与末 key 不可删（C 层直接 return），删除会连带清理 freeze/transition 标志。

---

## 4. Q(d) 负速度与速度范围

- **Retiming 不支持负速度（反向播放）**：
  - `bpy.ops.sequencer.retiming_segment_speed_set(speed=...)` 的 RNA 定义：`min=0.001`、`max=FLT_MAX`、默认 `100.0`（百分比，100 = 1x）——实测内省 + 官方 API 文档一致；负数直接 RNA 校验失败。
  - 结构层面也不可能：key 间禁止交叉（见 3），故时间轴增量恒 > 0、源增量恒 > 0 → 段速度恒 > 0。
- **速度下限**：operator 层面 0.001%（≈1e-5x）；**真正 0 速**用"冻结帧"（freeze frame：一对源位置相同的 key，段速=0）。
- **速度上限**：无（FLT_MAX）；直接改 `timeline_frame` 也仅受相邻 key 位置限制，无速度上限校验。
- 反向播放的替代通道（注意与 Retiming 是两套机制）：**旧式 Speed Control 效果条带**（`SpeedControlStrip`，效果条带）自 **5.1 起 Multiply 模式允许负值**（官方 5.1 发布说明 PR#152982）。但效果条带本身**不能**被 retime（`retiming_is_allowed` 排除 effect）。

---

## 5. Q(e) 音频条带是否支持 Retiming

**支持。**
- `SoundStrip` 注册了 `retiming_keys`（本机 `bl_rna` 实测 + rna_sequencer.cc 第 3674 行确认），`retiming_is_allowed` 含 `STRIP_TYPE_SOUND`。
- 实现：`retiming_sound_animation_data_set()` 把每段速度写为音频 pitch（逐帧 `speed_table`，transition 段逐帧变速），若开启 `Strip.pitch_correction`（Preserve Pitch，5.2 实测属性存在）则走 time-stretch 保持音高。**变速会让音高变化**（默认不保音高），与 Vegas 行为不同，插件若处理音频需提示用户。

---

## 6. Q(f) 默认 key 数量与"清空/重置"

- **未调整的条带：0 个 key**（本机实测：新建 image/sound 条带 `len(strip.retiming_keys) == 0`）。
- 首次执行任何 retiming 操作时自动补左右端点 key（`retiming_data_ensure` 建 2 个：源 0 / 源 len；`ensure_left_and_right_keys` 再落到位）→ 加 1 个中间 key 后共 **3 个**。
- **清空/重置**：`bpy.ops.sequencer.retiming_reset()`（官方 API 文档："Reset strip retiming"）→ `retiming_data_clear()` 把 `retiming_keys_num` 归 0，条带恢复 1x 原速、时长复原。这是"清空 Retiming"的唯一正规操作（C 层）；Python 等价物：`for k in list(...): k.remove()` 无法删干净首末 key，**不推荐**，直接用 `retiming_reset`。

---

## 7. Q(g) 与 Vegas 模型逐项对照

Vegas "速度线"模型：横轴时间、纵轴速度百分比；速度点在线上，相邻点之间速度**线性渐变**（可 smooth/linear 模式）；拖整条线 = 整段等速。

| Vegas 操作 | Blender 原生 Retiming 对应 | 差异/备注 |
|---|---|---|
| 等速拖线（整段统一变速） | `retiming_segment_speed_set(speed=…)`（strip 模式）；或直接改末 key `timeline_frame`（缩放段长） | **警告**：strip 模式下该 op 先 `retiming_reset` 清掉所有已有 key（源码 `strip_speed_set` 注释明说"overwrite existing keys"；5.1 已移除 `keep_retiming` 参数，官方发布说明 PR#153509）。想保留 key 必须走"移动 key"路线。 |
| 插入速度点 | `retiming_key_add(timeline_frame=N)` | key 落在当前映射线上（源位置=该处求值），即 Vegas"在线上插点"。 |
| 拖点 → 段间渐变（速度从 A 平滑到 B） | ① 移动中间 key：`key.timeline_frame = N` → 只改相邻两段速度（**跳变**，非渐变）；② `retiming_transition_add(duration=N)` → 圆弧平滑过渡 | Vegas ramp 是速度的直线渐变；原生 transition 是**切线圆弧**（固定形状，无缓动选择），两者形状不同。长距离渐变只能"多段等速 + 多个 transition"近似。 |
| 冻结（0 速） | `retiming_freeze_frame_add(duration=N)`（或 `retiming_add_freeze_frame_slide`） | 一对同源 key。 |
| 反向播放 | **无**（Retiming 不支持）；另有 Speed Control 效果条带 Multiply 负值（5.1+） | 两套机制互斥。 |
| 删除速度点 | `key.remove()` / `retiming_key_delete()`（需选中） | 首末 key 不可删。 |
| 重置全部 | `retiming_reset()` | 归 0 key，1x。 |
| 撤销/重做 | 各 op 均 `OPTYPE_REGISTER \| OPTYPE_UNDO`，直接赋值 `timeline_frame` 也进撤销栈（property set 有 undo 记录） | 与 03 原型票的 modal undo 策略配合。 |

---

## 8. 对插件数据策略的影响（供 map Q6）

1. **能力边界结论句**：原生 Retiming 数据模型**不能**直接表示 Vegas 式自由倾斜速度线（段内连续变速）；它只能表达"每段恒定速度 + 段间（圆弧/线性）过渡"，Vegas 的"倾斜/渐变"只能近似为"多段等速 + transition"。
2. 因此插件 v1 范围（等速 + 插点 + 拖点渐变）映射为：
   - 等速 → 直接写 `timeline_frame`（缩放 key 位置），**不要**用 strip 模式 `retiming_segment_speed_set`（会清 key）；
   - 插点 → `retiming_key_add(timeline_frame=N)`；
   - 拖点渐变 → 原生给"跳变"（移动 key）；若要"平滑"，插件自己生成 `retiming_transition_add` 或自建多段近似（v1 可先只做跳变，与 map "平滑延后"一致）。
3. **必须自建影子模型**：段速度与源位置 RNA 均不可读（只有 `timeline_frame`），读回原生数据无法重建 Vegas 速度线 → 沿用 map 已定"已有原生 keys 重置重建"策略，插件内存中维护自己的（时间,速度）点表，写盘只写 `timeline_frame`。
4. 音频：SoundStrip 可 retime，但默认变速会变调（`pitch_correction` 可选保音高）——插件若处理音频要暴露该开关。
5. 反向播放不在 Retiming 能力内（v1 不做；若将来需要，走 Speed Control 效果条带或 C++ 扩展）。

---

## 来源

**主来源（primary）：**
- 本机 Blender 5.2.1 LTS 二进制内省（`--background --python` 实测：`bpy.types`、`bl_rna.properties`、`bpy.ops.sequencer.*` 参数、live 条带 `retiming_keys` 长度/集合方法、`selected_retiming_keys` 只读性、`pitch_correction` 存在性等）。后台模式无法调用需 area 的 sequencer ops（5.x 移除 dict override；`bpy.context.temp_override` 亦未能通过其 poll），故行为结论以源码为准。
- Blender 官方源码 `v5.2.1` tag（GitHub mirror）：
  - `source/blender/makesdna/DNA_sequence_types.h`（SeqRetimingKey 结构/flag）
  - `source/blender/sequencer/intern/strip_retiming.cc`（段速度公式、evaluate 线性/圆弧、frame_set clamp、freeze/transition、音频 speed_table）
  - `source/blender/sequencer/SEQ_retiming.hh`（C API 面）
  - `source/blender/editors/space_sequencer/sequencer_retiming.cc`（op 注册与参数、poll=scene ed + act_strip + retiming_is_allowed）
  - `source/blender/makesrna/intern/rna_sequencer.cc`（RetimingKey RNA=timeline_frame+remove()；retiming_keys 注册于 Image/Movie/Scene/SoundStrip）
- 官方 Python API 文档（docs.blender.org/api/5.2）：`bpy.types.RetimingKey`（timeline_frame / remove）、`bpy.ops.sequencer.retiming_*`（参数与范围）、`bpy.types.Strip.show_retiming_keys`。
- 官方发布说明（developer.blender.org）：5.1 Sequencer —— Speed Control 负值（PR#152982）、retiming key 隐藏图标（PR#153683）、`retiming_segment_speed_set` 移除 `keep_retiming`（PR#153509）、条带时间属性改名表（PR#153012，旧名 6.0 移除）。

**辅助（manual/社区佐证）：**
- 官方手册中文镜像（bookstack.cn，源自 docs.blender.org/manual/zh-hans 5.0）"编辑 › 重定时关键帧"节：移动 key（拖拽或 G）、设置速度（速率 = 相对原始时间）、"保持当前重定时"（5.0 手册；5.1 已移除，见 PR#153509）。
- 社区：Blender 5.1/5.2 新闻报道（slashcam、blender.jp 译稿）对 retiming 图标/负速度的转述，与官方发布说明一致。
