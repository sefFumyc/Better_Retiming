# VSE 绘制与交互技术路径调研（Blender 5.2.1）

- 调研日期：2026-09-14
- 目标环境：`D:\Tools\blender-5.2.1-windows-x64\blender.exe`（5.2.1 LTS，经 `--background --python` 实测内省确认）
- 方法：本机 Blender 5.2.1 二进制内省（第一优先）＋ 官方文档（docs.blender.org / developer.blender.org）＋ Blender C 源码（GitHub main）＋ 社区开源插件
- 结论将供 `.scratch/vegas-speed-line/issues/03-*`（原型票）使用

---

## 0. 结论速览（TL;DR）

- **绘制**：纯 Python 可行。用 `bpy.types.SpaceSequenceEditor.draw_handler_add(callback, args, 'WINDOW', 'POST_PIXEL')` 注册持久 draw handler，回调里用 `gpu` 模块（`gpu.shader.from_builtin` + `gpu_extras.batch.batch_for_shader` + `gpu.state.blend_set`）画速度线，`blf` 画 HUD 文字。**bgl 已在 5.x 移除**（`import bgl` → ModuleNotFoundError），必须用 gpu。
- **坐标换算**：`region.view2d.region_to_view(x, y)` / `view_to_region(x, y, clip=True)`（官方 API，5.2.1 存在）。VSE 时间轴里 view 坐标 x=时间轴帧、y=通道（浮点）。条带屏幕矩形需自己算（条带是即时模式渲染，无矩形 API）。
- **交互**：modal operator（事件 → view2d → 帧/通道 → 命中条带/线段）。注意：**5.2.1 里 `Region.tag_redraw()` / `Area.tag_redraw()` 已从 Python API 移除**（实测所有 bpy.types 均无），刷新需用 `bpy.ops.wm.redraw_timer(type='DRAW_WIN_SWAP', iterations=1)`（5.2.1 实测存在）或走 gizmo（自动重绘）。
- **gizmo**：`GizmoGroup` 挂 `bl_space_type='SEQUENCE_EDITOR'` **注册成功（已实测）**；SSE 也有 `show_gizmo*` 开关属性。gizmo 路线自动重绘、自带悬停/模态，但"sequencer 区域是否真把 gizmo 画出来"需原型验证（注册成功 ≠ 一定渲染）。
- **原生 retiming ops（5.2.1 实测名/参数）**：`retiming_key_add(timeline_frame)`、`retiming_key_delete()`、`retiming_reset()`、`retiming_show()`、`retiming_segment_speed_set(speed)`、`retiming_transition_add(duration)`、`retiming_freeze_frame_add(duration)`、`retiming_add_freeze_frame_slide`、`retiming_add_transition_slide`。它们作用于**选中条带**（`retiming_key_add` 给所有选中条带加键，C 源码确认），不依赖光标。
- **关键约束（影响插件设计）**：`RetimingKey` 的 RNA 只暴露 `timeline_frame`（int）和 `remove()`；**段速度（内部 `strip_frame`）不暴露**，`retiming_keys.add()` 在 5.2.1 是空操作（实测 len 不变）→ 建键必须走 `retiming_key_add` op，段速度只能通过 `retiming_segment_speed_set(speed)` 写，读不出来 → 插件需自建速度模型（与 map.md 的"重置重建"决策吻合）。
- **5.x 大改名**：`Sequence` → `Strip` 系（`bpy.types.Sequence` 已不存在，基类是 `bpy.types.Strip`）；`sequence_editor.sequences` → `sequence_editor.strips`；`frame_final_start/end` → `left_handle/right_handle`，`frame_start` → `content_start` 等（旧名 6.0 移除）。

---

## 1. 环境与内省方法

用 `--background --python-expr/--python` 对 5.2.1 实测（PowerShell 中路径带空格需加引号）：

```
& "D:\Tools\blender-5.2.1-windows-x64\blender.exe" --background --python-expr "import bpy; print(bpy.app.version_string)"
# → 5.2.1 LTS (hash 9e2066aef7ef built 2026-08-25)
```

