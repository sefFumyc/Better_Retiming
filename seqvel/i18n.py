# -*- coding: utf-8 -*-
"""运行层文案表（ADR-0009）：en/zh 双语，语言模式存插件偏好，出厂 ``en``。

**两层分工**（见 `docs/adr/0009-english-first-bilingual-ui.md`）：

- **运行层**（本表）：面板文字、右键菜单项、INFO/ERROR 报告、段类型档名、
  状态提示——经 :func:`T` 在运行时按语言模式切换；
- **静态层**（不进本表）：侧边栏标签、面板标题、operator 的 ``bl_label`` /
  ``bl_description``、设置项悬停说明——注册期固定、运行时切不了（Blender 限制，
  重注册险路已否决），按修订 R1 **常驻英中双语**（zh/en 模式同串；侧栏标签
  Velocity 例外，只留英文）。

规则：

- **源文案一律英文**，中文是表里的第二列——新增用户可见文案必须进表，
  不许在业务模块里硬编码中文字符串；
- 本模块**纯 Python 可导入**（rules/status 等零 bpy 层也要用）：读偏好走
  懒加载 + 全兜底，任何失败都回退 ``en``，绝不抛；
- 键名按模块加前缀（``status.`` / ``rules.`` / ``ui.`` …），格式串用
  ``%`` 占位，:func:`T` 的可变参数原样透传。
"""

EN = "en"
ZH = "zh"
DEFAULT = EN
LANGS = (EN, ZH)

#: 测试钩子：非 None 时 ``current_language`` 直接返回它（生产代码不许碰）。
_LANG_OVERRIDE = None