内省得到的关键类型：`RetimingKey`、`RetimingKeys`、`Strip` 系（`MovieStrip/ImageStrip/SoundStrip/SceneStrip/EffectStrip/MetaStrip/…`）、`SequenceEditor`、`SpaceSequenceEditor`、`SequencerTimelineOverlay`、`SequencerToolSettings`。

---

## 2. Q(a) 在 VSE 条带上绘制自定义 overlay

### 2.1 draw handler（可行，推荐）
- API（实测存在）：`bpy.types.SpaceSequenceEditor.draw_handler_add(callback, args, region_type, draw_type)` → 返回 handle；`draw_handler_remove(handle, region_type)` 卸载。
  - `region_type='WINDOW'`；`draw_type='POST_PIXEL'`（区域像素坐标，最常用）或 `'POST_VIEW'`（view 坐标）。
  - 官方 docstring（5.2.1 实测）："Add a new draw handler to this space type. It will be called every time the specified region in the space type will be drawn."
- 典型写法（社区实证，BlenderArtists penduin 线程）：`bpy.types.SpaceSequenceEditor.draw_handler_add(self.draw_cb, (context,), 'WINDOW', 'POST_PIXEL')`；回调里 `region = context.region; v2d = region.view2d` 取屏幕矩形再画。
- 原生重定时覆盖层的 C 实现参考：`draw_retiming_overlay`（`source/blender/editors/space_sequencer/sequencer_view.cc`）——Blender 自己就是这么在条带上画 retiming keys/速度曲线的，纯 Python 可复刻同样思路。

### 2.2 gpu module（bgl 已废弃/移除）
- **bgl：已移除**。5.2.1 实测 `import bgl` → `ModuleNotFoundError: No module named 'bgl'`。老插件（如 VSE_Transform_Tools）的 `bgl.glBegin/glVertex2f` 全部失效，必须改写。
- gpu module（5.2.1 实测齐全）：
  - `gpu.types`：`GPUShader`、`GPUBatch`、`GPUOffScreen`、`GPUFrameBuffer`、`GPUTexture`、`GPUIndexBuf`、`GPUVertBuf`、`GPUVertFormat`…
  - `gpu.shader.from_builtin(...)`：`'2D_UNIFORM_COLOR'`、`'UNIFORM_COLOR'`、`'POLYLINE_UNIFORM_COLOR'`、`'IMAGE'`、`'SMOOTH_COLOR'` 等
  - `gpu.state`：`blend_set`、`line_width_set`、`depth_test_set`、`viewport_set` 等（实测列出）
  - `gpu_extras.batch.batch_for_shader(...)`（实测 OK）；`gpu_extras.presets.draw_circle_2d / draw_texture_2d`（实测 OK）
  - `blf` 模块（画文字/HUD）：`position/size/color/draw/dimensions`（实测 OK）
- 画速度线的推荐原语：`'2D_UNIFORM_COLOR'` 顶点着色器 + `batch_for_shader(shader, 'LINE_STRIP' 或 'TRIANGLE_FAN', {"pos": [...]})`，颜色用 `shader.uniform_float("color", (r,g,b,a))`；折线加 `gpu.state.line_width_set(w)`（画线宽需 2.x+ 某些平台限制，原型阶段验证）。

### 2.3 结论
用 **`SpaceSequenceEditor` draw handler（POST_PIXEL） + gpu module + blf** 在 VSE 时间轴条带上绘制自定义 overlay 完全可行，bgl 不可再用。

---

## 3. Q(b) 视图坐标换算（鼠标 → 条带/时间）

- 官方 API（`docs.blender.org/api/current/bpy.types.View2D.html`，5.2.1 实测 `region.view2d` 存在）：
  - `region.view2d.region_to_view(x, y) → (float, float)`：区域像素坐标 → view 坐标。VSE 时间轴中 view 坐标 x = 时间轴帧（float），y = 通道（float，整数通道落在整数带内）。
  - `region.view2d.view_to_region(x, y, clip=True) → (int, int)`：view 坐标 → 区域像素坐标。
- 鼠标坐标：modal 事件用 `event.mouse_region_x / mouse_region_y`（相对区域）。
- **条带屏幕矩形需自己算**（社区实证 iceythe 回复："Strips… are immediate mode rendered - there are no rectangles… you would have to compute the view2d positions yourself"）：
  - 条带时间范围（5.x 新属性，见 §6）：`strip.left_handle`（=旧 frame_final_start）～ `strip.right_handle`（=旧 frame_final_end）；通道 `strip.channel`。
  - 示例：`x1, y1 = v2d.view_to_region(strip.left_handle, strip.channel); x2, y2 = v2d.view_to_region(strip.right_handle, strip.channel + 1)` 得条带像素矩形（y 用 channel 与 channel+1 两个整数边界，即通道条带高度 1 个单位）。
  - 反算：`frame = v2d.region_to_view(mx, my)[0]` 得鼠标下的时间帧；`chan_float = v2d.region_to_view(mx, my)[1]`，`int(chan_float) ` 即通道。
- ⚠️ `bpy_extras.view2d` 模块在 5.2.1 **已移除**（实测 `ModuleNotFoundError`），不要再 import；直接用 `region.view2d` 方法。

---

## 4. Q(c) 鼠标交互：modal operator、刷新时机、strip 命中

### 4.1 modal operator 写法（标准模式，社区大量实证）
```
class SEQVEL_OT_speed_line(bpy.types.Operator):
    def invoke(self, context, event):
        # 记录起始状态；context.window_manager.modal_handler_add(self)
        # return {'RUNNING_MODAL'}
    def modal(self, context, event):
        # event.type in {'MOUSEMOVE','LEFTMOUSE','RIGHTMOUSE','ESC','RET'...}
        # 每帧：换算坐标 → 命中检测 → 改数据 → 触发重绘
```
- 键盘映射：在 `SEQUENCE_EDITOR` 的 keymap 上注册（`bpy.context.window_manager.keyconfigs.addon.keymaps.new(name='Sequencer', space_type='SEQUENCE_EDITOR', region_type='WINDOW')`），或注册为一个 tool（Tool + operator + keymap）。
- 命中哪个 strip：
  - 自算（推荐，可控）：遍历 `scene.sequence_editor.strips`，检查 `region_to_view(mx,my)` 的帧是否在 `[left_handle, right_handle]`、通道是否等于 `int(chan_float)`（注意通道方向：VSE 通道 1 在底部）。
  - 或调用原生命中 op：`bpy.ops.sequencer.select(mouse_x=..., mouse_y=..., wait_to_deselect_others=False, deselect_all=True)`（5.2.1 实测该 op 有 `mouse_x/mouse_y` 参数）——但会改变选择状态，需要取舍。
- 命中"速度线上的段/点"：用自建速度模型（§6.4）算出每条线段/每个 key 的屏幕坐标，做点-线距离/点-点距离判定。

### 4.2 ⚠️ 刷新时机（5.x 重要变化）
- **勘误（实现阶段实测）**：本节此前"`Region.tag_redraw` / `Area.tag_redraw` 已从 Python API 移除"的结论**是假的负**——`hasattr` / `dir()` 查不到 RNA 函数，但 `Area.bl_rna.functions` 里明确有 `tag_redraw` 与 `header_text_set`（原型未加保护直接调用 `context.area.header_text_set(...)` 且实测可用，即是佐证）。因此正确做法是"**先试 `area.tag_redraw()`，异常再退回 `wm.redraw_timer`**"（v1 的 `overlay.redraw` 就是这么写的）。教训：判断 5.x 是否移除某个 API，查 `bl_rna.functions` / `bl_rna.properties`，不要只信 `hasattr`。
- **实测（5.2.1，内省全部 bpy.types）：`Region.tag_redraw` / `Area.tag_redraw` / `tag_refresh_ui` / `tag_redraw_ui` 全部不存在**（`hasattr` False、`dir` 无、全类型扫描 0 命中）。
  - 官方文档页（api/current）仍列出 `Region.tag_redraw()` —— 文档滞后于 5.x 实际构建；以二进制内省为准。
  - 大量 4.x 插件（含 2026 年的 POW! gist）仍用 `context.area.tag_redraw()`，到 5.x 会报 AttributeError——这是 5.x 的破坏性变更，社区还没跟上。