#: (英文, 中文)。键名 = 模块前缀 + 语义名；占位符两侧语言共用同一套参数。
_TEXT = {
    # ---- regions：区域说明 ----------------------------------------------
    "regions.no_area": ("Timeline region not found (context has no area)",
                        "找不到时间轴区域（context 无 area）"),
    "regions.wrong_area": ("Current area is %s, not the Video Sequence Editor",
                           "当前区域是 %s，不是视频序列编辑器"),
    "regions.no_timeline": ("No usable timeline region in the Video Sequence Editor",
                            "视频序列编辑器里没有可用的时间轴区域"),
    "regions.timeline": ("Timeline region %dx%d", "时间轴区域 %d×%d"),

    # ---- status：状态提示 / 模式名 ------------------------------------
    "status.hint_idle": ("Drag the speed line directly to edit (always shown on strips with a line)",
                         "直接按住速度线拖动即可编辑（建过线的条带常开显示）"),
    "status.hint_edit": ("Release to commit · Esc to end · Space/navigation unaffected",
                         "松手落盘 · Esc 结束 · 空格播放/导航不受影响"),
    "status.mode_idle": ("Idle", "空闲"),
    "status.mode_point": ("Dragging speed point", "拖动速度点"),
    "status.mode_segment": ("Dragging speed segment", "拖动速度段"),
    "status.mode_tail": ("Dragging tail extension", "拖动尾段延长线"),
    "status.fine_suffix": (" — Fine x0.5", " — 微调 ×0.5"),
    "status.header_editing": ("Velocity line editing (%s) — %s",
                              "速度线拖动中（%s）— %s"),

    # ---- curve：段类型档名 / 类型说明（ADR-0006 语义随语言直译） -------
    "curve.type_linear": ("Linear", "线性"),
    "curve.type_smooth": ("Smooth", "平滑"),
    "curve.type_fast": ("Fast", "快速"),
    "curve.type_slow": ("Slow", "慢速"),
    # v1.5.2 移除 curve.note_*（原枚举项级说明）：项级说明会与属性级说明
    # 拼接重复，且走 RNA 缓存切语言不保证重算（ADR-0009 修订 R2）。

    # ---- rules：条带类型规则文案 --------------------------------------
    "rules.audio_sound": ("Audio strip: Speed Control does not touch audio (no speed or pitch change); v1 has no audio retiming. Audio retiming needs native Retiming — separate topic.",
                          "音频条带：Speed Control 不影响音频（不变速也不变调），v1 不支持音频变速。音频变速需要走原生 Retiming，另议。"),
    "rules.audio_movie": ("Movie strip with audio: Speed Control only affects the picture; audio does not follow the speed change.",
                          "视频条带如带音轨：Speed Control 只处理画面，音频不会跟着变速。"),
    "rules.refuse_effect": ("Select a source strip (movie/image/scene), not an effect strip.",
                            "请选中源条带（视频/图片/场景），而不是效果条带。"),
    "rules.refuse_type": ("Strip type (%s) does not support speed lines.",
                          "该条带类型（%s）不支持速度线。"),
    "rules.unknown_type": ("unknown", "未知"),
    "rules.sampling_subframe": ("Subframe rendering", "子帧渲染"),
    "rules.sampling_snap": ("Frame-snapped", "取整选帧"),
    "rules.note_snap": ("Non-integer speeds repeat frames (blended frames are deliberately not used)",
                        "非整数倍速会有重复帧（刻意不做混合帧）"),
    "rules.note_raw_cache": ("This scene's Raw cache rounds subframes: output frames within one integer source frame share one image (upstream defect)",
                             "本场景 Raw 缓存把子帧取整：同一整数源帧内的输出帧共用一张图（上游缺陷）"),

    # ---- carrier：承载 / 目标诊断 --------------------------------------
    "carrier.no_editor": ("No sequence editor (the scene has no VSE data yet)",
                          "没有序列编辑器（场景里还没有 VSE 数据）"),
    "carrier.no_target_selected": ("Select a strip in the Video Sequence Editor first",
                                   "请在序列编辑器中先选中一条条带"),
    "carrier.create_failed": ("Could not create the carrier strip (Speed Control)",
                              "无法创建承载条带（Speed Control）"),
    "carrier.not_aligned": ("Carrier strip failed to align to the target (channel/position/length)",
                            "承载条带未能对齐到目标（通道/位置/长度）"),
    "carrier.keys_missing": ("Not all keyframes were written (%d/%d)",
                             "关键帧未全部写入（%d/%d）"),
    "carrier.interpolation_failed": ("Could not set keyframe interpolation (rendering may round corners)",
                                     "未能设置关键帧插值（渲染可能圆滑拐角）"),
    "carrier.invalidate_failed": ("Could not rebuild the speed frame map (the tail may keep the old timing): %s",
                                  "未能重建速度映射表（拉长出来的那一段可能仍按旧时序播）：%s"),
    "carrier.no_carrier": ("no carrier strip bound to this target",
                           "这条目标没有绑定的承载条带"),
    "carrier.no_scene": ("No usable scene (context.scene is empty)",
                         "当前没有可用场景（context.scene 为空）"),
    "carrier.wrong_area": ("Current area is %s, not the Video Sequence Editor",
                           "当前区域是 %s，不是视频序列编辑器"),
    "carrier.no_editor_data": ("Scene \"%s\" has no sequence editor data yet: add a strip in the VSE first.",
                               "场景「%s」还没有序列编辑器数据：先在 VSE 里添加一条条带。"),
    "carrier.read_active_failed": ("Failed to read the active strip (%s): %s",
                                   "读取活动条带失败（%s）：%s"),
    "carrier.read_strips_failed": ("Failed to read the strip list (%s): %s",
                                   "读取条带列表失败（%s）：%s"),
    "carrier.scene_summary": ("Target scene \"%s\" · %d strips · %d selected · active strip: %s",
                              "目标场景「%s」· 条带 %d 条 · 选中 %d 条 · 活动条带：%s"),
    "carrier.none_value": ("(none)", "（无）"),
    "carrier.no_strips": ("No strips in the editor yet.", "编辑器里还没有条带。"),
    "carrier.click_strip": ("Click a strip on the timeline (or box-select) to show its speed line.",
                            "在时间轴上点一下条带（或框选）即可显示速度线。"),
    "carrier.target_uneditable": ("Current target \"%s\" cannot be edited: %s",
                                  "当前目标「%s」不可编辑：%s"),
    "carrier.no_target": ("No usable strip", "没有可用的条带"),
    "carrier.select_first": ("Select a strip in the Video Sequence Editor first (%s) · add-on %s",
                             "请在序列编辑器中先选中一条条带（%s）· 插件 %s"),

    # ---- watch：端点监听 ------------------------------------------------
    "watch.write_failed": ("End-carry write failed (%s: %s)",
                           "末端跟随落盘失败（%s: %s）"),
    "watch.listener_error": ("Strip endpoint listener error (%s)",
                             "条带端点监听出错（%s）"),
    "watch.evaluated_target": ("Update list handed over evaluated copies (skipped: %s) — landing through them would be silently dropped",
                               "更新清单里给的是求值副本（已跳过：%s）——经它落地会被静默丢弃"),

    # ---- interaction：菜单 / 报告 / 拒绝 --------------------------------
    "interaction.start_failed": ("Drag failed to start: %s", "拖动启动失败：%s"),
    "interaction.interrupted": ("Drag interrupted: %s", "拖动中断：%s"),
    "interaction.execute_hint": ("Drag the speed line directly on the timeline to edit (no mouse event here, cannot start)",
                                 "直接在时间轴上按住速度线拖动即可编辑（无鼠标事件，无法在此启动）"),
    "interaction.insert_busy": ("There is already a speed point here (or the strip edge is too close)",
                                "此处已有速度点（或已到条带边缘）"),
    "interaction.menu_invoke_failed": ("Could not open the %s: %s", "弹不出%s：%s"),
    "interaction.menu_no_window": ("No window in the current context; cannot open the %s (target recorded, menu items still work)",
                                   "当前上下文没有窗口，弹不出%s（目标已记录，不影响菜单项生效）"),
    "interaction.point_menu_title": ("Speed point menu", "速度点菜单"),
    "interaction.segment_menu_title": ("Segment curve type menu", "段曲线类型菜单"),
    "interaction.menu_start_failed": ("Right-click menu failed to start: %s", "右键菜单启动失败：%s"),
    "interaction.menu_delete_point": ("Delete Point", "删除点"),
    "interaction.set_type": ("Set Segment Curve Type", "设置段曲线类型"),
    "interaction.set_type_desc": ("Set the right-clicked segment to the given curve type (linear/smooth/fast/slow)",
                                  "把右键选中的那一段设为指定曲线类型（线性/平滑/快速/慢速）"),
    "interaction.strip_missing": ("Strip \"%s\" not found (deleted or renamed?) — right-click it again",
                                  "找不到条带「%s」（可能已被删除或改名），请重新右键"),
    "interaction.segment_gone": ("This segment no longer exists (right-click it again)",
                                 "这一段已经不存在了（请重新右键）"),
    "interaction.segment_is": ("This segment is already \"%s\"", "这一段已经是「%s」"),
    "interaction.segment_set": ("This segment is now \"%s\"", "这一段已设为「%s」"),
    "interaction.segment_expired": ("Segment selection expired (about %.0f s) — right-click the segment again",
                                    "段的选择已过期（约 %.0f 秒），请重新右键那一段再选类型"),
    "interaction.point_expired": ("Speed point selection expired (about %.0f s) — right-click the point again",
                                  "速度点的选择已过期（约 %.0f 秒），请重新右键那个点再删除"),
    "interaction.point_gone": ("This speed point no longer exists (right-click it again)",
                               "这个速度点已经不存在了（请重新右键）"),
    "interaction.delete_point": ("Delete Speed Point", "删除速度点"),
    "interaction.delete_point_desc": ("Delete the right-clicked speed point (at least 2 points kept; the first point is protected)",
                                      "删除右键选中的速度点（至少保留 2 个，首点受保护）"),
    "interaction.point_deleted": ("Speed point deleted", "已删除速度点"),
    "interaction.first_point_protected": ("The first point cannot be deleted: the speed line must start at the strip start",
                                          "首点不可删除：速度线必须从素材起点开始"),
    "interaction.min_points": ("Keep at least 2 speed points", "至少保留 2 个速度点"),
    "interaction.point_vanished": ("This point no longer exists (right-click it again)",
                                   "这个点已经不存在了（请重新右键）"),
    "interaction.delete_line": ("Delete Speed Line", "删除速度线"),
    "interaction.delete_line_desc": ("Delete this strip's speed line and remove its Speed Control carrier strip",
                                     "删除该条带的速度线，并移除承载用的 Speed Control 效果条带"),
    "interaction.line_deleted": ("Speed line deleted (%d carrier strip(s))", "已删除速度线（承载条带 %d 条）"),

    # ---- hover：悬停光标 ------------------------------------------------
    "hover.register_failed": ("Hover cursor operator registration failed: %s",
                              "悬停光标 operator 注册失败：%s"),
    "hover.cursor_failed": ("Cannot change the mouse cursor (%s): %s",
                            "改不了鼠标光标（%s）：%s"),
    "hover.update_failed": ("Hover cursor update failed: %s",
                            "悬停光标更新失败：%s"),

    # ---- preview：内存预览（ADR-0011） ----------------------------------
    "preview.no_scene": ("No sequence scene to build (the context has no displayed scene)",
                         "没有可构建的场景（上下文里取不到显示场景）"),
    "preview.no_range": ("Pick a render range first: the preview range and the scene frame range are both empty/reversed",
                         "请先选一段范围：预览范围与场景帧范围都为空/首尾反了"),
    "preview.no_editor": ("This scene has no sequence editor data yet: add a strip first",
                          "这个场景还没有序列编辑器数据：先添加一条条带"),
    "preview.already_building": ("A RAM preview build is already running (wait for it, or press Esc) · add-on %s",
                                 "内存预览正在构建中（等它跑完，或按 Esc 中止）· 插件 %s"),
    "preview.building_hint": ("Building RAM preview — press Esc to stop; what is built so far stays usable",
                              "正在构建内存预览 — 按 Esc 停止，已构建的部分仍然可用"),
    "preview.will_not_fit": ("This range needs about %d MB while the cache limit is %d MB: it will stop once full",
                             "这段范围大约需要 %d MB，而缓存上限是 %d MB：装满后会自动停下"),
    "preview.aborted": ("RAM preview build aborted (playback never started) · add-on %s",
                        "内存预览构建中止（播放没能起来）· 插件 %s"),
    "preview.interrupted": ("RAM preview interrupted: %s", "内存预览中断：%s"),
    "preview.start_failed": ("RAM preview failed to start: %s", "内存预览启动失败：%s"),
    "preview.final_left_on": ("This scene's final cache was off: it stays on after the build, otherwise the frames just cached would be dropped at once",
                              "本场景 final 缓存原本是关的：构建后保持打开——否则刚装进缓存的画面会被立刻丢掉"),
    "preview.built_full": ("RAM preview built to frame %d · playhead back at the range start · add-on %s",
                           "内存预览已构建到第 %d 帧 · 播放头已回到范围开头 · 插件 %s"),
    "preview.built_part": ("Cache filled: built up to frame %d (limit %d MB) · add-on %s",
                           "内存已满：构建到第 %d 帧（上限 %d MB）· 插件 %s"),
    "preview.built_stopped": ("RAM preview stopped early: kept up to frame %d · add-on %s",
                              "内存预览提前停止：保留到第 %d 帧 · 插件 %s"),

    # ---- ui：面板 / 设置 / Raw 缓存 -------------------------------------
    "ui.language": ("Language", "语言"),
    "ui.scene_mismatch": ("Editing scene \"%s\" (active scene \"%s\")",
                          "编辑场景「%s」（活动场景「%s」）"),
    "ui.target_strip": ("Target strip: %s", "目标条带：%s"),
    "ui.target_none": ("Target strip: (none)", "目标条带：（无）"),
    "ui.target_meta": ("Type %s · channel %d · length %d frames",
                       "类型 %s · 通道 %d · 长度 %d 帧"),
    "ui.segment_types": ("Segment types %s", "段类型 %s"),
    "ui.no_data": ("— (no speed line data yet; right-click the line to pick a shape)",
                   "—（还没有速度线数据，右键线上即可选形状）"),
    "ui.editing_now": ("Editing: %s · %s", "编辑中：%s · %s"),
    "ui.hit_line": ("%s · hit %s", "%s · 命中 %s"),
    "ui.refuse_title": ("Cannot edit", "不能编辑"),
    "ui.audio_note": ("Audio note", "音频提示"),
    "ui.carrier_line1": ("Carrier %s (channel %d · %d~%d · keyframes %s)",
                         "承载 %s（通道 %d · %d~%d · 关键帧 %s）"),
    "ui.carrier_line2": ("Mode %s · sampling %s", "模式 %s · 取帧 %s"),
    "ui.keys_unreadable": ("unreadable", "读不回"),
    "ui.keys_count": ("%d", "%d 个"),
    "ui.bound_mismatch": ("Warning: carrier binding target differs from the active strip",
                          "注意：承载绑定目标与活动条带不一致"),
    "ui.carrier_pending": ("Carrier not created yet (created automatically on first edit)",
                           "承载尚未创建（编辑后自动生成 SPD_ 条带）"),
    "ui.error_title": ("Notice", "提示"),
    "ui.version_line": ("Add-on %s", "插件 %s"),
    "ui.keymap_line": ("Keymap %s", "键位 %s"),
    "ui.keymap_no_addon": ("No addon keyconfig (keyconfigs.addon is empty)",
                           "无 addon 键位配置（keyconfigs.addon 为空）"),
    "ui.keymap_none": ("none", "无"),
    "ui.keymap_mount_item": ("Keymap item %s(%s@%s) failed to mount: %s",
                             "键位 %s(%s@%s) 挂不上：%s"),
    "ui.keymap_mount_failed": ("Keymap mounting failed entirely", "键位表整体挂载失败"),
    "ui.diag_line": ("Last write: %s point(s) · %s keyframes · aligned %s · retiming reset %s",
                     "上次写入：点 %s · 关键帧 %s · 对齐 %s · Retiming 重置 %s"),
    "ui.yes": ("yes", "是"),
    "ui.no": ("no", "否"),
    "ui.settings_display": ("Display", "显示"),
    "ui.settings_birth": ("New curve", "新建曲线"),
    "ui.set_hide_retiming": ("Hide native retiming while editing", "编辑时隐藏原生 Retiming 显示"),
    "ui.set_hide_retiming_on": ("Hide", "隐藏"),
    "ui.set_hide_retiming_off": ("Show", "显示"),
    "ui.set_hud": ("Show speed percentages", "显示速度百分比"),
    "ui.set_hud_on": ("Always", "常显"),
    "ui.set_hud_off": ("On drag", "拖动中"),
    "ui.set_speed_cap": ("Display range cap", "显示范围上限"),
    "ui.set_default_type": ("Default segment curve type", "默认段曲线类型"),
    # v1.5.2 移除 set_*_desc（原枚举项级说明）：与属性级说明拼接成
    # "英中英/英中中"重复，且走 RNA 缓存切语言不保证重算（ADR-0009 修订 R2）。
    # 属性的 tooltip 一律由注册期双语 description 独自承担。
    "ui.raw_cache_unreadable": ("Raw cache state unreadable", "Raw 缓存状态读不到"),
    "ui.raw_cache_state": ("This scene's Raw cache: %s", "本场景 Raw 缓存：%s"),
    "ui.raw_on": ("on", "开"),
    "ui.raw_off": ("off", "关"),
    "ui.subframe_limited": ("Subframe capped", "无级受限"),
    "ui.subframe_ok": ("Subframe rendering active (stepless speed)", "子帧渲染生效（无级变速）"),
    "ui.raw_disable": ("Disable Raw cache (restore stepless)", "关闭 Raw 缓存（恢复无级）"),
    "ui.raw_enable": ("Enable Raw cache", "打开 Raw 缓存"),
    "ui.raw_toggle": ("Toggle this scene's Raw cache", "切换本场景 Raw 缓存"),
    "ui.raw_toggle_desc": ("Turn off this scene's Raw cache so scene strips render subframes (stepless speed). The cache is a scene-level setting: disabling costs a bit of memory and CPU",
                           "关闭本场景序列编辑器的 Raw 缓存，恢复场景条带的子帧渲染（无级变速）。缓存是场景级设置：关闭会略微增加内存与 CPU 开销"),
    "ui.raw_read_failed": ("Could not read the Raw cache switch (%s) · add-on %s",
                           "读不到 Raw 缓存开关（%s）· 插件 %s"),
    "ui.raw_write_failed": ("Could not change the Raw cache (%s) · add-on %s",
                            "改不动 Raw 缓存（%s）· 插件 %s"),
    "ui.raw_toggled": ("Raw cache %s · scene strip subframe rendering %s · add-on %s",
                       "本场景 Raw 缓存已%s · 场景条带子帧渲染%s · 插件 %s"),
    "ui.raw_now_off": ("disabled", "关闭"),
    "ui.raw_now_on": ("enabled", "打开"),
    "ui.raw_restored": ("restored (stepless)", "恢复（无级）"),
    "ui.raw_eaten": ("will be snapped", "会被吞掉"),
    "ui.overlays_reason_none": ("No strips to draw", "无待绘制条带"),
    "ui.draw_no_timeline": ("Cannot locate the timeline region (%s)",
                            "找不到时间轴区域（%s）"),
    "ui.draw_no_editor": ("No sequence editor", "无序列编辑器"),
    "ui.draw_no_shader": ("Built-in GPU shader unavailable", "取不到内置着色器"),
    "ui.draw_note_exception": ("exception", "异常"),
    "ui.draw_transient": ("ok(%d) · transient %d point(s)", "ok(%d) · 临时 %d点"),
    "ui.draw_ok": ("ok(%d)", "ok(%d)"),
    "ui.draw_error": ("Draw error (see console)", "绘制出错（详见控制台）"),
    "ui.delete_button": ("Delete Speed Line", "删除速度线"),
    "ui.ram_section": ("Memory Preview (Shift+B)", "内存预览（Shift+B）"),
    "ui.ram_range": ("Range %d~%d (%s)", "范围 %d~%d（%s）"),
    "ui.ram_source_preview": ("preview range", "预览范围"),
    "ui.ram_source_scene": ("scene range", "场景范围"),
    "ui.ram_no_range": ("Pick a range first (preview range or scene frame range)",
                        "先选一段范围（预览范围或场景帧范围）"),
    "ui.ram_idle": ("Not built yet — press Shift+B", "尚未构建 — 按 Shift+B"),
    "ui.ram_building": ("Building %d%% · frame %s", "构建中 %d%% · 第 %s 帧"),
    "ui.ram_built": ("Built to frame %s · %d MB cached", "已构建到第 %s 帧 · 缓存 %d MB"),
    "ui.ram_full": ("Cache filled: kept up to frame %s", "内存已满：保留到第 %s 帧"),
    "ui.ram_stopped": ("Stopped early: kept up to frame %s", "提前停止：保留到第 %s 帧"),
    "ui.ram_invalidated": ("Invalidated: the sequence changed, rebuild with Shift+B",
                           "已失效：序列有改动，请重新 Shift+B 构建"),
    "ui.ram_aborted": ("Build aborted — try Shift+B again", "构建中止 — 请再按一次 Shift+B"),
    "ui.ram_estimate": ("Needs about %d MB · limit %d MB", "大约需要 %d MB · 上限 %d MB"),
    "ui.ram_no_cache": ("No VSE data yet", "还没有 VSE 数据"),

    # ---- store：影子模型落盘 --------------------------------------------
    "store.materialize_failed": (
        "Could not write the other half of the cut (%s) — its curve stays in memory only",
        "切断的另一半没能写进条带（%s）——那条曲线暂时只活在内存里"),
}