- **5.2.1 可用的刷新手段（实测）**：
  1. `bpy.ops.wm.redraw_timer(type='DRAW_WIN_SWAP', iterations=1)` —— 实测存在，强制一次重绘；社区公认"hack 但可用"（Blender Gotchas 亦提及）。
  2. `bpy.app.timers`（实测存在）—— 配合 `wm.redraw_timer` 或属性变更使用。
  3. Gizmo 模态交互 —— 事件驱动自动重绘，**无需手动刷新**（这是 gizmo 路线的最大优势）。
- 推荐：写一个兼容 helper：`if hasattr(context.area, 'tag_redraw'): context.area.tag_redraw() else: bpy.ops.wm.redraw_timer(type='DRAW_WIN_SWAP', iterations=1)`，兼顾旧版与 5.2.1。

---

## 5. Q(d) 5.2.1 的 sequencer 是否支持 gizmo

- **实测：`GizmoGroup` 挂 `bl_space_type='SEQUENCE_EDITOR'` + `bl_region_type='WINDOW'` 注册成功**（`bpy.utils.register_class` 无报错；第一次失败只是因为 Gizmo 子类没写 `draw()` 方法，补上后注册通过）。
- `SpaceSequenceEditor` 有 `show_gizmo`、`show_gizmo_navigate`、`show_gizmo_context`、`show_gizmo_tool` 四个 RNA 属性（默认 True，实测）——说明该空间带 gizmo 显示开关，具备 gizmo 基础设施。
- `GizmoGroup` 官方文档（api/current）：`bl_space_type`、`bl_region_type`、`bl_options`（含 `SCALE`、`SELECT`、`PERSISTENT`、`SHOW_MODAL_ALL`…）；`Gizmo` 有 `draw()`、`draw_prepare()`、`modal()` 等。
- 未发现官方/社区的 sequencer gizmo 现成例子（原生 retiming UI 是 C 代码直接绘制+处理，不是 gizmo）；gizmo 在 2D 空间（Graph/Sequencer）的**实际渲染行为需原型验证**：注册成功 ≠ sequencer 区域一定把 gizmo 画出来。若原型确认能渲染，gizmo 路线是最优解（自动重绘 + 自带悬停高亮 + modal 拖拽）。

---

## 6. Q(e) 原生 Retiming operators 的确切名称/参数（5.2.1 实测）+ 数据模型

### 6.1 operators（`bpy.ops.sequencer.*`，5.2.1 实测）
| op | 参数（实测） | 语义（手册/C 源码确认） |
|---|---|---|
| `retiming_key_add` | `timeline_frame`(int, 默认=当前帧) | 给**所有选中条带**在指定帧加 retiming key；自动补首/尾 key；自动置 `show_retiming_keys`。不依赖光标（C 源码 `selected_strips_from_context`） |
| `retiming_key_delete` | 无 | 删除**选中的 retiming keys**（需先选中 key；选 key 的机制非 RNA，或直接用 `key.remove()`） |
| `retiming_reset` | 无 | 重置选中条带 retiming |
| `retiming_show` | 无 | 切换选中条带 `SEQ_SHOW_RETIMING`（= `strip.show_retiming_keys`） |
| `retiming_segment_speed_set` | `speed`(float) | 设置某个重定时段的播放速度（相对原始速度的倍率）。4.x 曾有 `keep_retiming` 参数，**5.1 已移除**（PR#153509，实测 5.2.1 只剩 `speed`）。目标段判定（播放头 vs 光标）需原型验证 |
| `retiming_transition_add` | `duration`(int) | 在选中的 key 处创建平滑速度过渡（2 个联动 key） |
| `retiming_freeze_frame_add` | `duration`(int) | 冻结帧 |
| `retiming_add_freeze_frame_slide` | 宏 | = `retiming_freeze_frame_add` + `TRANSFORM_OT_seq_slide` |
| `retiming_add_transition_slide` | 宏 | = `retiming_transition_add` + `TRANSFORM_OT_seq_slide` |

- 所有 retiming op 的 poll：需要 sequencer 编辑上下文 + `ed.act_strip` 存在 + `retiming_is_allowed(strip)`（C 源码 `retiming_poll`；Effect strip 不允许）。**headless 下 poll 失败**（实测 `poll() failed, context is incorrect`），必须在 UI 上下文（modal/工具）内调用。
- 原生快捷键（grep.app 源码）：I（加 key 菜单）、Shift+R（设速度）、Ctrl+R（显示/隐藏 keys）、Shift+Ctrl+R（5.x）。

### 6.2 数据模型（5.2.1 实测）
- 只有 `MovieStrip`、`ImageStrip`、`SceneStrip`、`SoundStrip` 有 `retiming_keys` 集合（基类 `Strip` 只有 `show_retiming_keys`）。
- `RetimingKey` RNA 只暴露：`timeline_frame`(int) + `remove()`（官方文档确认）。**段速度内部数据（`strip_frame`）不暴露**。
- **`strip.retiming_keys.add()` 在 5.2.1 是空操作**（实测：len 保持 0，返回 None，`show_retiming_keys=True` 也没用）→ 建 key 只能走 `retiming_key_add` op。
- `SequenceEditor.selected_retiming_keys`（只读，当前选中的 key 集合）。
- 结论：**"直接读写原生 Retiming 数据"在 RNA 层面只能做到：读 key 的 `timeline_frame` + 删 key（`key.remove()` 或选中后 `retiming_key_delete`）；建 key 与设速度必须走 ops。** 插件需要自己的速度/键位模型来驱动绘制（map.md 已有"重置重建"决策，正好自洽：重置后插件创建的每个 key 都是自己"认识"的，速度也是自己设的）。

### 6.3 5.x 条带时间属性改名（5.1 发布说明 PR#153012，6.0 移除旧名；实测 5.2.1 旧名已发 DeprecationWarning）
- `frame_final_start` → `left_handle`；`frame_final_end` → `right_handle`
- `frame_start` → `content_start`；`frame_duration` → `content_duration`（新增只读 `content_end`）
- `frame_offset_start/end` → `left_handle_offset/right_handle_offset`
- `animation_offset_start/end` → `content_trim_start/content_trim_end`
- `frame_final_duration` → `duration`
- 集合改名：`sequence_editor.sequences` → `sequence_editor.strips`（另有 `strips_all` 含 meta 嵌套）；基类 `bpy.types.Sequence` 不存在了，新基类是 `bpy.types.Strip`。

---

## 7. Q(f) 可借鉴的开源项目

1. **VSE Transform Tools**（`github.com/kgeogeo/VSE_Transform_Tools`，单文件 VSE_Transform_Tool.py）
   - 思路：VSE 预览区的模态变换（拖拽/旋转/缩放），**modal operator + draw_handler 叠加层**。绘制用旧 bgl（2.6–3.x 时代）——**bgl 已死，需照抄结构但换 gpu**。可借鉴：modal 状态机、按 `context.space_data.type == 'SEQUENCE_EDITOR'` 的 poll、draw callback 随鼠标更新。