def preferences():
    """插件偏好对象（语言模式存这里）；纯 Python / 未注册 / 任何异常 → None。"""
    try:
        import bpy
        if bpy is None:
            return None
        entry = bpy.context.preferences.addons.get(__package__.split(".")[0])
        return getattr(entry, "preferences", None) if entry is not None else None
    except Exception:
        return None


def current_language():
    """当前语言模式：插件偏好 ``language``，读不到回退 :data:`DEFAULT`。"""
    if _LANG_OVERRIDE is not None:
        return _LANG_OVERRIDE if _LANG_OVERRIDE in LANGS else DEFAULT
    prefs = preferences()
    lang = getattr(prefs, "language", None) if prefs is not None else None
    return lang if lang in LANGS else DEFAULT


def T(key, *args):
    """取当前语言的文案；``args`` 透传给 ``%`` 格式化。未知键原样返回。"""
    entry = _TEXT.get(key)
    if entry is None:
        return key
    text = entry[0] if current_language() == EN else entry[1]
    return text % args if args else text


def type_label(kind):
    """段曲线类型的显示名（四档；未知档原样返回）。"""
    return T("curve.type_%s" % kind) if kind in ("linear", "smooth", "fast", "slow") \
        else str(kind)


def set_language_for_test(lang):
    """测试钩子：临时钉住语言（None = 还原为读偏好）。生产代码不许调用。"""
    global _LANG_OVERRIDE
    _LANG_OVERRIDE = lang