2. **BlenderArtists "Sequencer feature revival: show all clips' start/end frames during grab"**（penduin / iceythe 回复，2023）
   - 思路：**现代 VSE 条带 overlay 的最简实证**——`SpaceSequenceEditor.draw_handler_add(cb, (context,), 'WINDOW', 'POST_PIXEL')` + `region.view2d.region_to_view`/`view_to_region` 手算条带屏幕位置 + `blf` 画数字。直接套用其坐标换算公式（含通道 y 定位）。局限：检测"正在拖条带"只能 hack C 层（ctypes）——我们的插件用自建 modal 就不需要这个 hack。
3. **POW! addon**（Kurakurage-jp gist，2026-01；View3D 但模式通用）
   - 思路：标准 GPU overlay 管线——`gpu.shader.from_builtin('IMAGE')`、`gpu_extras.batch.batch_for_shader`、`gpu.state.blend_set('ALPHA')`、draw_handler_add('WINDOW','POST_PIXEL')、`bpy.app.timers` 定时清理 handler。可借鉴 1:1 到 VSE。
4. **VSE+（Mumu Mundo，免费商业插件）**：主打"Disconnect and Retime"——一键对选中条带断开音视频并加 retiming keys。功能参考（我们的"重置重建"也属于这类自动化），闭源，无实现可抄。
5. **Power Sequencer**（GDQuest/blender-power-sequencer）：最大开源 VSE 插件。不画自定义 overlay，但 modal operator 组织、keymap 注册、VSE 自动化模式值得参考。
6. 对照组（**不建议**）：Speed Control 效果条带方案（如 `srivastavaarpit121/blender-vse-speed-adjust-addon`、BlenderMarket "Clip Speed"）——用 SPEED effect strip + `speed_factor`，不产生原生 retiming 数据，与"直接走原生 Retiming 管线"的目标冲突。

---

## 8. 技术路线建议（供 03 原型票）

### 推荐路线 A（主）：持久 draw handler（视觉）+ modal operator（交互）
- 结构：
  - **绘制**：启用插件时注册一个 `SpaceSequenceEditor.draw_handler_add(cb, (context,), 'WINDOW', 'POST_PIXEL')`。cb 内：遍历选中条带（`scene.sequence_editor.strips` 中 `select` 的），用 `v2d.view_to_region(left_handle, channel)` / `(right_handle, channel+1)` 得条带矩形；用插件自建速度模型把每条段画成折线（Vegas 速度线样式，基线 = 条带高度中线）；key 画小菱形；`blf` 画速度百分比 HUD。
  - **交互**：注册一个 VSE keymap（`keymaps.new(name='Sequencer', space_type='SEQUENCE_EDITOR', region_type='WINDOW')`）绑定的 modal operator：
    1. `invoke`：记录选中条带；`modal_handler_add`。
    2. `modal`：`event.mouse_region_x/y` → `v2d.region_to_view` → (frame, channel)；命中检测（自算矩形 + 点-线距离）；操作分发：
       - 拖线上（改变某段速度）：选中条带 + `retiming_segment_speed_set(speed=换算出的目标倍率)`（原型阶段验证其"目标段"是播放头还是光标：若基于播放头，先 `scene.frame_set(frame)` 再把播放头放回）；同步更新自建模型。
       - 双击/快捷键插点：`retiming_key_add(timeline_frame=int(frame))`；把新 key 记入自建模型（初始速度=该点当前速度）。
       - 拖点（速度渐变）：同"拖线上"。
       - 删除点：`key.remove()`（或选中 key 后 `retiming_key_delete`）。
    3. 刷新：`hasattr(area,'tag_redraw')` 分支 / `bpy.ops.wm.redraw_timer(type='DRAW_WIN_SWAP', iterations=1)`（5.2.1）。
  - **数据一致性**：进入编辑（或"重置重建"）时先 `retiming_reset()`（选中条带），之后所有 key 均由插件创建并维护在自建模型中；`retiming_keys` 的 `timeline_frame` 用作与原生同步的校验。
- 优点：无 gizmo 渲染不确定性；draw handler + modal 是社区验证过的老套路；对条带矩形、命中、绘制完全可控。
- 风险：`retiming_segment_speed_set` 的目标段判定；刷新靠 `wm.redraw_timer` hack（功能可用，稍粗糙）。

### 备选路线 B：GizmoGroup('SEQUENCE_EDITOR')
- 结构：注册 `GizmoGroup(bl_space_type='SEQUENCE_EDITOR', bl_region_type='WINDOW')`，`setup/draw_prepare` 里为每个选中条带 `gizmos.new(MyGizmo)`；`Gizmo.draw()` 里用 gpu 画速度线；`Gizmo.modal()` 处理拖拽；选中条带变化时增删 gizmo。
- 优点：**交互期间自动重绘**（绕开 tag_redraw 问题）；自带悬停高亮与光标反馈。
- 风险：sequencer 区域是否实际渲染 gizmo 需原型验证（注册已实测通过）；gizmo 池管理略复杂。
- 建议：原型阶段先用 5 分钟验证"SEQUENCE_EDITOR 里 gizmo 是否可见"，可见则 B 为最终形态，否则退回 A。

### 原型（03 票）必验证清单
1. `SpaceSequenceEditor.draw_handler_add` 在 5.2.1 的绘制与条带矩形计算（§3 公式）。
2. `bpy.ops.wm.redraw_timer` 在 modal 中的刷新效果。
3. `retiming_segment_speed_set(speed)` 的目标段判定（播放头 or 鼠标位置 or 选中段），以及 `key.remove()` 是否可用。
4. Gizmo 在 SEQUENCE_EDITOR 是否真的渲染（决定路线 B）。
5. `retiming_key_add(timeline_frame)` 在模态内（选中条带状态下）的行为与 undo 表现。

---

## 9. 参考来源

- 本机 Blender 5.2.1 LTS 二进制内省（`--background --python`，多轮脚本，2026-09-14）：retiming 类型/ops/参数、RetimingKey RNA、Strip 属性改名、bgl/gpu/blf/gpu_extras、Region/Area 方法、GizmoGroup 注册实测、`wm.redraw_timer`、`bpy_extras.view2d` 移除等。
- 官方 Python API：`docs.blender.org/api/current/bpy.types.View2D.html`、`bpy.types.Region.html`（tag_redraw 文档滞后于 5.2.1 构建）、`bpy.types.RetimingKey.html`（仅 timeline_frame + remove()）、`bpy.types.GizmoGroup.html`、`gpu.html` / `gpu.types`。
- 官方手册（Retiming Keys 章节）：`docs.blender.org/manual/en/latest/video_editing/edit/montage/editing.html`
- 发布说明：`developer.blender.org/docs/release_notes/5.1/sequencer/`（属性改名 PR#153012、keep_retiming 移除 PR#153509、retiming 图标 PR#153683、Speed Control 负值 PR#152982）；`release_notes/5.1/python_api/`
- Blender C 源码（GitHub main）：`source/blender/editors/space_sequencer/sequencer_retiming.cc`（retiming_poll、retiming_key_add 作用于选中条带、retiming_show/reset 等）；`sequencer_view.cc`（draw_retiming_overlay，经 deepwiki.com/blender/blender/14.2-sequencer-ui-and-timeline 佐证）
- 社区：
  - BlenderArtists "Sequencer feature revival: show all clips' start/end frames during grab"（penduin/iceythe）：`blenderartists.org/t/sequencer-feature-revival-show-all-clips-start-end-frames-during-grab/1453986`
  - VSE Transform Tools：`github.com/kgeogeo/VSE_Transform_Tools`
  - POW! addon：`gist.github.com/Kurakurage-jp/a39615c84cdaecc57b609b47e998bcee`
  - VSE+：`mumumundo.gumroad.com/l/vse-plus`
  - Power Sequencer：`github.com/GDQuest/blender-power-sequencer`
  - MolecularNodes "Blender Gotchas"（redraw_timer 用法）：`bradyajohnston.github.io/MolecularNodes/api/blender.html`
